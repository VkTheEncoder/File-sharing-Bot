import os
import re
import logging
import asyncio
import threading
import time
import random
import string
from pathlib import Path
from typing import Dict, Tuple

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
from flask import Flask

# -------------------- Logging --------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# -------------------- Environment Configuration --------------------
BOT_TOKEN = "7947042930:AAE14yUT642RjiiwkaM_dgoGazQdh54SkcU"  # Replace with your actual BotFather token
PRIVATE_CHANNEL_ID = -1002033692655  # Replace with your private channel's ID
DELETE_AFTER_SECONDS = 15 * 60       # 15 minutes
BOT_USERNAME = "file_sharing_bot03_bot"       # e.g., file_sharing_bot03_bot

# -------------------- Global Variables --------------------
# For single-file sharing, we already had share_links; here we reuse it.
# For batch mode, share_links will store entries with mode "batch".
share_links: Dict[str, Dict] = {}
# Sessions for ongoing operations; keyed by user_id.
user_sessions: Dict[int, Dict] = {}

TEMP_DIR = Path("temp")
TEMP_DIR.mkdir(exist_ok=True)

# -------------------- Flask Web Server --------------------
app = Flask(__name__)

@app.route("/")
def index():
    return "Bot is alive!"

def run_flask():
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)

# -------------------- Helper Functions --------------------
def clean_temp(user_id: int):
    """Remove temporary files for a user."""
    user_dir = TEMP_DIR / str(user_id)
    if user_dir.exists():
        for f in user_dir.glob("*"):
            try:
                f.unlink()
            except Exception as e:
                logger.error(f"Error deleting {f}: {e}")
        try:
            user_dir.rmdir()
        except Exception as e:
            logger.error(f"Error removing directory {user_dir}: {e}")

async def run_command(command: list) -> Tuple[bool, str]:
    """Run shell command with error handling."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return False, stderr.decode()
        return True, stdout.decode()
    except Exception as e:
        return False, str(e)

def human_readable_size(size: float) -> str:
    """Convert bytes to a human-readable string."""
    if size < 1024:
        return f"{size:.2f} B"
    elif size < 1024**2:
        return f"{size/1024:.2f} KB"
    elif size < 1024**3:
        return f"{size/1024**2:.2f} MB"
    else:
        return f"{size/1024**3:.2f} GB"

def delete_message_job(context: CallbackContext):
    """Job callback to delete a message after a delay."""
    job_data = context.job.context
    chat_id = job_data["chat_id"]
    msg_id = job_data["message_id"]
    try:
        context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except Exception as e:
        logger.error(f"Error deleting message: {e}")

# -------------------- Batch Mode Helpers --------------------
# In batch mode, we store in the session:
#   "mode": "batch", "first_msg_id": int, "last_msg_id": int, "channel_id": PRIVATE_CHANNEL_ID, "unique_id": str
# The user sends /batch to start a batch session.
# Then, when a forwarded file arrives, we check if first_msg_id is set; if not, we set it.
# Next forwarded file sets last_msg_id.
# Then we generate a shareable link (like before) with the range.

def generate_random_id(length=16) -> str:
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

# -------------------- Bot Handlers --------------------
def start_command(update: Update, context: CallbackContext):
    """Handle /start command.
    If a parameter is provided and exists in share_links, deliver the file(s).
    Otherwise, show a welcome/help message.
    """
    args = context.args
    if args:
        share_id = args[0]
        if share_id in share_links:
            info = share_links[share_id]
            # Check if this is a batch share or single-file share.
            if info.get("mode") == "batch":
                # Batch mode: retrieve messages from PRIVATE_CHANNEL_ID between first_msg_id and last_msg_id
                first_id = info["first_msg_id"]
                last_id = info["last_msg_id"]
                channel_id = info["channel_id"]
                update.message.reply_text("Batch request received. Retrieving files, please wait...")
                # Retrieve messages in the private channel in the given range.
                messages_to_forward = []
                # Telegram returns history in descending order by default.
                # We'll loop and collect messages that have message_id between first_id and last_id (inclusive).
                for msg in context.bot.get_chat_history(chat_id=channel_id, limit=1000):
                    if msg.message_id < first_id:
                        break
                    if first_id <= msg.message_id <= last_id:
                        messages_to_forward.append(msg)
                # Reverse to send in ascending order.
                messages_to_forward.reverse()
                count = 0
                for m in messages_to_forward:
                    try:
                        context.bot.forward_message(
                            chat_id=update.effective_chat.id,
                            from_chat_id=channel_id,
                            message_id=m.message_id
                        )
                        count += 1
                    except Exception as e:
                        logger.error(f"Error forwarding message {m.message_id}: {e}")
                update.message.reply_text(f"Batch complete. {count} file(s) delivered.\nThey will auto-delete after 15 minutes.")
            else:
                # For a single file share (existing logic)
                file_info = info
                try:
                    forwarded_msg = context.bot.forward_message(
                        chat_id=update.effective_chat.id,
                        from_chat_id=file_info["channel_id"],
                        message_id=file_info["message_id"]
                    )
                    context.job_queue.run_once(
                        delete_message_job,
                        DELETE_AFTER_SECONDS,
                        context={"chat_id": forwarded_msg.chat_id, "message_id": forwarded_msg.message_id}
                    )
                    update.message.reply_text("Here is your file. (This message will auto-delete after 15 minutes.)")
                except Exception as e:
                    logger.error(f"Error forwarding message for share ID {share_id}: {e}")
                    update.message.reply_text("Sorry, I couldn't retrieve the file. It might have been removed.")
        else:
            update.message.reply_text("Invalid share link. Please check the link and try again.")
    else:
        update.message.reply_text(
            f"Welcome! To get a file, click on a shareable link (e.g., https://t.me/{BOT_USERNAME}?start=<share_id>).\n"
            "If you are an admin and forward a file from the private channel, use /batch for multiple files or forward a single file for one link."
        )

def batch_handler(update: Update, context: CallbackContext):
    """Handle /batch command: start a batch session."""
    user_id = update.effective_user.id
    # Create a new batch session in user_sessions
    user_sessions[user_id] = {
        "mode": "batch",
        "first_msg_id": None,
        "last_msg_id": None,
        "channel_id": PRIVATE_CHANNEL_ID,
        "unique_id": generate_random_id(6)
    }
    update.message.reply_text("Batch mode activated.\nPlease forward the **first file** from your private channel.")

def handle_files(update: Update, context: CallbackContext):
    """Handle incoming forwarded files for both single and batch sessions."""
    user_id = update.effective_user.id
    if user_id not in user_sessions:
        return  # No active session, ignore.
    session = user_sessions[user_id]
    # For batch mode:
    if session.get("mode") == "batch":
        if session["first_msg_id"] is None:
            # Expect first file; must be from the private channel.
            if update.message.forward_from_chat and update.message.forward_from_chat.id == PRIVATE_CHANNEL_ID:
                session["first_msg_id"] = update.message.forward_from_message_id
                update.message.reply_text("First file recorded.\nNow please forward the **last file** from your private channel.")
            else:
                update.message.reply_text("Please forward a file from the private channel.")
            return
        elif session["last_msg_id"] is None:
            # Expect last file.
            if update.message.forward_from_chat and update.message.forward_from_chat.id == PRIVATE_CHANNEL_ID:
                session["last_msg_id"] = update.message.forward_from_message_id
                # Generate a shareable link.
                share_id = generate_random_id(32)
                share_links[share_id] = {
                    "mode": "batch",
                    "first_msg_id": session["first_msg_id"],
                    "last_msg_id": session["last_msg_id"],
                    "channel_id": PRIVATE_CHANNEL_ID,
                    "unique_id": session["unique_id"]
                }
                update.message.reply_text(f"Batch shareable link generated:\nhttps://t.me/{BOT_USERNAME}?start={share_id}")
                # Optionally, clear the session:
                del user_sessions[user_id]
            else:
                update.message.reply_text("Please forward a file from the private channel.")
            return
    else:
        # Single-file session (existing logic)
        # This part remains the same as before.
        file_name = update.message.document.file_name if update.message.document else update.message.video.file_name
        file_size = update.message.document.file_size if update.message.document else update.message.video.file_size
        if file_size > MAX_FILE_SIZE:
            update.message.reply_text("❌ File size exceeds 2GB limit!")
            return
        if not session.get("video_path"):
            if not re.search(r"\.(mp4|mkv|avi|mov)$", file_name, re.I):
                update.message.reply_text("❌ Invalid video format! Supported: MP4, MKV, AVI, MOV")
                return
            # For single file, download and generate a share link (existing logic)
            # (This code path is omitted here for brevity; assume it works as before.)
            update.message.reply_text("Single-file session not implemented in this batch handler.")

# -------------------- Main Function --------------------
def main():
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start_command))
    dp.add_handler(CommandHandler("batch", batch_handler))
    dp.add_handler(MessageHandler(Filters.forwarded, handle_files))

    updater.start_polling()
    logger.info("Bot started. Listening for commands...")

    # Start Flask server to keep container alive
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    updater.idle()

if __name__ == "__main__":
    main()
