from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from config import GENERAL_TOPIC_ID, PROJECTS_DIR, AVAILABLE_MODELS, CLAUDE_MODEL
from core.types import Trigger, make_session_key
from core.dispatcher import TransportListener
from utils import ensure_image_within_limits, get_project_folders
from .client import TelegramClient
from .reply_target import TelegramReplyTarget

_log = logging.getLogger("tele-claude.telegram.listener")


class TelegramListener(TransportListener):
    """Listens for Telegram messages and converts to Triggers."""

    platform = "telegram"

    def __init__(self, bot_token: str, allowed_chats: set[int], local_cwd: Optional[str] = None) -> None:
        self._bot_token = bot_token
        self._allowed_chats = allowed_chats
        self._local_cwd = str(Path(local_cwd).resolve()) if local_cwd else None
        self._app: Optional[Application] = None
        self._on_trigger: Optional[Callable[[Trigger], Awaitable[None]]] = None
        self._thread_cwds: dict[int, str] = {}

    def resolve_cwd(self, trigger: Trigger) -> Optional[str]:
        if self._local_cwd:
            return self._local_cwd
        thread_id = trigger.reply_context.get("thread_id") or GENERAL_TOPIC_ID
        if thread_id == GENERAL_TOPIC_ID:
            return str(Path.home())
        return self._thread_cwds.get(thread_id)

    async def start(self, on_trigger: Callable[[Trigger], Awaitable[None]]) -> None:
        self._on_trigger = on_trigger
        self._app = Application.builder().token(self._bot_token).build()

        self._app.add_handler(MessageHandler(
            filters.StatusUpdate.FORUM_TOPIC_CREATED & filters.ChatType.SUPERGROUP,
            self._handle_topic_created,
        ))
        self._app.add_handler(CommandHandler(
            "new",
            self._handle_new_topic,
            filters=filters.ChatType.SUPERGROUP,
        ))
        self._app.add_handler(CommandHandler(
            "help",
            self._handle_help,
            filters=filters.ChatType.SUPERGROUP,
        ))
        self._app.add_handler(CommandHandler(
            "model",
            self._handle_model,
            filters=filters.ChatType.SUPERGROUP,
        ))
        self._app.add_handler(CallbackQueryHandler(self._handle_callback))
        self._app.add_handler(MessageHandler(
            filters.TEXT & filters.ChatType.SUPERGROUP,
            self._handle_message,
        ))
        self._app.add_handler(MessageHandler(
            filters.PHOTO & filters.ChatType.SUPERGROUP,
            self._handle_photo,
        ))

        await self._app.initialize()
        await self._app.start()
        if self._app.updater:
            await self._app.updater.start_polling()

    async def stop(self) -> None:
        if not self._app:
            return
        try:
            if self._app.updater:
                await self._app.updater.stop()
        except Exception:
            _log.exception("Failed to stop Telegram updater")
        try:
            await self._app.stop()
            await self._app.shutdown()
        except Exception:
            _log.exception("Failed to stop Telegram app")

    def create_reply_target(self, reply_context: dict[str, Any]) -> TelegramReplyTarget:
        if not self._app:
            raise RuntimeError("TelegramListener not started")
        bot = reply_context.get("bot") or self._app.bot
        chat_id = reply_context["chat_id"]
        thread_id = reply_context.get("thread_id") or GENERAL_TOPIC_ID
        client = TelegramClient(bot=bot, chat_id=chat_id, thread_id=thread_id)
        return TelegramReplyTarget(client)

    async def create_session(self, trigger: Trigger, cwd: str) -> Any:
        import session as session_module

        if not self._app:
            raise RuntimeError("TelegramListener not started")
        reply_context = trigger.reply_context
        chat_id = reply_context["chat_id"]
        thread_id = reply_context.get("thread_id") or GENERAL_TOPIC_ID
        bot = reply_context.get("bot") or self._app.bot
        display_name = "~" if cwd == str(Path.home()) else Path(cwd).name
        logs_dir = Path(cwd) / ".bot-logs" if self._local_cwd else None
        sandboxed = bool(self._local_cwd)
        success = await session_module._start_session_impl(
            chat_id=chat_id,
            thread_id=thread_id,
            cwd=cwd,
            display_name=display_name,
            bot=bot,
            logs_dir=logs_dir,
            sandboxed=sandboxed,
        )
        if not success:
            raise RuntimeError("Failed to start Telegram session")
        self._thread_cwds[thread_id] = cwd
        return session_module.sessions[thread_id]

    async def create_topic(self, chat_id: int, topic_name: str) -> int:
        if not self._app:
            raise RuntimeError("TelegramListener not started")
        topic = await self._app.bot.create_forum_topic(chat_id=chat_id, name=topic_name)
        return topic.message_thread_id

    def _is_authorized_chat(self, chat_id: Optional[int]) -> bool:
        if not self._allowed_chats:
            return True
        if chat_id is None:
            return False
        return chat_id in self._allowed_chats

    async def _handle_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        if message is None:
            return
        if not self._is_authorized_chat(message.chat_id):
            return

        from commands import get_help_message
        import session as session_module

        thread_id = message.message_thread_id or GENERAL_TOPIC_ID
        session = session_module.sessions.get(thread_id)
        contextual = session.contextual_commands if session else []
        current_model = (session.model_override if session else None) or CLAUDE_MODEL
        help_text = get_help_message(contextual, current_model)
        await context.bot.send_message(
            chat_id=message.chat_id,
            message_thread_id=message.message_thread_id,
            text=help_text,
            parse_mode="HTML",
        )

    async def _handle_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        if message is None:
            return
        if not self._is_authorized_chat(message.chat_id):
            return

        import session as session_module

        thread_id = message.message_thread_id or GENERAL_TOPIC_ID
        session = session_module.sessions.get(thread_id)
        chat_id = message.chat_id
        msg_thread_id = message.message_thread_id

        if not session:
            await context.bot.send_message(
                chat_id=chat_id,
                message_thread_id=msg_thread_id,
                text="No active session. Start a session first.",
            )
            return

        text = message.text or ""
        args = text.split(maxsplit=1)

        if len(args) < 2:
            current = session.model_override or CLAUDE_MODEL or "default"
            models_list = "\n".join(f"  <code>{m}</code>" for m in AVAILABLE_MODELS)
            await context.bot.send_message(
                chat_id=chat_id,
                message_thread_id=msg_thread_id,
                text=(
                    f"<b>Current model:</b> <code>{current}</code>\n\n"
                    f"<b>Available:</b>\n{models_list}\n\n"
                    "Usage: /model &lt;name&gt;"
                ),
                parse_mode="HTML",
            )
            return

        model_name = args[1].strip()
        if model_name not in AVAILABLE_MODELS:
            models_list = "\n".join(f"  <code>{m}</code>" for m in AVAILABLE_MODELS)
            await context.bot.send_message(
                chat_id=chat_id,
                message_thread_id=msg_thread_id,
                text=f"Unknown model: <code>{model_name}</code>\n\n<b>Available:</b>\n{models_list}",
                parse_mode="HTML",
            )
            return

        session.model_override = model_name
        await context.bot.send_message(
            chat_id=chat_id,
            message_thread_id=msg_thread_id,
            text=f"Model switched to <code>{model_name}</code> for this session.",
            parse_mode="HTML",
        )

    async def _handle_new_topic(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        if message is None:
            return
        if not self._is_authorized_chat(message.chat_id):
            return
        if message.message_thread_id not in (None, GENERAL_TOPIC_ID):
            return
        if not context.args:
            await message.reply_text("Usage: /new topic-name")
            return

        topic_name = " ".join(context.args)
        topic = await context.bot.create_forum_topic(chat_id=message.chat_id, name=topic_name)
        await message.reply_text(f"Created topic: {topic_name}")

        if self._local_cwd:
            await self._prime_topic_session(message.chat_id, topic.message_thread_id, context.bot)
            return

        await self._send_folder_picker(message.chat_id, topic.message_thread_id, context.bot)

    async def _handle_topic_created(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        if message is None or message.forum_topic_created is None:
            return
        if not self._is_authorized_chat(message.chat_id):
            return
        thread_id = message.message_thread_id
        if thread_id is None:
            return

        if self._local_cwd:
            await self._prime_topic_session(message.chat_id, thread_id, context.bot)
            return

        await self._send_folder_picker(message.chat_id, thread_id, context.bot)

    async def _send_folder_picker(self, chat_id: int, thread_id: int, bot) -> None:
        folders = get_project_folders()
        if not folders:
            await bot.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                text="No project folders found in ~/Projects",
            )
            return

        keyboard = [
            [InlineKeyboardButton(folder, callback_data=f"folder:{thread_id}:{folder}")]
            for folder in folders[:20]
        ]
        await bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text="Select a project folder to start Claude session:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _prime_topic_session(self, chat_id: int, thread_id: int, bot) -> None:
        if not self._on_trigger:
            return
        cwd = self._local_cwd or str(Path.home())
        self._thread_cwds[thread_id] = cwd
        trigger = Trigger(
            platform="telegram",
            session_key=make_session_key("telegram", chat_id=chat_id, thread_id=thread_id),
            prompt="",
            reply_context={
                "chat_id": chat_id,
                "thread_id": thread_id,
                "bot": bot,
                "cwd": cwd,
            },
            source="user",
        )
        await self._on_trigger(trigger)

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        message = query.message
        chat_id = message.chat.id if message else None
        if not self._is_authorized_chat(chat_id):
            await query.answer("Unauthorized", show_alert=True)
            return
        await query.answer()

        data = query.data
        if not data:
            return

        if data.startswith("folder:"):
            parts = data.split(":", 2)
            if len(parts) == 3:
                _, thread_id_str, folder_name = parts
                thread_id = int(thread_id_str)
                if message is None or not isinstance(message, Message):
                    return

                cwd = str(PROJECTS_DIR / folder_name)
                if not Path(cwd).exists():
                    await context.bot.send_message(
                        chat_id=message.chat_id,
                        message_thread_id=thread_id,
                        text=f"Failed to start session: folder '{folder_name}' not found",
                    )
                    return

                self._thread_cwds[thread_id] = cwd
                await message.edit_text(
                    f"Starting Claude session in <code>{folder_name}</code>...",
                    parse_mode="HTML",
                )

                if self._on_trigger:
                    trigger = Trigger(
                        platform="telegram",
                        session_key=make_session_key("telegram", chat_id=message.chat_id, thread_id=thread_id),
                        prompt="",
                        reply_context={
                            "chat_id": message.chat_id,
                            "thread_id": thread_id,
                            "bot": context.bot,
                            "cwd": cwd,
                        },
                        source="user",
                    )
                    await self._on_trigger(trigger)
            return

        if data.startswith("perm:"):
            parts = data.split(":", 3)
            if len(parts) == 4:
                _, action, request_id, tool_name = parts
                if message is None or not isinstance(message, Message):
                    return

                import session as session_module

                perm_thread_id = message.message_thread_id
                active_session = session_module.sessions.get(perm_thread_id) if perm_thread_id else None
                if active_session and active_session.logger:
                    active_session.logger.log_permission_callback(request_id, action, tool_name)

                if action == "allow":
                    success = await session_module.resolve_permission(request_id, allowed=True, always=False, tool_name=tool_name)
                    if success:
                        await message.edit_text(f"✅ Allowed <code>{tool_name}</code> (one-time)", parse_mode="HTML")
                    else:
                        await message.edit_text("⚠️ Permission request expired")
                elif action == "deny":
                    success = await session_module.resolve_permission(request_id, allowed=False, always=False, tool_name=tool_name)
                    if success:
                        await message.edit_text(f"❌ Denied <code>{tool_name}</code>", parse_mode="HTML")
                    else:
                        await message.edit_text("⚠️ Permission request expired")
                elif action == "always":
                    success = await session_module.resolve_permission(request_id, allowed=True, always=True, tool_name=tool_name)
                    if success:
                        await message.edit_text(f"✅ Always allowed <code>{tool_name}</code>", parse_mode="HTML")
                    else:
                        await message.edit_text("⚠️ Permission request expired")
            return

    async def _handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        if message is None:
            return
        if not self._is_authorized_chat(message.chat_id):
            return
        if not self._on_trigger:
            return

        thread_id = message.message_thread_id or GENERAL_TOPIC_ID
        cwd = self._thread_cwds.get(thread_id)
        if self._local_cwd:
            cwd = self._local_cwd
        elif thread_id == GENERAL_TOPIC_ID:
            cwd = str(Path.home())

        if not cwd:
            return

        trigger = Trigger(
            platform="telegram",
            session_key=make_session_key("telegram", chat_id=message.chat_id, thread_id=message.message_thread_id),
            prompt=message.text or "",
            reply_context={
                "chat_id": message.chat_id,
                "thread_id": thread_id,
                "bot": context.bot,
                "cwd": cwd,
            },
            source="user",
        )
        await self._on_trigger(trigger)

    async def _handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        if message is None or not message.photo:
            return
        if not self._is_authorized_chat(message.chat_id):
            return
        if not self._on_trigger:
            return

        thread_id = message.message_thread_id or GENERAL_TOPIC_ID
        cwd = self._thread_cwds.get(thread_id)
        if self._local_cwd:
            cwd = self._local_cwd
        elif thread_id == GENERAL_TOPIC_ID:
            cwd = str(Path.home())

        if not cwd:
            return

        photo = message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        photo_path = os.path.join(tempfile.gettempdir(), f"telegram_photo_{photo.file_unique_id}.jpg")
        await file.download_to_drive(photo_path)
        photo_path = await asyncio.to_thread(ensure_image_within_limits, photo_path)

        trigger = Trigger(
            platform="telegram",
            session_key=make_session_key("telegram", chat_id=message.chat_id, thread_id=message.message_thread_id),
            prompt=message.caption or "",
            images=[photo_path],
            reply_context={
                "chat_id": message.chat_id,
                "thread_id": thread_id,
                "bot": context.bot,
                "cwd": cwd,
            },
            source="user",
        )
        await self._on_trigger(trigger)
