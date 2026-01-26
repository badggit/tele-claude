"""
Telegram bot runners for global and local project modes.
"""
import logging
import os
import sys
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application, MessageHandler, CommandHandler,
    CallbackQueryHandler, filters
)

from logger import setup_logging


def run_global() -> None:
    """Run Telegram bot in global mode (project picker via PROJECTS_DIR).

    Uses BOT_TOKEN from environment/.env file.
    """
    # Import config after dotenv is loaded by main.py
    from config import BOT_TOKEN
    from platforms.telegram.handlers import (
        handle_new_topic, handle_callback, handle_message,
        handle_topic_created, handle_photo, handle_help
    )

    setup_logging()

    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN not found in environment")

    app = Application.builder().token(BOT_TOKEN).build()

    # Handle new forum topic created (auto-detect)
    app.add_handler(MessageHandler(
        filters.StatusUpdate.FORUM_TOPIC_CREATED & filters.ChatType.SUPERGROUP,
        handle_topic_created
    ))

    # Handle /new command in groups (manual fallback)
    app.add_handler(CommandHandler(
        "new",
        handle_new_topic,
        filters=filters.ChatType.SUPERGROUP
    ))

    # Handle /help command
    app.add_handler(CommandHandler(
        "help",
        handle_help,
        filters=filters.ChatType.SUPERGROUP
    ))

    # Handle inline keyboard button clicks
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Handle all text messages in groups
    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.SUPERGROUP,
        handle_message
    ))

    # Handle photo messages in groups
    app.add_handler(MessageHandler(
        filters.PHOTO & filters.ChatType.SUPERGROUP,
        handle_photo
    ))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


def run_local(local_cwd: Path) -> None:
    """Run Telegram bot in local mode (CWD-anchored).

    Every new topic auto-starts a session in the given directory.
    Uses BOT_TOKEN from .env.telebot in the local directory.

    Args:
        local_cwd: Absolute path to the project directory
    """
    from platforms.telegram.handlers import (
        handle_callback, handle_message, handle_photo,
        handle_help, is_authorized_chat
    )
    from session import start_session_local

    setup_logging()
    logger = logging.getLogger("tele-claude.bot_local")

    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        print("Error: BOT_TOKEN not found in .env.telebot", file=sys.stderr)
        sys.exit(1)

    if not local_cwd.exists():
        print(f"Error: Directory does not exist: {local_cwd}", file=sys.stderr)
        sys.exit(1)

    print(f"Starting bot anchored to: {local_cwd}")
    print(f"Project name: {local_cwd.name}")
    logger.info(f"Bot starting for project: {local_cwd}")

    async def handle_topic_created_local(update: Update, context) -> None:
        """Handle new topic creation - auto-start session with local folder."""
        message = update.message
        if message is None or message.forum_topic_created is None:
            return

        if not is_authorized_chat(message.chat_id):
            logger.warning(f"Unauthorized topic creation from chat {message.chat_id}")
            return

        thread_id = message.message_thread_id
        chat_id = message.chat_id

        if thread_id is None:
            return

        local_dir = context.application.bot_data.get("local_project_dir")
        local_name = context.application.bot_data.get("local_project_name")

        logger.info(f"New topic {thread_id} - starting session in {local_dir}")

        success = await start_session_local(chat_id, thread_id, local_dir, context.bot)
        if not success:
            await context.bot.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                text=f"Failed to start session in {local_name}"
            )

    app = Application.builder().token(bot_token).build()

    # Store local project path in bot_data
    app.bot_data["local_project_dir"] = str(local_cwd)
    app.bot_data["local_project_name"] = local_cwd.name

    # Handle new forum topic created - auto-start session
    app.add_handler(MessageHandler(
        filters.StatusUpdate.FORUM_TOPIC_CREATED & filters.ChatType.SUPERGROUP,
        handle_topic_created_local
    ))

    # Handle /help command
    app.add_handler(CommandHandler(
        "help",
        handle_help,
        filters=filters.ChatType.SUPERGROUP
    ))

    # Handle inline keyboard button clicks
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Handle text messages
    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.SUPERGROUP,
        handle_message
    ))

    # Handle photos
    app.add_handler(MessageHandler(
        filters.PHOTO & filters.ChatType.SUPERGROUP,
        handle_photo
    ))

    app.run_polling(allowed_updates=Update.ALL_TYPES)
