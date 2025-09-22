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
batch_tasks = {}  # prevent duplicate scheduling

# ------------------------------
# Schedule batched series message
# ------------------------------
async def schedule_series_batch(bot, series_key, display_name, language, genres):
    """Send combined message after delay for a batch of episodes"""
    await asyncio.sleep(10)  # batch delay (10s)
    
    # Preserve order, remove duplicates
    episodes = list(dict.fromkeys(episode_batch[series_key]))
    episode_batch.pop(series_key, None)  # clear batch
    batch_tasks.pop(series_key, None)    # clear task

    episodes_line = f"<b>📽 Episodes:</b> {', '.join(episodes)}\n\n"
    text = f"<b>✅{display_name} #𝖳𝖵𝖲𝖤𝖱𝖨𝖤𝖲</b>\n\n"
    text += f"<blockquote><b>🎙 {language}</b></blockquote>\n"
    text += episodes_line
    text += f"<b>📽 Genre:</b> {genres}"

    # Clean button link (no repeated S01)
    btn_link = f"https://telegram.me/{temp.U_NAME}?start=getfile-{display_name.replace(' ', '-')}"
    btn = [[InlineKeyboardButton('🔍 𝙲𝚕𝚒𝚌𝚔 𝚝𝚘 𝚂𝚎𝚊𝚛𝚌𝚑', url=btn_link)]]

    await bot.send_message(
        chat_id=MOVIE_UPDATE_CHANNEL,
        text=text,
        reply_markup=InlineKeyboardMarkup(btn)
    )

# ------------------------------
# Send message for movies or series
# ------------------------------
async def send_msg(bot, filename, caption): 
    try:
        # Clean filename & caption
        filename = re.sub(r'\(\@\S+\)|\[\@\S+\]|\b@\S+|\bwww\.\S+', '', filename).strip()
        caption = re.sub(r'\(\@\S+\)|\[\@\S+\]|\b@\S+|\bwww\.\S+', '', caption).strip()
        
        # Year detection
        year_match = re.search(r"\b(19|20)\d{2}\b", caption)
        year = year_match.group(0) if year_match else None

        # Season / Episode detection
        season_match = re.search(r"(?i)(?:s|season)0*(\d{1,2})", caption) or re.search(r"(?i)(?:s|season)0*(\d{1,2})", filename)
        episode_match = re.search(r"(?i)E(\d{1,3})", caption) or re.search(r"(?i)E(\d{1,3})", filename)

        season = season_match.group(1) if season_match else None 
        episode = episode_match.group(1) if episode_match else None

        # Decide Movie / Series
        is_series = True if (season or episode) else False
        tag = "#𝖳𝖵𝖲𝖤𝖱𝖨𝖤𝖲" if is_series else "#𝖬𝖮𝖵𝖨𝖤"

        # Trim filename if needed
        if year:
            filename = filename[: filename.find(year) + 4]  
        elif season and season in filename:
            filename = filename[: filename.find(season) + 1]

        # Language detection
        language = ""
        for lang in CAPTION_LANGUAGES:
            if lang.lower() in caption.lower():
                language += f"{lang}, "
        language = language[:-2] if language else "🤔 Unknown 😄"

        # Clean filename for display
        clean_name = re.sub(r"[\(\)\[\]\{\}:;'\-!]", "", filename).strip()

        # Duplication check key
        if is_series:
            # Avoid repeated Sxx
            display_name = clean_name
            if not re.search(r"(?i)S\d{1,2}", clean_name):
                display_name += f" S{season.zfill(2)}"
            series_key = display_name
            unique_key = series_key
        else:
            display_name = clean_name
            unique_key = clean_name

        if not await add_name(OWNERID, unique_key):
            return  # already posted, skip

        # Fetch metadata
        imdb = await get_movie_details(clean_name)  
        if imdb and imdb.get("genres"):
            if isinstance(imdb["genres"], list):
                genres = ", ".join(imdb["genres"])
            else:
                genres = imdb["genres"]
        else:
            genres = "🎭 Unknown 😅"

        # Handle series batching
        if is_series:
            if episode:
                episode_batch[series_key].append(f"E{episode.zfill(2)}")
            else:
                episode_batch[series_key].append("E??")

            if series_key not in batch_tasks:
                batch_tasks[series_key] = asyncio.create_task(
                    schedule_series_batch(bot, series_key, display_name, language, genres)
                )
            return  # don't send immediately

        # Movies → send immediately
        text = f"<b>✅{display_name} {tag}</b>\n\n"
        text += f"<blockquote><b>🎙 {language}</b></blockquote>\n\n"
        text += f"📽 Genre: {genres}"

        # Clean button link
        btn_link = f"https://telegram.me/{temp.U_NAME}?start=getfile-{display_name.replace(' ', '-')}"
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






