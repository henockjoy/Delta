import sys
import glob
import importlib
import aiohttp
import os
from pathlib import Path
from pyrogram import Client, idle, __version__
from pyrogram.raw.all import layer
from pyrogram.errors import FloodWait
import logging
import logging.config
import time
import asyncio
from datetime import date, datetime
import pytz
from aiohttp import web
import psutil  # For memory monitoring

from database.ia_filterdb import Media, choose_mediaDB, tempDict, db as clientDB
from database.users_chats_db import db
from info import *
from utils import temp
from Script import script
from plugins import web_server, check_expired_premium
from LucyBot.Bot import Codeflix
from LucyBot.util.keepalive import ping_server
from LucyBot.Bot.clients import initialize_clients

logging.config.fileConfig('logging.conf')
logging.getLogger().setLevel(logging.INFO)
logging.getLogger("pyrogram").setLevel(logging.ERROR)
logging.getLogger("imdbpy").setLevel(logging.ERROR)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logging.getLogger("aiohttp").setLevel(logging.ERROR)
logging.getLogger("aiohttp.web").setLevel(logging.ERROR)

botStartTime = time.time()
ppath = "plugins/*.py"
files = glob.glob(ppath)

MAX_RESTARTS = 5
RESTART_DELAY = 10  # seconds
MEMORY_THRESHOLD = 80  # restart if RAM usage > 80%

def memory_ok(threshold=MEMORY_THRESHOLD):
    """Check if memory usage is below the threshold."""
    mem = psutil.virtual_memory()
    return mem.percent < threshold

async def Lucy_start():
    print('\nInitializing Yoon')
    if not memory_ok():
        raise MemoryError(f"Memory usage too high: {psutil.virtual_memory().percent}%")
    
    try:
        await Codeflix.start()
    except FloodWait as e:
        print(f"FloodWait: sleeping for {e.value} seconds")
        await asyncio.sleep(e.value)
        await Codeflix.start()
    
    bot_info = await Codeflix.get_me()
    Codeflix.username = bot_info.username
    await initialize_clients()
    
    for name in files:
        with open(name) as a:
            patt = Path(a.name)
            plugin_name = patt.stem.replace(".py", "")
            plugins_dir = Path(f"plugins/{plugin_name}.py")
            import_path = "plugins.{}".format(plugin_name)
            spec = importlib.util.spec_from_file_location(import_path, plugins_dir)
            load = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(load)
            sys.modules["plugins." + plugin_name] = load
            print("Lucy Bot Imported => " + plugin_name)
    
    if ON_HEROKU:
        asyncio.create_task(ping_server()) 
    
    b_users, b_chats = await db.get_banned()
    temp.BANNED_USERS = b_users
    temp.BANNED_CHATS = b_chats
    await Media.ensure_indexes()
    
    stats = await clientDB.command('dbStats')
    free_dbSize = round(512-((stats['dataSize']/(1024*1024))+(stats['indexSize']/(1024*1024))), 2)
    logging.info(f"Since primary DB have enough space ({free_dbSize}MB) left, It will be used for storing datas.")
    
    await choose_mediaDB()    
    me = await Codeflix.get_me()
    temp.ME = me.id
    temp.U_NAME = me.username
    temp.B_NAME = me.first_name
    temp.B_LINK = me.mention
    Codeflix.username = '@' + me.username
    Codeflix.loop.create_task(check_expired_premium(Codeflix))
    
    logging.info(f"{me.first_name} with Pyrogram v{__version__} (Layer {layer}) started on {me.username}.")
    logging.info(LOG_STR)
    logging.info(script.LOGO)
    
    tz = pytz.timezone('Asia/Kolkata')
    today = date.today()
    now = datetime.now(tz)
    time_str = now.strftime("%H:%M:%S %p")
    
    # Send initial start/restart message
    await Codeflix.send_message(chat_id=LOG_CHANNEL, text=script.RESTART_TXT.format(temp.B_LINK, today, time_str))
    
    app = web.AppRunner(await web_server())
    await app.setup()
    bind_address = "0.0.0.0"
    await web.TCPSite(app, bind_address, PORT).start()
    
    await idle()

async def start_with_restart():
    """Auto-restart the bot on crash or high memory usage"""
    restarts = 0
    while True:
        try:
            await Lucy_start()
        except KeyboardInterrupt:
            logging.info("Service Stopped By User 👋")
            break
        except MemoryError as me:
            logging.error(f"Memory error: {me}", exc_info=True)
            restarts += 1
            if restarts > MAX_RESTARTS:
                logging.error("Too many memory crashes. Exiting.")
                break
            logging.info(f"Restarting bot in {RESTART_DELAY} seconds due to high memory usage... (Attempt {restarts}/{MAX_RESTARTS})")
            # Send restart message to LOG_CHANNEL
            try:
                await Codeflix.send_message(LOG_CHANNEL, f"⚠️ Bot restarting due to high memory usage ({psutil.virtual_memory().percent}%)\nAttempt {restarts}/{MAX_RESTARTS}")
            except:
                pass
            await asyncio.sleep(RESTART_DELAY)
        except Exception as e:
            logging.error(f"Bot crashed with error: {e}", exc_info=True)
            restarts += 1
            if restarts > MAX_RESTARTS:
                logging.error("Too many crashes. Exiting.")
                break
            logging.info(f"Restarting bot in {RESTART_DELAY} seconds... (Attempt {restarts}/{MAX_RESTARTS})")
            # Send restart message to LOG_CHANNEL
            try:
                await Codeflix.send_message(LOG_CHANNEL, f"⚠️ Bot crashed and is restarting due to error:\n{e}\nAttempt {restarts}/{MAX_RESTARTS}")
            except:
                pass
            await asyncio.sleep(RESTART_DELAY)

if __name__ == '__main__':
    try:
        asyncio.run(start_with_restart())
    except Exception as e:
        logging.error(f"Failed to start bot: {e}", exc_info=True)
