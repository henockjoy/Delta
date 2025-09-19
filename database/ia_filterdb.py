import logging
from struct import pack
import re
import base64
import io
import aiohttp
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
# Helper: Clean noisy tags from filenames
# ------------------------------
def remove_noise_tags(filename: str) -> str:
    noise_patterns = [
        r"\bHQ\b", r"\bHDRip\b", r"\bWEBRip\b", r"\bWEB-DL\b", r"\bWEB-HD\b", r"\bBluRay\b",
        r"\b10bit\b", r"\bDDP?5\.1\b", r"\bAAC\b", r"\bATMOS\b", r"\bx264\b",
        r"\bx265\b", r"\bHEVC\b", r"\bESub\b", r"\bMULTi\b", r"\bDS4K\b",
        r"\b[0-9]{3,4}p\b", r"\bJHS\b", r"\bMRiPS\b", r"\bPahe\.in\b"
    ]
    name = filename
    for pattern in noise_patterns:
        name = re.sub(pattern, "", name, flags=re.IGNORECASE)
    name = re.sub(r"[._]+", " ", name)
    return name.strip()

# ------------------------------
# Title Cleaning Functions
# ------------------------------
def clean_title(filename: str, is_series: bool = False) -> str:
    name = remove_noise_tags(filename)
    if is_series:
        match = re.match(r"(.+?)\s*[Ss](\d{1,2})(?:[ ._-]?[Ee](\d{1,2}))?", name)
        if match:
            title = match.group(1).strip()
            season = match.group(2).zfill(2)
            episode = match.group(3).zfill(2) if match.group(3) else None
            if episode:
                return f"{title} S{season}E{episode}"
            return f"{title} S{season}"
    else:
        match = re.match(r"(.+?)\s*\(?(\d{4})\)?", name)
        if match:
            title = match.group(1).strip()
            year = match.group(2)
            return f"{title} {year}"
    return name.strip()

def clean_button_link(filename: str) -> str:
    name = remove_noise_tags(filename)
    match = re.match(r"(.+?)\s*[Ss](\d{1,2})", name)
    if match:
        title = match.group(1).strip().replace(" ", "-")
        season = match.group(2).zfill(2)
        return f"{title}-S{season}"
    match = re.match(r"(.+?)\s*\(?(\d{4})\)?", name)
    if match:
        title = match.group(1).strip().replace(" ", "-")
        year = match.group(2)
        return f"{title}-{year}"
    return name.split()[0].replace(" ", "-")
# ------------------------------
# Initialize MongoDB collection
# ------------------------------
mongo_client = AsyncIOMotorClient(DATABASE_URI)
db = mongo_client[DATABASE_NAME]
collection = db[COLLECTION_NAME]

# ------------------------------
# Send Message Function
# ------------------------------
async def send_msg(bot, filename, caption, is_series=False, collection=None):
    try:
        clean_caption_title = clean_title(filename, is_series)
        button_base = clean_button_link(filename)
        tag = "#𝚃𝚅𝚂𝙴𝚁𝙸𝙴𝚂" if is_series else "#𝙼𝙾𝚅𝙸𝙴"

        # ------------------------
        # Duplicate check using fuzzy matching (read-only)
        # ------------------------
        duplicate_found = False
        async for f in collection.find({}, {"file_name": 1, "caption": 1}):
            db_name = f.get("file_name", "") or ""
            db_caption = f.get("caption", "") or ""
            if fuzz.ratio(clean_caption_title.lower(), db_name.lower()) > 90 or \
               fuzz.ratio(clean_caption_title.lower(), db_caption.lower()) > 90:
                duplicate_found = True
                break
        if duplicate_found:
            logger.info(f"Skipping duplicate (fuzzy match): {clean_caption_title}")
            return

        # ------------------------
        # IMDb details
        # ------------------------
        imdb = await get_movie_details(clean_caption_title)
        if not imdb or not imdb.get("imdb_url"):
            logger.info(f"IMDb not found for: {clean_caption_title}")
            return  # skip sending if IMDb not found

        imdb_link = imdb.get("imdb_url", "")
        genre = "Unknown"
        for key in ["genre", "genres", "Genre"]:
            if key in imdb and imdb[key]:
                if isinstance(imdb[key], list):
                    genre = ", ".join([str(g).strip() for g in imdb[key] if g])
                else:
                    genre = str(imdb[key]).strip()
                break

        # ------------------------
        # Detect languages
        # ------------------------
        detected_langs = []
        for lang in CAPTION_LANGUAGES:
            if re.search(rf"\b{re.escape(lang.lower())}\b", caption.lower()) or \
               re.search(rf"\b{re.escape(lang.lower())}\b", filename.lower()):
                detected_langs.append(lang)
        seen = set()
        unique_langs = [l for l in detected_langs if not (l in seen or seen.add(l))]
        language = ", ".join(unique_langs) if unique_langs else "Unknown"

        # ------------------------
        # Final caption
        # ------------------------
        final_caption = (
            f"<b>✅ {clean_caption_title} {tag}</b>\n\n"
            f"<blockquote><b>🎙 {language}</b></blockquote>\n\n"
            f"<b>⭐ <a href='{imdb_link}'>IMDb</a></b>\n"
            f"<b>📽 Genre:</b> {genre}"
        )

        # ------------------------
        # Inline button
        # ------------------------
        btn = [[
            InlineKeyboardButton(
                "🔍 𝙲𝚕𝚒𝚌𝚔 𝚝𝚘 𝚂𝚎𝚊𝚛𝚌𝚑",
                url=f"https://telegram.me/{bot.me.username}?start=getfile-{button_base}"
            )
        ]]

        # ------------------------
        # Send message
        # ------------------------
        await bot.send_message(
            chat_id=MOVIE_UPDATE_CHANNEL,
            text=final_caption,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(btn)
        )

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






