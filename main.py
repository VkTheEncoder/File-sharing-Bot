import sys
import types
import urllib3
import http.client

# --------------- Dummy Injection for Python 3.13 ---------------
if not hasattr(urllib3, "contrib"):
    contrib_module = types.ModuleType("urllib3.contrib")
    sys.modules["urllib3.contrib"] = contrib_module
    urllib3.contrib = contrib_module

if "urllib3.contrib.appengine" not in sys.modules:
    mod_appengine = types.ModuleType("urllib3.contrib.appengine")
    mod_appengine.monkeypatch = lambda: None
    mod_appengine.is_appengine_sandbox = lambda: False
    sys.modules["urllib3.contrib.appengine"] = mod_appengine
    urllib3.contrib.appengine = mod_appengine
else:
    if not hasattr(urllib3.contrib.appengine, "monkeypatch"):
        urllib3.contrib.appengine.monkeypatch = lambda: None
    if not hasattr(urllib3.contrib.appengine, "is_appengine_sandbox"):
        urllib3.contrib.appengine.is_appengine_sandbox = lambda: False

import os
import logging
import random
import string
import threading

from datetime import timedelta
from flask import Flask
from telegram import Update
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    Filters,
    CallbackContext
)

# Import PyMongo
from pymongo import MongoClient



# --------------- Configuration ---------------
BOT_TOKEN = "7947042930:AAE14yUT642RjiiwkaM_dgoGazQdh54SkcU"  # Replace with your bot token
PRIVATE_CHANNEL_ID = -1002033692655  # Replace with your private channel ID
DELETE_AFTER_SECONDS = 15 * 60       # 15 minutes
BOT_USERNAME = "file_sharing_bot03_bot"

# MongoDB configuration: set MONGO_URI as an environment variable or use your connection string here
MONGO_URI = os.environ.get("MONGO_URI", "mongodb+srv://soseh50374:WEsff3bG5XrNcunn@cluster0.6lfh0jj.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0")
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["telegram_bot"]      # Database name
links_collection = db["share_links"]     # Collection name

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --------------- Data Structures ---------------
# user_sessions stores ephemeral states (like batch session).
# user_sessions[user_id] = {
#   "mode": "batch",
#   "first_msg_id": None,
#   "last_msg_id": None
# }
user_sessions = {}

# --------------- Utility Functions ---------------
def generate_random_id(length=16) -> str:
    """Generate a random alphanumeric string."""
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

def delete_message_job(context: CallbackContext):
    """Delete a previously re-sent message after X seconds."""
    data = context.job.context
    chat_id = data["chat_id"]
    msg_id = data["message_id"]
    try:
        context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except Exception as e:
        logger.error(f"Error deleting ephemeral message: {e}")

# --------------- Database Helper Functions ---------------
def save_share_link(share_id: str, data: dict):
    """Save a share link document in MongoDB."""
    document = {"share_id": share_id}
    document.update(data)
    links_collection.insert_one(document)

def get_share_link(share_id: str):
    """Retrieve a share link document from MongoDB."""
    return links_collection.find_one({"share_id": share_id})

# --------------- Single-File Approach ---------------
def handle_single_share(update: Update, context: CallbackContext, share_id: str, info: dict):
    """Ephemeral approach for single-file share."""
    try:
        forwarded_msg = context.bot.forward_message(
            chat_id=update.effective_chat.id,
            from_chat_id=info["channel_id"],
            message_id=info["message_id"]
        )
        # Schedule ephemeral deletion
        context.job_queue.run_once(
            delete_message_job,
            DELETE_AFTER_SECONDS,
            context={"chat_id": forwarded_msg.chat_id, "message_id": forwarded_msg.message_id}
        )
        update.message.reply_text("Here is your file. (Auto-deletes in 15 minutes.)")
    except Exception as e:
        logger.error(f"Error forwarding single-file share {share_id}: {e}")
        update.message.reply_text("Sorry, couldn't retrieve the file. Possibly removed or an error occurred.")

# --------------- Batch Approach ---------------
def handle_batch_share(update: Update, context: CallbackContext, share_id: str, info: dict):
    """Ephemeral approach for batch share: forward all messages from first_msg_id to last_msg_id."""
    first_id = info["first_msg_id"]
    last_id = info["last_msg_id"]

    update.message.reply_text("Batch request received. Retrieving files...")

    count = 0
    # Ensure first_id <= last_id
    if first_id > last_id:
        first_id, last_id = last_id, first_id

    for mid in range(first_id, last_id + 1):
        try:
            fwd_msg = context.bot.forward_message(
                chat_id=update.effective_chat.id,
                from_chat_id=PRIVATE_CHANNEL_ID,
                message_id=mid
            )
            count += 1
            # Schedule ephemeral deletion for each forwarded message
            context.job_queue.run_once(
                delete_message_job,
                DELETE_AFTER_SECONDS,
                context={"chat_id": fwd_msg.chat_id, "message_id": fwd_msg.message_id}
            )
        except Exception as e:
            logger.warning(f"Failed to forward message ID {mid}: {e}")

    update.message.reply_text(f"Batch complete: {count} messages forwarded.\n(They auto-delete in 15 minutes.)")

# --------------- /start Handler ---------------
def start_command(update: Update, context: CallbackContext):
    """ /start command. If param is share_id, handle single-file or batch. Else show help. """
    args = context.args
    if args:
        share_id = args[0]
        info = get_share_link(share_id)
        if not info:
            update.message.reply_text("Invalid share link. Please check the link and try again.")
            return
        mode = info.get("mode", "single")
        if mode == "single":
            handle_single_share(update, context, share_id, info)
        elif mode == "batch":
            handle_batch_share(update, context, share_id, info)
    else:
        update.message.reply_text(
            f"Welcome! To get a file, click on a shareable link (e.g. https://t.me/{BOT_USERNAME}?start=<share_id>).\n"
            "If you're an admin and forward a file from the private channel to me, I'll generate a shareable link.\n"
            "Or use /batch for multiple files."
        )

# --------------- /batch Command ---------------
def batch_command(update: Update, context: CallbackContext):
    """Start a batch session for the user."""
    user_id = update.effective_user.id
    user_sessions[user_id] = {
        "mode": "batch",
        "first_msg_id": None,
        "last_msg_id": None
    }
    update.message.reply_text("Batch mode activated.\nPlease forward the first file from your private channel.")

# --------------- Forwarded Messages Handler ---------------
def forward_handler(update: Update, context: CallbackContext):
    """When a file is forwarded from the private channel, handle single-file share or batch logic."""
    msg = update.message
    user_id = msg.from_user.id

    # Verify that the forwarded message is from the private channel
    if msg.forward_from_chat and msg.forward_from_chat.id == PRIVATE_CHANNEL_ID:
        if user_id in user_sessions and user_sessions[user_id].get("mode") == "batch":
            session = user_sessions[user_id]
            if session["first_msg_id"] is None:
                session["first_msg_id"] = msg.forward_from_message_id
                update.message.reply_text("First file recorded. Now please forward the last file from your private channel.")
            elif session["last_msg_id"] is None:
                session["last_msg_id"] = msg.forward_from_message_id
                # Create a share_id for batch sharing
                share_id = generate_random_id(32)
                save_share_link(share_id, {
                    "mode": "batch",
                    "first_msg_id": session["first_msg_id"],
                    "last_msg_id": session["last_msg_id"]
                })
                update.message.reply_text(
                    f"Batch shareable link generated:\nhttps://t.me/{BOT_USERNAME}?start={share_id}\n\n"
                    "Anyone clicking this link will receive all messages in that ID range from your private channel."
                )
                # End batch session
                del user_sessions[user_id]
        else:
            # Handle single-file share
            original_message_id = msg.forward_from_message_id
            share_id = generate_random_id(32)
            save_share_link(share_id, {
                "mode": "single",
                "channel_id": PRIVATE_CHANNEL_ID,
                "message_id": original_message_id
            })
            update.message.reply_text(
                f"Shareable link generated:\nhttps://t.me/{BOT_USERNAME}?start={share_id}\n\n"
                "Anyone clicking this link will receive this file. The re-sent message auto-deletes after 15 minutes."
            )
    else:
        update.message.reply_text("Please forward a file from the private channel to generate a shareable link.")

# --------------- Main ---------------
def main():
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start_command))
    dp.add_handler(CommandHandler("batch", batch_command))
    dp.add_handler(MessageHandler(Filters.forwarded, forward_handler))

    updater.start_polling()
    logger.info("Bot started. Listening for commands...")

    # Start a minimal Flask server for uptime monitoring
    app = Flask(__name__)
    @app.route("/")
    def index():
        return "Bot is alive!"
    def run_flask():
        app.run(host="0.0.0.0", port=8000)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    updater.idle()

if __name__ == "__main__":
    main()
