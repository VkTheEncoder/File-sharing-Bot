import sys
import types
import urllib3
import http.client
import random
import string
from datetime import timedelta
import threading

# --- Dummy injection for missing imghdr (for Python 3.13) ---
if "imghdr" not in sys.modules:
    mod_imghdr = types.ModuleType("imghdr")
    def what(file, h=None):
        return None
    mod_imghdr.what = what
    sys.modules["imghdr"] = mod_imghdr

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

# --- Ensure external 'six' is available and mapped to Telegram's vendor path ---
try:
    import six
except ImportError:
    print("Module 'six' is required. Please install it using: pip install six")
    sys.exit(1)
sys.modules["telegram.vendor.ptb_urllib3.urllib3.packages.six"] = six
sys.modules["telegram.vendor.ptb_urllib3.urllib3.packages.six.moves"] = six.moves

# --- Map http.client to Telegram's expected module location ---
sys.modules["telegram.vendor.ptb_urllib3.urllib3.packages.six.moves.http_client"] = http.client

# ------------------- Telegram Bot Code Below -------------------

import logging
from flask import Flask
from telegram import Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext

# -------------------- CONFIGURATION --------------------
BOT_TOKEN = "7947042930:AAE14yUT642RjiiwkaM_dgoGazQdh54SkcU"   # Replace with your actual BotFather token
PRIVATE_CHANNEL_ID = -1002033692655 # Replace with your private channel's ID
DELETE_AFTER_SECONDS = 15 * 60      # 15 minutes (in seconds)
BOT_USERNAME = "file_sharing_bot03_bot"
# --------------------------------------------------------

logging.basicConfig(level=logging.INFO)

# Global dictionary to store shareable links:
# Structure: share_links[share_id] = { "channel_id": ..., "message_id": ... }
share_links = {}

def generate_random_id(length=8):
    """Generate a random alphanumeric string of specified length."""
    import random, string
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

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
                    context=(forwarded_msg.chat_id, forwarded_msg.message_id)
                )
                update.message.reply_text("Here is your file. (This message will be auto-deleted after 15 minutes.)")
            except Exception as e:
                logging.error(f"Error forwarding message for share ID {share_id}: {e}")
                update.message.reply_text("Sorry, I couldn't retrieve the file. It might have been removed.")
        else:
            update.message.reply_text("Invalid share link. Please check the link and try again.")
    else:
        # No parameter; display a help message.
        update.message.reply_text(
            f"Welcome! To get a file, click on a shareable link (e.g., https://t.me/{BOT_USERNAME}?start=SHARE_ID).\n"
            "If you are an admin and forward a file from the private channel to me, I will generate such a link for you."
        )

def forward_from_private(update: Update, context: CallbackContext):
    """
    When the bot receives a forwarded message, check if it's from the private channel.
    If so, generate a shareable link and send it back.
    """
    if update.message.forward_from_chat and update.message.forward_from_chat.id == PRIVATE_CHANNEL_ID:
        original_message_id = update.message.forward_from_message_id
        # Generate a random share ID with 32 characters
        share_id = generate_random_id(length=32)
        share_links[share_id] = {
            "channel_id": PRIVATE_CHANNEL_ID,
            "message_id": original_message_id
        }
        # Create a deep link URL
        share_url = f"https://t.me/{BOT_USERNAME}?start={share_id}"
        update.message.reply_text(
            f"Shareable link generated:\n{share_url}\n\n"
            "Anyone clicking this link will receive the file."
        )
    else:
        update.message.reply_text("Please forward a file from the private channel to generate a shareable link.")

def delete_message_job(context: CallbackContext):
    """Job callback to delete a forwarded message."""
    chat_id, msg_id = context.job.context
    try:
        context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except Exception as e:
        logging.error(f"Error deleting message: {e}")

# -------------------- FLASK SERVER --------------------
app = Flask(__name__)

@app.route("/")
def index():
    return "Bot is alive!"

def run_flask():
    # Replit often uses port 8080 or 8000, but you can pick any free port
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
    logging.info("Bot started. Listening for commands...")

    # Start the Flask web server in a separate thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Keep the bot running until interrupted
    updater.idle()

if __name__ == "__main__":
    main()
