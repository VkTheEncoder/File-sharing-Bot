import sys
import types
import urllib3
import http.client
import random
import string
from datetime import timedelta
import threading

# --- Dummy injection for missing urllib3.contrib and its appengine submodule ---
if not hasattr(urllib3, "contrib"):
    contrib_module = types.ModuleType("urllib3.contrib")
    sys.modules["urllib3.contrib"] = contrib_module
    urllib3.contrib = contrib_module

if "urllib3.contrib.appengine" not in sys.modules:
    mod_appengine = types.ModuleType("urllib3.contrib.appengine")
    # Provide a dummy monkeypatch function (does nothing)
    mod_appengine.monkeypatch = lambda: None
    # Provide is_appengine_sandbox that always returns False
    mod_appengine.is_appengine_sandbox = lambda: False
    sys.modules["urllib3.contrib.appengine"] = mod_appengine
    urllib3.contrib.appengine = mod_appengine
else:
    if not hasattr(urllib3.contrib.appengine, "monkeypatch"):
        urllib3.contrib.appengine.monkeypatch = lambda: None
    if not hasattr(urllib3.contrib.appengine, "is_appengine_sandbox"):
        urllib3.contrib.appengine.is_appengine_sandbox = lambda: False

# ------------------- Telegram Bot Code Below -------------------

import logging
from flask import Flask
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext

# -------------------- CONFIGURATION --------------------
BOT_TOKEN = "7947042930:AAE14yUT642RjiiwkaM_dgoGazQdh54SkcU"   # Replace with your actual BotFather token
PRIVATE_CHANNEL_ID = -1002033692655 # Replace with your private channel's ID
DELETE_AFTER_SECONDS = 15 * 60      # 15 minutes
BOT_USERNAME = "file_sharing_bot03_bot"    # e.g., file_sharing_bot03_bot
# --------------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global dictionary to store shareable links:
# Structure: share_links[share_id] = { "channel_id": ..., "message_id": ... }
share_links = {}

def generate_random_id(length=8):
    """Generate a random alphanumeric string of specified length."""
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

def delete_message_job(context: CallbackContext):
    """Job callback to delete a message after some delay."""
    job_data = context.job.context
    chat_id = job_data["chat_id"]
    msg_id = job_data["message_id"]
    try:
        context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except Exception as e:
        logger.error(f"Error deleting message: {e}")

def start(update: Update, context: CallbackContext):
    """
    Handler for /start command.
    If a start parameter is provided, it is treated as a shareable ID.
    Otherwise, a help message is sent.
    """
    args = context.args
    if args:
        share_id = args[0]
        if share_id in share_links:
            file_info = share_links[share_id]
            try:
                forwarded_msg = context.bot.forward_message(
                    chat_id=update.effective_chat.id,
                    from_chat_id=file_info["channel_id"],
                    message_id=file_info["message_id"]
                )
                # Schedule deletion of the forwarded message after DELETE_AFTER_SECONDS.
                context.job_queue.run_once(
                    delete_message_job,
                    DELETE_AFTER_SECONDS,
                    context={"chat_id": forwarded_msg.chat_id, "message_id": forwarded_msg.message_id}
                )
                update.message.reply_text("Here is your file. (This message will be auto-deleted after 15 minutes.)")
            except Exception as e:
                logger.error(f"Error forwarding message for share ID {share_id}: {e}")
                update.message.reply_text("Sorry, I couldn't retrieve the file. It might have been removed.")
        else:
            update.message.reply_text("Invalid share link. Please check the link and try again.")
    else:
        # No parameter => show help
        update.message.reply_text(
            f"Welcome! To get a file, click on a shareable link (e.g., https://t.me/{BOT_USERNAME}?start=SHARE_ID).\n"
            "If you are an admin and forward a file from the private channel to me, I will generate such a link for you."
        )

def forward_from_private(update: Update, context: CallbackContext):
    """
    When the bot receives a forwarded message from the private channel,
    it generates a shareable link and sends it back.
    """
    if update.message.forward_from_chat and update.message.forward_from_chat.id == PRIVATE_CHANNEL_ID:
        original_message_id = update.message.forward_from_message_id
        # Generate a random share ID (e.g. 32 chars)
        share_id = generate_random_id(length=32)
        share_links[share_id] = {
            "channel_id": PRIVATE_CHANNEL_ID,
            "message_id": original_message_id
        }
        # Create a deep link URL
        share_url = f"https://t.me/{BOT_USERNAME}?start={share_id}"
        update.message.reply_text(
            f"Shareable link generated:\n{share_url}\n\n"
            "Anyone clicking this link will receive the file. The re-sent message will auto-delete after 15 minutes."
        )
    else:
        update.message.reply_text("Please forward a file from the private channel to generate a shareable link.")

# -------------------- FLASK SERVER --------------------
app = Flask(__name__)

@app.route("/")
def index():
    return "Bot is alive!"

def run_flask():
    app.run(host="0.0.0.0", port=8000)

# -------------------- MAIN FUNCTION --------------------
def main():
    """Start the Telegram bot and Flask server."""
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    # Handlers
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(MessageHandler(Filters.forwarded, forward_from_private))

    # Start the bot (polling) in the background
    updater.start_polling()
    logger.info("Bot started. Listening for commands...")

    # Start the Flask web server in a separate thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Keep the bot running until interrupted
    updater.idle()

if __name__ == "__main__":
    main()
