import asyncio
import datetime
from database.ia_filterdb import Media  # Your main media collection
from LucyBot.Bot import send_msg       # Make sure send_msg() is implemented in your bot

async def watch_media_collection(bot):
    """
    Watch the main Media collection for new inserts and send messages automatically.
    """
    print("✅ MongoDB watcher started")

    while True:
        try:
            today = datetime.date.today().isoformat()
            
            # Fetch unsent media for today
            async for media in Media.find({"sent": {"$ne": True}}):
                filename = media.get("filename")
                caption = media.get("caption", "")

                if filename:
                    # Call your existing send_msg() to handle TV/Movie logic
                    await send_msg(bot, filename, caption)

                    # Mark as sent in Media collection
                    await Media.update_one(
                        {"_id": media["_id"]},
                        {"$set": {"sent": True}}
                    )

            # Sleep 5 seconds before checking again
            await asyncio.sleep(5)

        except Exception as e:
            print(f"Watcher error: {e}")
            await asyncio.sleep(5)
