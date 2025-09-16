import sys
import glob
import importlib
import aiohttp
import os
import sys
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

async def keep_alive():
    """Send a request every 111 seconds to keep the bot alive (if required)."""
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(KEEP_ALIVE_URL) as resp:
                    logging.info(f"Keep-alive request sent (status {resp.status}).")
            except Exception as e:
                logging.error(f"Keep-alive request failed: {e}")
            await asyncio.sleep(111)

async def kulasthree(self):
    """Restart the bot every 24 hours."""
    while True:
        await asyncio.sleep(24 * 60 * 60)  # wait 24 hours
        logging.info("🔄 Bot is restarting")
        try:
            await self.send_message(chat_id=LOG_CHANNEL, text="🔄 Bot is restarting ...")
        except Exception as e:
            logging.error(f"Failed to send restart message: {e}")
        os.execl(sys.executable, sys.executable, *sys.argv)

async def Lucy_start():
    print('\n')
    print('\nInitalizing Yoon')
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
    time = now.strftime("%H:%M:%S %p")
    await Codeflix.send_message(chat_id=LOG_CHANNEL, text=script.RESTART_TXT.format(temp.B_LINK, today, time))
    app = web.AppRunner(await web_server())
    await app.setup()
    bind_address = "0.0.0.0"
    await web.TCPSite(app, bind_address, PORT).start()
    await idle()
    
if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(Lucy_start())
    except KeyboardInterrupt:
        logging.info('Service Stopped Bye 👋')
