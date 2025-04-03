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
from datetime import timedelta, datetime

from flask import Flask, request, render_template_string
from telegram import Update
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    Filters,
    CallbackContext
)

from pymongo import MongoClient
import requests

# --------------- Configuration ---------------
BOT_TOKEN = "7947042930:AAE14yUT642RjiiwkaM_dgoGazQdh54SkcU"  # Replace with your bot token
PRIVATE_CHANNEL_ID = -1002033692655  # Replace with your private channel ID
DELETE_AFTER_SECONDS = 15 * 60       # 15 minutes
BOT_USERNAME = "file_sharing_bot03_bot"   # Replace with your bot username

# MongoDB configuration
MONGO_URI = os.environ.get("MONGO_URI", "mongodb+srv://soseh50374:WEsff3bG5XrNcunn@cluster0.6lfh0jj.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0")
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["telegram_bot"]      # Database name
links_collection = db["share_links"]     # Collection for share links
tokens_collection = db["token_verifications"]  # Collection for token verifications

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

# --------------- Token Verification Functions ---------------
TOKEN_VALIDITY_HOURS = 12

def generate_verification_token(user_id: int) -> str:
    token = generate_random_id(32)
    expires_at = datetime.utcnow() + timedelta(hours=TOKEN_VALIDITY_HOURS)
    token_doc = {
        "user_id": user_id,
        "token": token,
        "expires_at": expires_at,
        "verified": False  # Set to True after challenge success
    }
    tokens_collection.insert_one(token_doc)
    return token

def is_token_verified(user_id: int) -> bool:
    token_doc = tokens_collection.find_one({"user_id": user_id})
    if token_doc and token_doc.get("verified") and token_doc["expires_at"] > datetime.utcnow():
        return True
    return False

# --------------- URL Shortener Integration ---------------
def get_shortened_url(long_url: str) -> str:
    """
    Call your URL shortener API to get a shortened URL.
    
    Instructions:
    1. Replace 'https://yourshortenerapi.com/api/shorten' with your URL shortener API endpoint.
    2. Replace 'YOUR_API_KEY' with your actual API key if needed.
    3. Adjust payload and response parsing based on your API's documentation.
    """
    api_url = "https://indiaearnx.com"  # <-- Set your API endpoint here
    payload = {"long_url": long_url}
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer 4ef712999679a47b42ac1f33898f1b4bd73cd50e"  # <-- Replace with your API key if required
    }
    
    try:
        response = requests.post(api_url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        # Assuming the API returns the shortened URL under the key 'short_url'
        return data.get("short_url", long_url)
    except Exception as e:
        logger.error(f"Error shortening URL: {e}")
        return long_url

# --------------- Telegram Bot Handlers ---------------
# Dictionary to keep track of user batch sessions
user_sessions = {}

def handle_single_share(update: Update, context: CallbackContext, share_id: str, info: dict):
    """Ephemeral approach for single-file share."""
    try:
        forwarded_msg = context.bot.forward_message(
            chat_id=update.effective_chat.id,
            from_chat_id=info["channel_id"],
            message_id=info["message_id"]
        )
        # Schedule message deletion
        context.job_queue.run_once(
            delete_message_job,
            DELETE_AFTER_SECONDS,
            context={"chat_id": forwarded_msg.chat_id, "message_id": forwarded_msg.message_id}
        )
        update.message.reply_text("Here is your file. (Auto-deletes in 15 minutes.)")
    except Exception as e:
        logger.error(f"Error forwarding single-file share {share_id}: {e}")
        update.message.reply_text("Sorry, couldn't retrieve the file. Possibly removed or an error occurred.")

def handle_batch_share(update: Update, context: CallbackContext, share_id: str, info: dict):
    """Ephemeral approach for batch share: forward all messages from first_msg_id to last_msg_id."""
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

    update.message.reply_text(f"Batch complete: {count} messages forwarded.\n(They auto-delete in 15 minutes.)")

def start_command(update: Update, context: CallbackContext):
    """ /start command. Checks token verification before file delivery."""
    args = context.args
    if not args:
        update.message.reply_text(
            f"Welcome! To get a file, click on a shareable link (e.g. https://t.me/{BOT_USERNAME}?start=<share_id>).\n"
            "If you're an admin and forward a file from the private channel to me, I'll generate a shareable link.\n"
            "Or use /batch for multiple files."
        )
        return

    share_id = args[0]
    info = get_share_link(share_id)
    if not info:
        update.message.reply_text("Invalid share link. Please check the link and try again.")
        return

    user_id = update.effective_user.id
    if not is_token_verified(user_id):
        # Generate token and verification URL
        token = generate_verification_token(user_id)
        verification_link = f"https://yourdomain.com/verify?token={token}&user_id={user_id}"
        short_verification_link = get_shortened_url(verification_link)
        update.message.reply_text(
            f"Before you can access the file, please complete a verification challenge.\n"
            f"Click here: {short_verification_link}"
        )
        return

    # Proceed with file delivery if token is verified
    mode = info.get("mode", "single")
    if mode == "single":
        handle_single_share(update, context, share_id, info)
    elif mode == "batch":
        handle_batch_share(update, context, share_id, info)

def batch_command(update: Update, context: CallbackContext):
    """Start a batch session for the user."""
    user_id = update.effective_user.id
    user_sessions[user_id] = {
        "mode": "batch",
        "first_msg_id": None,
        "last_msg_id": None
    }
    update.message.reply_text("Batch mode activated.\nPlease forward the first file from your private channel.")

def forward_handler(update: Update, context: CallbackContext):
    """Handle forwarded messages from the private channel for generating share links."""
    msg = update.message
    user_id = msg.from_user.id

    if msg.forward_from_chat and msg.forward_from_chat.id == PRIVATE_CHANNEL_ID:
        if user_id in user_sessions and user_sessions[user_id].get("mode") == "batch":
            session = user_sessions[user_id]
            if session["first_msg_id"] is None:
                session["first_msg_id"] = msg.forward_from_message_id
                update.message.reply_text("First file recorded. Now please forward the last file from your private channel.")
            elif session["last_msg_id"] is None:
                session["last_msg_id"] = msg.forward_from_message_id
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
                del user_sessions[user_id]
        else:
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

# --------------- Flask App for Verification ---------------
CHALLENGE_HTML = """
<html>
  <body>
    <h3>Token Verification</h3>
    <p>Solve this challenge to verify: What is 3 + 4?</p>
    <form method="POST">
      <input type="hidden" name="token" value="{{ token }}">
      <input type="hidden" name="user_id" value="{{ user_id }}">
      <input type="text" name="answer" placeholder="Your answer">
      <input type="submit" value="Submit">
    </form>
  </body>
</html>
"""

flask_app = Flask(__name__)

@flask_app.route("/")
def index():
    return "Bot is alive!"

@flask_app.route("/verify", methods=["GET", "POST"])
def verify():
    if request.method == "GET":
        token = request.args.get("token")
        user_id = request.args.get("user_id")
        return render_template_string(CHALLENGE_HTML, token=token, user_id=user_id)
    else:
        token = request.form.get("token")
        user_id = int(request.form.get("user_id"))
        answer = request.form.get("answer")
        
        if answer.strip() == "7":  # correct answer for the challenge
            tokens_collection.update_one(
                {"user_id": user_id, "token": token},
                {"$set": {"verified": True, "expires_at": datetime.utcnow() + timedelta(hours=TOKEN_VALIDITY_HOURS)}}
            )
            return "Verification successful! You can now return to Telegram and access your file."
        else:
            return "Incorrect answer. Please try again."

# --------------- Main Function ---------------
def main():
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start_command))
    dp.add_handler(CommandHandler("batch", batch_command))
    dp.add_handler(MessageHandler(Filters.forwarded, forward_handler))

    updater.start_polling()
    logger.info("Bot started. Listening for commands...")

    # Start Flask server for verification in a separate thread
    def run_flask():
        flask_app.run(host="0.0.0.0", port=8000)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    updater.idle()

if __name__ == "__main__":
    main()
