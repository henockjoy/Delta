import logging
from struct import pack
import re
import base64
from pyrogram.file_id import FileId
from pymongo.errors import DuplicateKeyError
from umongo import Instance, Document, fields
from motor.motor_asyncio import AsyncIOMotorClient
from marshmallow.exceptions import ValidationError
from info import CAPTION_LANGUAGES, DATABASE_URI, DATABASE_NAME, COLLECTION_NAME, USE_CAPTION_FILTER, MAX_B_TN, MOVIE_UPDATE_CHANNEL, OWNERID
from utils import get_settings, save_group_settings, temp, get_status
from database.users_chats_db import add_name
from .Imdbposter import get_movie_details, fetch_image
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
import asyncio
from collections import defaultdict
from urllib.parse import quote

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
#---------------------------------------------------------
# Some basic variables needed
tempDict = {'indexDB': DATABASE_URI}

# Primary DB
client = AsyncIOMotorClient(DATABASE_URI)
db = client[DATABASE_NAME]
instance = Instance.from_db(db)


# Primary DB Model
@instance.register
class Media(Document):
    file_id = fields.StrField(attribute='_id')
    file_ref = fields.StrField(allow_none=True)
    file_name = fields.StrField(required=True)
    file_size = fields.IntField(required=True)
    file_type = fields.StrField(allow_none=True)
    mime_type = fields.StrField(allow_none=True)
    caption = fields.StrField(allow_none=True)

    class Meta:
        indexes = ('$file_name', )
        collection_name = COLLECTION_NAME

async def choose_mediaDB():
    """Always use Media as the database."""
    global saveMedia
    logger.info("Using primary db (Media)")
    saveMedia = Media

async def save_file(bot, media):
    """Save file in Media database"""
    file_id, file_ref = unpack_new_file_id(media.file_id)
    file_name = re.sub(r"(_|\-|\.|\+)", " ", str(media.file_name))

    try:
        # Always check duplicates in Media
        if await Media.count_documents({'file_id': file_id}, limit=1):
            logger.warning(f'{file_name} is already saved in Media database!')
            return False, 0

        file = Media(
            file_id=file_id,
            file_ref=file_ref,
            file_name=file_name,
            file_size=media.file_size,
            file_type=media.file_type,
            mime_type=media.mime_type,
            caption=media.caption.html if media.caption else None,
        )

    except ValidationError:
        logger.exception('Error occurred while saving file in Media')
        return False, 2

    else:
        try:
            await file.commit()
        except DuplicateKeyError:
            logger.warning(f'{getattr(media, "file_name", "NO_FILE")} is already saved in Media')   
            return False, 0
        else:
            logger.info(f'{getattr(media, "file_name", "NO_FILE")} is saved to Media')
            if await get_status(bot.me.id):
                await send_msg(bot, file.file_name, file.caption)
            return True, 1

async def get_search_results(chat_id, query, file_type=None, max_results=10, offset=0, filter=False):
    """For given query return (results, next_offset, total_results)"""
    if chat_id is not None:
        settings = await get_settings(int(chat_id))
        try:
            if settings['max_btn']:
                max_results = 10
            else:
                max_results = int(MAX_B_TN)
        except KeyError:
            await save_group_settings(int(chat_id), 'max_btn', False)
            settings = await get_settings(int(chat_id))
            if settings['max_btn']:
                max_results = 10
            else:
                max_results = int(MAX_B_TN)

    query = query.strip()
    if not query:
        raw_pattern = '.'
    elif ' ' not in query:
        raw_pattern = r'(\b|[\.\+\-_])' + query + r'(\b|[\.\+\-_])'
    else:
        raw_pattern = query.replace(' ', r'.*[\s\.\+\-_()]')
    
    try:
        regex = re.compile(raw_pattern, flags=re.IGNORECASE)
    except:
        return []

    if USE_CAPTION_FILTER:
        filter = {'$or': [{'file_name': regex}, {'caption': regex}]}
    else:
        filter = {'file_name': regex}

    if file_type:
        filter['file_type'] = file_type

    total_results = await Media.count_documents(filter)

    # verifies max_results is an even number or not
    if max_results % 2 != 0: 
        logger.info(f"Since max_results is an odd number ({max_results}), bot will use {max_results+1} as max_results to make it even.")
        max_results += 1

    cursor = Media.find(filter)
    cursor.sort('$natural', -1)
    cursor.skip(offset).limit(max_results)

    files = await cursor.to_list(length=max_results)
    next_offset = offset + len(files)

    if next_offset >= total_results:
        next_offset = ''

    return files, next_offset, total_results


async def get_bad_files(query, file_type=None, filter=False):
    """For given query return (results, total_results)"""
    query = query.strip()
    if not query:
        raw_pattern = '.'
    elif ' ' not in query:
        raw_pattern = r'(\b|[\.\+\-_])' + query + r'(\b|[\.\+\-_])'
    else:
        raw_pattern = query.replace(' ', r'.*[\s\.\+\-_()]')
    
    try:
        regex = re.compile(raw_pattern, flags=re.IGNORECASE)
    except:
        return []

    if USE_CAPTION_FILTER:
        filter = {'$or': [{'file_name': regex}, {'caption': regex}]}
    else:
        filter = {'file_name': regex}

    if file_type:
        filter['file_type'] = file_type

    cursor = Media.find(filter)
    cursor.sort('$natural', -1)

    files = await cursor.to_list(length=(await Media.count_documents(filter)))
    total_results = len(files)

    return files, total_results

async def get_file_details(query):
    filter = {'file_id': query}
    cursor = Media.find(filter)
    filedetails = await cursor.to_list(length=1)
    return filedetails


def encode_file_id(s: bytes) -> str:
    r = b""
    n = 0

    for i in s + bytes([22]) + bytes([4]):
        if i == 0:
            n += 1
        else:
            if n:
                r += b"\x00" + bytes([n])
                n = 0

            r += bytes([i])

    return base64.urlsafe_b64encode(r).decode().rstrip("=")

def encode_file_ref(file_ref: bytes) -> str:
    return base64.urlsafe_b64encode(file_ref).decode().rstrip("=")

def unpack_new_file_id(new_file_id):
    """Return file_id, file_ref"""
    decoded = FileId.decode(new_file_id)
    file_id = encode_file_id(
        pack(
            "<iiqq",
            int(decoded.file_type),
            decoded.dc_id,
            decoded.media_id,
            decoded.access_hash
        )
    )
    file_ref = encode_file_ref(decoded.file_reference)
    return file_id, file_ref

# ------------------------------
# Episode batching storage
# ------------------------------
episode_batch = defaultdict(list)   # key = "SeriesName S01", value = list of episodes
batch_tasks = {}                    # key = series_key → scheduled task
batch_messages = {}                 # key = series_key → message_id

# ------------------------------
# Schedule batched series message
# ------------------------------
async def schedule_series_batch(bot, series_key, display_name, language, genres):
    """Send or update combined message for a batch of episodes"""
    await asyncio.sleep(10)  # batch delay

    try:
        episodes = list(dict.fromkeys(episode_batch.get(series_key, [])))
        if not episodes:
            return

        # Show episodes as range
        if len(episodes) == 1:
            episodes_line = f"<b>📽 Episodes:</b> {episodes[0]}\n\n"
        else:
            episodes_line = f"<b>📽 Episodes:</b> {episodes[0]} - {episodes[-1]}\n\n"

        text = f"<b>✅{display_name} #𝖳𝖵𝖲𝖤𝖱𝖨𝖤𝖲</b>\n\n"
        text += f"<blockquote><b>🎙 {language}</b></blockquote>\n"
        text += episodes_line
        text += f"<b>📽 Genre:</b> {genres}"

        btn_link = f"https://telegram.me/{temp.U_NAME}?start=getfile-{quote(display_name.replace(' ', '-'))}"
        btn = [[InlineKeyboardButton('🔍 𝙲𝚕𝚒𝚌𝚔 𝚝𝚘 𝚂𝚎𝚊𝚛𝚌𝚑', url=btn_link)]]

        # Edit existing message if exists
        if series_key in batch_messages:
            try:
                await bot.edit_message_text(
                    chat_id=MOVIE_UPDATE_CHANNEL,
                    message_id=batch_messages[series_key],
                    text=text,
                    reply_markup=InlineKeyboardMarkup(btn)
                )
            except Exception as e:
                logging.error(f"Failed to edit message: {e}")
            finally:
                episode_batch.pop(series_key, None)
                batch_tasks.pop(series_key, None)
            return

        # Send new message
        msg = await bot.send_message(
            chat_id=MOVIE_UPDATE_CHANNEL,
            text=text,
            reply_markup=InlineKeyboardMarkup(btn)
        )
        batch_messages[series_key] = msg.id
        episode_batch.pop(series_key, None)
        batch_tasks.pop(series_key, None)

    except Exception as e:
        logging.error(f"schedule_series_batch error: {e}")

# ------------------------------
# Send message for movies or series
# ------------------------------
async def send_msg(bot, filename, caption):
    try:
        # Clean filename & caption
        filename = re.sub(r'\(\@\S+\)|\[\@\S+\]|\b@\S+|\bwww\.\S+', '', filename).strip()
        caption = re.sub(r'\(\@\S+\)|\[\@\S+\]|\b@\S+|\bwww\.\S+', '', caption).strip()

        # ------------------------------
        # Detect season & episode
        # ------------------------------
        season, episode = None, None

        se_ep_match = re.search(r"(?i)S(\d{1,2})E(\d{1,3})", filename) \
            or re.search(r"(?i)S(\d{1,2})E(\d{1,3})", caption)

        if se_ep_match:
            season, episode = se_ep_match.group(1), se_ep_match.group(2)
        else:
            season_match = re.search(r"(?i)(?:\bS|Season)\s*0*(\d{1,2})\b", filename) \
                or re.search(r"(?i)(?:\bS|Season)\s*0*(\d{1,2})\b", caption)
            episode_match = re.search(r"(?i)\bE(\d{1,3})\b", filename) \
                or re.search(r"(?i)\bE(\d{1,3})\b", caption)
            season = season_match.group(1) if season_match else None
            episode = episode_match.group(1) if episode_match else None

        if season:
            season = season.zfill(2)
        if episode:
            episode = episode.zfill(2)

        is_series = True if (season or episode) else False
        tag = "#𝖳𝖵𝖲𝖤𝖱𝖨𝖤𝖲" if is_series else "#𝖬𝖮𝖵𝖨𝖤"

        # Trim filename
        year_match = re.search(r"\b(19|20)\d{2}\b", caption)
        year = year_match.group(0) if year_match else None
        if year:
            filename = filename[: filename.find(year) + 4]
        elif season and season in filename:
            filename = filename[: filename.find(season) + len(season)]

        # Language detection (unchanged)
        language = ""
        for lang in CAPTION_LANGUAGES:
            if lang.lower() in caption.lower():
                language += f"{lang}, "
        language = language[:-2] if language else "Unknown"

        # Clean display name
        clean_name = re.sub(r"[\(\)\[\]\{\}:;'\-!]", "", filename).strip()

        # Duplication key
        display_name = clean_name
        if is_series and not re.search(r"(?i)S\d{1,2}", clean_name):
            display_name += f" S{season.zfill(2)}"
        unique_key = display_name

        if not await add_name(OWNERID, unique_key):
            return  # skip duplicates

        # Fetch genres (unchanged)
        imdb = await get_movie_details(clean_name)
        if imdb and imdb.get("genres"):
            genres = ", ".join(imdb["genres"]) if isinstance(imdb["genres"], list) else imdb["genres"]
        else:
            genres = "Unknown"

        # ------------------------------
        # Series batching
        # ------------------------------
        if is_series:
            series_key = f"{clean_name} S{season}" if season else clean_name
            episode_batch[series_key].append(f"E{episode}" if episode else "E??")

            # Start or restart batching task
            if series_key in batch_tasks:
                batch_tasks[series_key].cancel()
            batch_tasks[series_key] = asyncio.create_task(
                schedule_series_batch(bot, series_key, display_name, language, genres)
            )
            return

        # ------------------------------
        # Movie message
        # ------------------------------
        text = f"<b>✅{display_name} {tag}</b>\n\n"
        text += f"<blockquote><b>🎙 {language}</b></blockquote>\n\n"
        text += f"<b>📽 Genre:</b> {genres}"

        btn_link = f"https://telegram.me/{temp.U_NAME}?start=getfile-{quote(display_name.replace(' ', '-'))}"
        btn = [[InlineKeyboardButton('🔍 𝙲𝚕𝚒𝚌𝚔 𝚝𝚘 𝚂𝚎𝚊𝚛𝚌𝚑', url=btn_link)]]

        await bot.send_message(
            chat_id=MOVIE_UPDATE_CHANNEL,
            text=text,
            reply_markup=InlineKeyboardMarkup(btn)
        )

    except Exception as e:
        logging.error(f"send_msg error: {e}")
        
async def get_qualities(text, qualities: list):
    """Get all Quality from text"""
    quality = []
    for q in qualities:
        if q in text:
            quality.append(q)
    quality = ", ".join(quality)
    return quality[:-2] if quality.endswith(", ") else quality






