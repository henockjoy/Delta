import logging
from struct import pack
import re
import base64
import aiohttp
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
from rapidfuzz import fuzz

aiohttp_session = None

TMDB_API_KEY = "0da1b0909b6f81d9543daf54db258f5a"
TMDB_BASE = "https://api.themoviedb.org/3"

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

def get_cert_emoji(cert):
    cert = (cert or "").upper().strip()

    mapping = {
        # 🎬 Movies (MPAA)
        "G": "👶 G",
        "PG": "👨‍👩‍👧 PG",
        "PG-13": "🎬 PG-13",
        "R": "🔞 R",
        "NC-17": "⛔ NC-17",
        "NR": "🚫 NR",
        "UR": "✍🏻 UR",

        # 📺 TV Ratings
        "TV-Y": "👶 TV-Y",
        "TV-Y7": "👦 TV-Y7",
        "TV-G": "👶 TV-G",
        "TV-PG": "👨‍👩‍👧 TV-PG",
        "TV-14": "🎞️ TV-14",
        "TV-MA": "🔞 TV-MA",
    }

    # 🔥 Smart handling for messy TMDB values
    if cert.startswith("PG-13"):
        return "🎬 PG-13"
    if cert.startswith("PG"):
        return "👨‍👩‍👧 PG"
    if cert.startswith("TV-Y7"):
        return "👦 TV-Y7"
    if cert.startswith("TV-Y"):
        return "👶 TV-Y"
    if cert.startswith("TV-MA"):
        return "🔞 TV-MA"
    if cert.startswith("TV-14"):
        return "🎞️ TV-14"
    if cert.startswith("TV-PG"):
        return "👨‍👩‍👧 TV-PG"

    return mapping.get(cert, "🚫 NR")

async def init_aiohttp():
    global aiohttp_session
    if aiohttp_session is None:
        aiohttp_session = aiohttp.ClientSession()

async def close_aiohttp():
    global aiohttp_session
    if aiohttp_session:
        await aiohttp_session.close()

async def get_tmdb_card(query):
    if aiohttp_session is None:
        raise RuntimeError("aiohttp session not initialized")
    try:
        search_url = f"{TMDB_BASE}/search/multi"

        async with aiohttp_session.get(search_url, params={
            "api_key": TMDB_API_KEY,
            "query": query
        }, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()

        results = data.get("results", [])
        if not results:
            return None   

        def best_match(results, query):
            best = None
            best_score = 0
            for r in results:
                title = r.get("title") or r.get("name") or ""
                score = fuzz.token_sort_ratio(query.lower(), title.lower())
                if score > best_score:
                    best = r
                    best_score = score
            return best or results[0]

        item = best_match(results, query)
        media_type = item.get("media_type")
        media_id = item.get("id")

        # ------------------------------
        # DETAILS
        # ------------------------------
        detail_url = f"{TMDB_BASE}/{media_type}/{media_id}"

        async with aiohttp_session.get(detail_url, params={
            "api_key": TMDB_API_KEY
        }, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            detail = await resp.json()

        genres = [g["name"] for g in detail.get("genres", [])]

        

        # ------------------------------
        # Runtime & Rating
        # ------------------------------
        if media_type == "movie":
            runtime = detail.get("runtime") or 0
        else:
            runtime_list = detail.get("episode_run_time") or []
            runtime = runtime_list[0] if runtime_list else 0

        rating = detail.get("vote_average") or 0

        hours = runtime // 60
        minutes = runtime % 60
        runtime_str = f"{hours}h {minutes}m" if runtime else "Unknown"
        rating = round(rating, 1) if rating else "N/A"

        # ------------------------------
        # Certification
        # ------------------------------
        certification = "NR"

        try:
            if media_type == "movie":
                cert_url = f"{TMDB_BASE}/movie/{media_id}/release_dates"

                async with aiohttp_session.get(cert_url, params={
                    "api_key": TMDB_API_KEY
                }) as resp:
                    cert_data = await resp.json()

                for country in cert_data.get("results", []):
                    if country["iso_3166_1"] in ["IN", "US", "GB"]:
                        for rel in country.get("release_dates", []):
                            if rel.get("certification"):
                                certification = rel["certification"]
                                break
                    if certification != "NR":
                        break

            elif media_type == "tv":
                cert_url = f"{TMDB_BASE}/tv/{media_id}/content_ratings"

                async with aiohttp_session.get(cert_url, params={
                    "api_key": TMDB_API_KEY
                }) as resp:
                    cert_data = await resp.json()

                for country in cert_data.get("results", []):
                    if country["iso_3166_1"] in ["IN", "US", "GB"]:
                        if country.get("rating"):
                            certification = country["rating"]
                            break

        except Exception as e:
            logger.warning(f"Certification fetch failed: {e}")

        # ------------------------------
        # FINAL RETURN
        # ------------------------------
        return {
            "genres": genres,
            "runtime": runtime_str,
            "rating": rating,
            "certification": certification
        }

    except Exception as e:
        logger.error(f"TMDB error: {e}")
        return None

# ------------------------------
# Episode batching storage
# ------------------------------
episode_batch = defaultdict(list)   # key = "SeriesName S01", value = list of episodes
batch_tasks = {}                    # key = series_key → scheduled task
batch_messages = {}                 # key = series_key → message_id

# ------------------------------
# Schedule batched series message
# ------------------------------
async def schedule_series_batch(bot, series_key, display_name, language, genres, cert, runtime, rating):
    """Send or update combined message for a batch of episodes"""
    await asyncio.sleep(10)  # batch delay

    try:
        episodes = list(dict.fromkeys(episode_batch.get(series_key, [])))
        if not episodes:
            return

        text = f"<b>✅{display_name} #𝖳𝖵𝖲𝖤𝖱𝖨𝖤𝖲</b>\n"
        text += f"<code>{cert} | ⏱ {runtime} | ⭐ {rating}</code>\n\n"
        text += f"<blockquote><b>🎙 {language}</b></blockquote>\n"
        text += f"<b>📽 Genre:</b> {genres}\n\n"

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
        caption = re.sub(r'\(\@\S+\)|\[\@\S+\]|\b@\S+|\bwww\.\S+', '', caption or '').strip()

        # ------------------------------
        # Detect season & episode (IMPROVED)
        # ------------------------------
        season, episode = None, None

        # Detect SxxExx / SxxVxx
        se_ep_match = re.search(r"(?i)S(\d{1,2})\s*[-._ ]?\s*(?:E|EP|V)(\d{1,3})", filename) \
            or re.search(r"(?i)S(\d{1,2})\s*[-._ ]?\s*(?:E|EP|V)(\d{1,3})", caption)

        if se_ep_match:
            season, episode = se_ep_match.group(1), se_ep_match.group(2)

        # Detect only Season (S01)
        season_only_match = re.search(r"(?i)\bS(\d{1,2})\b", filename) \
            or re.search(r"(?i)\bS(\d{1,2})\b", caption)

        if not season and season_only_match:
            season = season_only_match.group(1)

        # Normalize
        if season:
            season = season.zfill(2)
        if episode:
            episode = episode.zfill(2)

        # FINAL DECISION
        is_series = True if season else False
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
        language = language[:-2] if language else "Orginal Audio"

        # Clean existing season/episode from name
        clean_name = re.sub(r"(?i)\bS\d{1,2}([EVP]\d{1,3})?\b", "", clean_name).strip()

        # Build display name
        if season and episode:
            display_name = f"{clean_name} S{season}E{episode}"
        elif season:
            display_name = f"{clean_name} S{season}"
        else:
            display_name = clean_name

        if not await add_name(OWNERID, unique_key):
            return  # skip duplicates

        # Fetch genres (unchanged)
        def clean_query(text):
            text = re.sub(r"\b(19|20)\d{2}\b", "", text)
            text = re.sub(r"[.\-_]", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
            return text
        clean_name = re.sub(r"\bS\d{1,2}E\d{1,3}\b", "", clean_name)
        clean_name = re.sub(r"\b(WEB-DL|WEBRip|HDRip|BluRay|AAC|x264|H264|DDP5\.1)\b", "", clean_name, flags=re.I)
        clean_name = re.sub(r"\b(19|20)\d{2}\b", "", clean_name)
        clean_name = re.sub(r"[.\-_]", " ", clean_name)
        clean_name = re.sub(r"\s+", " ", clean_name).strip()
        tmdb = await get_tmdb_card(clean_name)

        if tmdb:
            genres = ", ".join(tmdb.get("genres", [])) or "Unknown"

            runtime = tmdb.get("runtime", "Unknown")
            rating = tmdb.get("rating", "N/A")

            cert_raw = tmdb.get("certification", "NR")
            cert = get_cert_emoji(cert_raw)

        else:
            genres = "Unknown"
            runtime = "Unknown"
            rating = "N/A"
            cert = get_cert_emoji("NR")

        # ------------------------------
        # Series batching
        # ------------------------------
        if is_series:
            season_num = season.zfill(2) if season else "01"
            series_key = f"{clean_name} S{season_num}"
            if episode and episode.isdigit():
                episode_batch[series_key].append(f"E{episode}")

            # Start or restart batching task
            if series_key in batch_tasks:
                batch_tasks[series_key].cancel()
            batch_tasks[series_key] = asyncio.create_task(
                schedule_series_batch(bot, series_key, display_name, language, genres, cert, runtime, rating)
            )
            return

        # ------------------------------
        # Movie message
        # ------------------------------
        text = f"<b>✅{display_name} {tag}</b>\n"
        text += f"<code>{cert} | ⏱ {runtime} | ⭐ {rating}</code>\n\n"
        text += f"<blockquote><b>🎙 {language}</b></blockquote>\n"
        text += f"<b>📽 Genre:</b> {genres}\n\n"

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
    return quality


