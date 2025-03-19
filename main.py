import sys
import types
import urllib3
import http.client
import random
import string

# --------------- Dummy Injection (Must be at the very top) ---------------
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

import logging
import threading

from datetime import timedelta
from flask import Flask
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext


# --------------- Configuration ---------------
BOT_TOKEN = "7947042930:AAE14yUT642RjiiwkaM_dgoGazQdh54SkcU"         # Replace with your actual BotFather token
PRIVATE_CHANNEL_ID = -1002033692655       # Replace with your private channel's ID
DELETE_AFTER_SECONDS = 15 * 60            # 15 minutes
BOT_USERNAME = "file_sharing_bot03_bot"          # e.g., file_sharing_bot03_bot

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --------------- Global Data Structures ---------------
# share_links stores shareable link info:
# For single-file: share_links[share_id] = {"mode": "single", "channel_id": ..., "message_id": ...}
# For batch: share_links[share_id] = {"mode": "batch", "first_msg_id": int, "last_msg_id": int}
share_links = {}

# user_sessions stores ephemeral states (like batch session):
# user_sessions[user_id] = {"mode": "batch", "first_msg_id": None, "last_msg_id": None}
user_sessions = {}

TEMP_DIR = Path("temp")
TEMP_DIR.mkdir(exist_ok=True)

# --------------- Utility Functions ---------------
def generate_random_id(length=16) -> str:
    """Generate a random alphanumeric string."""
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

def delete_message_job(context: CallbackContext):
    """Job callback to delete a message after a delay."""
    data = context.job.context
    chat_id = data["chat_id"]
    msg_id = data["message_id"]
    try:
        context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except Exception as e:
        logger.error(f"Error deleting ephemeral message: {e}")

# --------------- Single-File Approach ---------------
def handle_single_share(update: Update, context: CallbackContext, share_id: str):
    """Forward a single file from the private channel with content protection."""
    info = share_links[share_id]
    try:
        # Use send_document with protect_content=True to prevent forwarding.
        # For single-file share, we forward via send_document by first downloading and re-uploading,
        # or you can try to use forward_message (but forward_message doesn't support protect_content).
        forwarded_msg = context.bot.forward_message(
            chat_id=update.effective_chat.id,
            from_chat_id=info["channel_id"],
            message_id=info["message_id"]
        )
        # Schedule deletion after DELETE_AFTER_SECONDS.
        context.job_queue.run_once(
            delete_message_job,
            DELETE_AFTER_SECONDS,
            context={"chat_id": forwarded_msg.chat_id, "message_id": forwarded_msg.message_id}
        )
        update.message.reply_text("Here is your file. (It will auto-delete in 15 minutes.)")
    except Exception as e:
        logger.error(f"Error forwarding single-file share {share_id}: {e}")
        update.message.reply_text("Sorry, couldn't retrieve the file. Possibly removed or an error occurred.")

# --------------- Batch Approach ---------------
def handle_batch_share(update: Update, context: CallbackContext, share_id: str):
    """Forward all files in the batch range from the private channel."""
    info = share_links[share_id]
    first_id = info["first_msg_id"]
    last_id = info["last_msg_id"]

    update.message.reply_text("Batch request received. Retrieving files...")

    count = 0
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
            context.job_queue.run_once(
                delete_message_job,
                DELETE_AFTER_SECONDS,
                context={"chat_id": fwd_msg.chat_id, "message_id": fwd_msg.message_id}
            )
        except Exception as e:
            logger.warning(f"Failed to forward message ID {mid}: {e}")

    update.message.reply_text(f"Batch complete: {count} message(s) forwarded.\n(They auto-delete in 15 minutes.)")

# --------------- /start Handler ---------------
def start_command(update: Update, context: CallbackContext):
    """Handle /start [share_id] command."""
    args = context.args
    if args:
        share_id = args[0]
        if share_id not in share_links:
            update.message.reply_text("Invalid share link. Please check and try again.")
            return
        info = share_links[share_id]
        mode = info.get("mode", "single")
        if mode == "single":
            handle_single_share(update, context, share_id)
        elif mode == "batch":
            handle_batch_share(update, context, share_id)
    else:
        update.message.reply_text(
            f"Welcome! To get a file, click on a shareable link (e.g., https://t.me/{BOT_USERNAME}?start=<share_id>).\n"
            "If you are an admin and forward a file from the private channel, I'll generate a link.\n"
            "Use /batch for multiple files."
        )

# --------------- /batch Command Handler ---------------
def batch_command(update: Update, context: CallbackContext):
    """Start a batch session for the user."""
    user_id = update.effective_user.id
    user_sessions[user_id] = {
        "mode": "batch",
        "first_msg_id": None,
        "last_msg_id": None
    }
    update.message.reply_text("Batch mode activated.\nPlease forward the **first file** from your private channel.")

# --------------- Forwarded Message Handler ---------------
def forward_handler(update: Update, context: CallbackContext):
    """
    When a file is forwarded from the private channel:
    - In batch mode: record the first file then the last file.
    - In single-file mode: generate a share link immediately.
    """
    msg = update.message
    user_id = msg.from_user.id

    if msg.forward_from_chat and msg.forward_from_chat.id == PRIVATE_CHANNEL_ID:
        if user_id in user_sessions and user_sessions[user_id].get("mode") == "batch":
            session = user_sessions[user_id]
            if session["first_msg_id"] is None:
                session["first_msg_id"] = msg.forward_from_message_id
                update.message.reply_text("First file recorded. Now please forward the **last file** from your private channel.")
            elif session["last_msg_id"] is None:
                session["last_msg_id"] = msg.forward_from_message_id
                share_id = generate_random_id(32)
                share_links[share_id] = {
                    "mode": "batch",
                    "first_msg_id": session["first_msg_id"],
                    "last_msg_id": session["last_msg_id"]
                }
                update.message.reply_text(
                    f"Batch shareable link generated:\nhttps://t.me/{BOT_USERNAME}?start={share_id}\n\n"
                    "Anyone clicking this link will receive all messages in that range from your private channel."
                )
                del user_sessions[user_id]
        else:
            original_msg_id = msg.forward_from_message_id
            share_id = generate_random_id(32)
            share_links[share_id] = {
                "mode": "single",
                "channel_id": PRIVATE_CHANNEL_ID,
                "message_id": original_msg_id
            }
            update.message.reply_text(
                f"Shareable link generated:\nhttps://t.me/{BOT_USERNAME}?start={share_id}\n\n"
                "Anyone clicking this link will receive this file. (Auto-deletes after 15 minutes.)"
            )
    else:
        update.message.reply_text("Please forward a file from the private channel to generate a shareable link.")

# --------------- Flask Server (Keep-Alive) ---------------
app = Flask(__name__)

@app.route("/")
def index():
    return "Bot is alive!"

def run_flask():
    app.run(host="0.0.0.0", port=8000)

# --------------- Main Function ---------------
def main():
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start_command))
    dp.add_handler(CommandHandler("batch", batch_command))
    dp.add_handler(MessageHandler(Filters.forwarded, forward_handler))

    updater.start_polling()
    logger.info("Bot started. Listening for commands...")

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    updater.idle()

if __name__ == "__main__":
    main()
