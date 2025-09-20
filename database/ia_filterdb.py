import logging
from struct import pack
import re
import base64
import datetime
import io
import aiohttp
import asyncio
from pyrogram import Client
from pyrogram.file_id import FileId
from pymongo.errors import DuplicateKeyError
from pyrogram.enums import ParseMode
from umongo import Instance, Document, fields
from motor.motor_asyncio import AsyncIOMotorClient
from marshmallow.exceptions import ValidationError
from info import CAPTION_LANGUAGES, DATABASE_URI, DATABASE_NAME, COLLECTION_NAME, USE_CAPTION_FILTER, MAX_B_TN, MOVIE_UPDATE_CHANNEL, OWNERID
from utils import get_settings, save_group_settings, temp, get_status
from database.users_chats_db import add_name
from .Imdbposter import get_movie_details, fetch_image
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from PIL import Image
from rapidfuzz import fuzz
from motor.motor_asyncio import AsyncIOMotorClient
from langdetect import detect_langs, DetectorFactory

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

# -------------------------
# Deterministic langdetect
# -------------------------
DetectorFactory.seed = 0

# -------------------------
# Universal language detection
# -------------------------
def detect_languages(filename: str, caption: str = ""):
    text = (filename + " " + caption).replace(".", " ").replace("_", " ")
    try:
        langs_probs = detect_langs(text)
        langs = [lp.lang.capitalize() for lp in langs_probs if lp.prob > 0.1]
        return langs if langs else ["Unknown"]
    except:
        return ["Unknown"]

# -------------------------
# Title cleaner and type detection
# -------------------------
def clean_title(filename: str):
    name = re.sub(r"[._]+", " ", filename)
    # TV series with episode
    match = re.match(r"(.+?)\s[Ss](\d{1,2})[Ee](\d{1,2})", name)
    if match:
        title = match.group(1).strip()
        season = match.group(2).zfill(2)
        episode = match.group(3).zfill(2)
        return f"{title} S{season}", True, episode
    # TV series with season only
    match = re.match(r"(.+?)\s[Ss](\d{1,2})", name)
    if match:
        title = match.group(1).strip()
        season = match.group(2).zfill(2)
        return f"{title} S{season}", True, None
    # Movie
    match = re.match(r"(.+?)\s*\(?(\d{4})\)?", name)
    if match:
        title = match.group(1).strip()
        return f"{title} {match.group(2)}", False, None
    return name.strip(), False, None

# -------------------------
# Example IMDb fetch function (replace with your actual async function)
# -------------------------
async def get_movie_details(title: str):
    # Dummy example for testing
    return {
        "imdb_url": f"https://www.imdb.com/title/tt0000000",
        "genre": ["Action", "Drama", "Mystery"]
    }

# -------------------------
# Main send_msg function
# -------------------------
async def send_msg(bot: Client, filename: str, caption: str = ""):
    try:
        clean_caption_title, is_series, season_episode = clean_title(filename)
        tag = "#𝚃𝚅𝚂𝙴𝚁𝙸𝙴𝚂" if is_series else "#𝙼𝙾𝚅𝙸𝙴"

        # For movies: check if any file with the same cleaned title exists
        existing_file = await Media.find_one({"file_name": {"$regex": clean_caption_title, "$options": "i"}})

        # For series: check if the same season already exists
        if is_series and season_episode:
            season_match = re.search(r"S(\d{2})", clean_caption_title)
            if season_match:
                season_num = season_match.group(1)
                existing_file = await Media.find_one({
                    "file_name": {"$regex": f"{clean_caption_title}", "$options": "i"},
                    "file_name": {"$regex": f"S{season_num}", "$options": "i"}
                })

        if existing_file:
            logger.info(f"⏩ Duplicate found: {clean_caption_title} → skipping")
            return


        # -------------------------
        # Detect languages automatically
        # -------------------------
        detected_langs = detect_languages(filename, caption)
        language = ", ".join(detected_langs)

        # -------------------------
        # Fetch IMDb data
        # -------------------------
        search_title = re.sub(r"\s[Ss]\d{1,2}", "", clean_caption_title)
        search_title = re.sub(r"\s\d{4}$", "", search_title).strip()
        imdb_data = await get_movie_details(search_title)
        imdb_link, genre = "", "Unknown"
        if imdb_data:
            imdb_link = imdb_data.get("imdb_url", "")
            for key in ["genre", "genres", "Genre"]:
                if key in imdb_data and imdb_data[key]:
                    raw_genre = imdb_data[key]
                    if isinstance(raw_genre, list):
                        genre = ", ".join([str(g).strip() for g in raw_genre])
                    else:
                        genre = str(raw_genre).strip()
                    break

        # -------------------------
        # Prepare caption
        # -------------------------
        final_caption = f"<b>✅ {clean_caption_title} {tag}</b>\n\n"
        final_caption += f"<blockquote><b>🎙 {language}</b></blockquote>\n\n"
        if imdb_link:
            final_caption += f"<b>⭐ <a href='{imdb_link}'>IMDb</a></b>\n"
        if genre:
            final_caption += f"<b>📽 Genre:</b> {genre}"

        # -------------------------
        # Send message
        # -------------------------
        await bot.send_message(
            chat_id=MOVIE_UPDATE_CHANNEL,
            text=final_caption,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                "🔍 𝙲𝚕𝚒𝚌𝚔 𝚝𝚘 𝚂𝚎𝚊𝚛𝚌𝚑",
                url=f"https://telegram.me/{bot.me.username}?start=getfile-{clean_caption_title.replace(' ', '-')}"
            )]])
        )

        logger.info(f"[NEW] Sent message for {clean_caption_title}")

    except Exception as e:
        logger.error(f"❌ Error in send_msg: {e}")


        
async def get_qualities(text, qualities: list):
    """Get all Quality from text"""
    quality = []
    for q in qualities:
        if q in text:
            quality.append(q)
    quality = ", ".join(quality)
    return quality[:-2] if quality.endswith(", ") else quality






