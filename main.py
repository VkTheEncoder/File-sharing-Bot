import sys
import types
import urllib3
import http.client
import random
import string


# ---------------- Dummy Injection (Must be at the top) ----------------
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
import threading
from datetime import timedelta
from pathlib import Path
from flask import Flask
from telegram import Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext

# ---------------- Configuration ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "7947042930:AAE14yUT642RjiiwkaM_dgoGazQdh54SkcU")
PRIVATE_CHANNEL_ID = int(os.environ.get("PRIVATE_CHANNEL_ID", -1002033692655))
DELETE_AFTER_SECONDS = 15 * 60  # 15 minutes
BOT_USERNAME = os.environ.get("BOT_USERNAME", "file_sharing_bot03_bot")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------- PostgreSQL Setup ----------------
import psycopg2
from psycopg2.extras import RealDictCursor

def get_db_connection():
    DATABASE_URL = os.environ.get("DATABASE_URL")
    if not DATABASE_URL:
        raise Exception("DATABASE_URL environment variable not set")
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn

def init_db():
    """Create the share_links table if it doesn't exist."""
    conn = get_db_connection()
    with conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS share_links (
                    share_id TEXT PRIMARY KEY,
                    mode TEXT,
                    channel_id INTEGER,
                    message_id INTEGER,
                    first_msg_id INTEGER,
                    last_msg_id INTEGER
                );
            """)
    conn.close()

def add_share_link(share_id: str, info: dict):
    """Insert a new share link record into PostgreSQL."""
    conn = get_db_connection()
    with conn:
        with conn.cursor() as cur:
            if info["mode"] == "single":
                cur.execute("""
                    INSERT INTO share_links (share_id, mode, channel_id, message_id)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (share_id) DO NOTHING;
                """, (share_id, info["mode"], info["channel_id"], info["message_id"]))
            elif info["mode"] == "batch":
                cur.execute("""
                    INSERT INTO share_links (share_id, mode, first_msg_id, last_msg_id)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (share_id) DO NOTHING;
                """, (share_id, info["mode"], info["first_msg_id"], info["last_msg_id"]))
    conn.close()

def get_share_link(share_id: str):
    """Retrieve share link data from PostgreSQL."""
    conn = get_db_connection()
    with conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT share_id, mode, channel_id, message_id, first_msg_id, last_msg_id
                FROM share_links
                WHERE share_id = %s;
            """, (share_id,))
            row = cur.fetchone()
    conn.close()
    if row:
        info = {"mode": row["mode"]}
        if row["mode"] == "single":
            info["channel_id"] = row["channel_id"]
            info["message_id"] = row["message_id"]
        elif row["mode"] == "batch":
            info["first_msg_id"] = row["first_msg_id"]
            info["last_msg_id"] = row["last_msg_id"]
        return info
    return None

# ---------------- Ephemeral User Sessions (in-memory) ----------------
user_sessions = {}

# ---------------- Temporary Directory ----------------
TEMP_DIR = Path("temp")
TEMP_DIR.mkdir(exist_ok=True)

# ---------------- Utility Functions ----------------
def generate_random_id(length=16) -> str:
    """Generate a random alphanumeric string."""
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

def delete_message_job(context: CallbackContext):
    """Delete a forwarded message after DELETE_AFTER_SECONDS."""
    data = context.job.context
    chat_id = data["chat_id"]
    msg_id = data["message_id"]
    try:
        context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
    except Exception as e:
        logger.error(f"Error deleting ephemeral message: {e}")

# ---------------- Single-File Approach ----------------
def handle_single_share(update: Update, context: CallbackContext, share_id: str):
    """Forward a single file from the private channel, ephemeral style."""
    info = get_share_link(share_id)
    if not info:
        update.message.reply_text("Invalid or expired share link.")
        return

    try:
        forwarded_msg = context.bot.forward_message(
            chat_id=update.effective_chat.id,
            from_chat_id=info["channel_id"],
            message_id=info["message_id"]
        )
        context.job_queue.run_once(
            delete_message_job,
            DELETE_AFTER_SECONDS,
            context={"chat_id": forwarded_msg.chat_id, "message_id": forwarded_msg.message_id}
        )
        update.message.reply_text("Here is your file. (Auto-deletes in 15 minutes.)")
    except Exception as e:
        logger.error(f"Error forwarding single-file share {share_id}: {e}")
        update.message.reply_text("Sorry, couldn't retrieve the file. Possibly removed or an error occurred.")

# ---------------- Batch Approach ----------------
def handle_batch_share(update: Update, context: CallbackContext, share_id: str):
    """Forward a range of messages (batch) from the private channel, ephemeral style."""
    info = get_share_link(share_id)
    if not info:
        update.message.reply_text("Invalid or expired share link.")
        return

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

# ---------------- /start Command ----------------
def start_command(update: Update, context: CallbackContext):
    """
    /start [share_id]
    If share_id is provided, retrieves link info from PostgreSQL.
    Otherwise, shows a help message.
    """
    args = context.args
    if args:
        share_id = args[0]
        info = get_share_link(share_id)
        if not info:
            update.message.reply_text("Invalid share link. Please check and try again.")
            return

        mode = info.get("mode", "single")
        if mode == "single":
            handle_single_share(update, context, share_id)
        elif mode == "batch":
            handle_batch_share(update, context, share_id)
    else:
        update.message.reply_text(
            f"Welcome! To get a file, click on a shareable link (e.g. https://t.me/{BOT_USERNAME}?start=<share_id>).\n"
            "If you're an admin and forward a file from the private channel, I'll generate a link.\n"
            "Use /batch for multiple files."
        )

# ---------------- /batch Command ----------------
def batch_command(update: Update, context: CallbackContext):
    """
    /batch -> starts a batch session for the user.
    The bot asks for the first file from the private channel.
    """
    user_id = update.effective_user.id
    user_sessions[user_id] = {
        "mode": "batch",
        "first_msg_id": None,
        "last_msg_id": None
    }
    update.message.reply_text("Batch mode activated.\nPlease forward the **first file** from your private channel.")

# ---------------- Forward Handler ----------------
def forward_handler(update: Update, context: CallbackContext):
    """
    Handle forwarded files from the private channel.
    - In batch mode, records the first and last message IDs.
    - Otherwise, creates a single-file share link and saves it in PostgreSQL.
    """
    msg = update.message
    user_id = msg.from_user.id

    if msg.forward_from_chat and msg.forward_from_chat.id == PRIVATE_CHANNEL_ID:
        # Batch mode: record first then last message IDs.
        if user_id in user_sessions and user_sessions[user_id].get("mode") == "batch":
            session = user_sessions[user_id]
            if session["first_msg_id"] is None:
                session["first_msg_id"] = msg.forward_from_message_id
                update.message.reply_text("First file recorded. Now please forward the **last file**.")
            elif session["last_msg_id"] is None:
                session["last_msg_id"] = msg.forward_from_message_id
                share_id = generate_random_id(32)
                info = {
                    "mode": "batch",
                    "first_msg_id": session["first_msg_id"],
                    "last_msg_id": session["last_msg_id"]
                }
                add_share_link(share_id, info)
                update.message.reply_text(
                    f"Batch shareable link generated:\nhttps://t.me/{BOT_USERNAME}?start={share_id}\n\n"
                    "Anyone clicking this link will receive all messages in that range from your private channel."
                )
                del user_sessions[user_id]
        else:
            # Single-file approach.
            original_msg_id = msg.forward_from_message_id
            share_id = generate_random_id(32)
            info = {
                "mode": "single",
                "channel_id": PRIVATE_CHANNEL_ID,
                "message_id": original_msg_id
            }
            add_share_link(share_id, info)
            update.message.reply_text(
                f"Shareable link generated:\nhttps://t.me/{BOT_USERNAME}?start={share_id}\n\n"
                "Anyone clicking this link will receive this file. (Auto-deletes after 15 minutes.)"
            )
    else:
        update.message.reply_text("Please forward a file from the private channel to generate a shareable link.")

# ---------------- Flask Server ----------------
app = Flask(__name__)

@app.route("/")
def index():
    return "Bot is alive!"

def run_flask():
    port = int(os.environ.get("PORT", 8000))  # Railway sets PORT environment variable
    app.run(host="0.0.0.0", port=port)

# ---------------- Main ----------------
def main():
    # Initialize the PostgreSQL database and ensure the table exists.
    init_db()

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
