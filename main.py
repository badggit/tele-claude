#!/usr/bin/env python3
"""
Unified entry point for Claude Code Bot.

Usage:
    python main.py run                # Run all available listeners
    python main.py telegram           # Telegram only (global mode)
    python main.py telegram --local   # Telegram local mode (CWD-anchored)
    python main.py discord            # Discord only
    python main.py sessions list      # Query task API for active sessions
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any


async def _run_dispatcher(*, enable_telegram: bool, enable_discord: bool, local_cwd: Path | None) -> None:
    from dotenv import load_dotenv
    from core.dispatcher import Dispatcher
    from task_api import start_task_api, stop_task_api

    if local_cwd is not None:
        env_file = local_cwd / ".env.telebot"
        if not local_cwd.exists():
            print(f"Error: Directory does not exist: {local_cwd}", file=sys.stderr)
            sys.exit(1)
        if env_file.exists():
            load_dotenv(env_file, override=True)
        else:
            print(f"Error: {env_file} not found", file=sys.stderr)
            print(f"Create .env.telebot in {local_cwd} with BOT_TOKEN=...", file=sys.stderr)
            sys.exit(1)
    else:
        load_dotenv()

    from config import (
        BOT_TOKEN,
        DISCORD_BOT_TOKEN,
        ALLOWED_CHATS,
        DISCORD_ALLOWED_GUILDS,
        TASK_API_HOST,
        TASK_API_PORT,
    )
    from platforms.telegram.listener import TelegramListener
    from platforms.discord.listener import DiscordListener

    dispatcher = Dispatcher()

    if enable_telegram and BOT_TOKEN:
        dispatcher.add_listener(TelegramListener(BOT_TOKEN, ALLOWED_CHATS, local_cwd=str(local_cwd) if local_cwd else None))
    if enable_discord and DISCORD_BOT_TOKEN:
        dispatcher.add_listener(DiscordListener(DISCORD_BOT_TOKEN, DISCORD_ALLOWED_GUILDS))

    if not dispatcher.sessions and not dispatcher.get_listener("telegram") and not dispatcher.get_listener("discord"):
        print("No listeners configured. Check BOT_TOKEN / DISCORD_BOT_TOKEN.", file=sys.stderr)
        sys.exit(1)

    await start_task_api(dispatcher, host=TASK_API_HOST, port=TASK_API_PORT)
    await dispatcher.start()

    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        await dispatcher.stop()
        await stop_task_api()


async def _task_api_request(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    from aiohttp import ClientSession
    from config import TASK_API_HOST, TASK_API_PORT

    url = f"http://{TASK_API_HOST}:{TASK_API_PORT}{path}"
    async with ClientSession() as session:
        async with session.request(method, url, json=payload) as resp:
            data = await resp.json()
            return resp.status, data


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Claude Code Bot - Bridge chat platforms to Claude Agent SDK"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run all available listeners")

    tg_parser = subparsers.add_parser("telegram", help="Run Telegram listener")
    tg_parser.add_argument(
        "--local", nargs="?", const=".", default=None, metavar="PATH",
        help="Local project mode: anchor to PATH (default: CWD), use .env.telebot",
    )

    subparsers.add_parser("discord", help="Run Discord listener")

    sessions_parser = subparsers.add_parser("sessions", help="Manage sessions via task API")
    sessions_sub = sessions_parser.add_subparsers(dest="action", required=True)
    sessions_sub.add_parser("list", help="List active sessions")
    get_parser = sessions_sub.add_parser("get", help="Get session details")
    get_parser.add_argument("key", help="Session key (e.g., telegram:123:456)")
    inject_parser = sessions_sub.add_parser("inject", help="Inject a prompt")
    inject_parser.add_argument("prompt", help="Prompt to inject")
    inject_parser.add_argument("--key", dest="session_key")
    inject_parser.add_argument("--platform", choices=["telegram", "discord"])
    inject_parser.add_argument("--chat-id", type=int)
    inject_parser.add_argument("--thread-id", type=int)
    inject_parser.add_argument("--channel-id", type=int)
    inject_parser.add_argument("--topic-name", type=str)

    args = parser.parse_args()

    if args.command == "run":
        asyncio.run(_run_dispatcher(enable_telegram=True, enable_discord=True, local_cwd=None))
        return

    if args.command == "telegram":
        local = Path(args.local).resolve() if args.local is not None else None
        asyncio.run(_run_dispatcher(enable_telegram=True, enable_discord=False, local_cwd=local))
        return

    if args.command == "discord":
        asyncio.run(_run_dispatcher(enable_telegram=False, enable_discord=True, local_cwd=None))
        return

    if args.command == "sessions":
        if args.action == "list":
            status, data = asyncio.run(_task_api_request("GET", "/sessions"))
            _print_json({"status": status, "data": data})
            return
        if args.action == "get":
            status, data = asyncio.run(_task_api_request("GET", f"/sessions/{args.key}"))
            _print_json({"status": status, "data": data})
            return
        if args.action == "inject":
            payload: dict[str, Any] = {"prompt": args.prompt}
            if args.session_key:
                payload["session_key"] = args.session_key
            if args.platform:
                payload["platform"] = args.platform
            if args.chat_id is not None:
                payload["chat_id"] = args.chat_id
            if args.thread_id is not None:
                payload["thread_id"] = args.thread_id
            if args.channel_id is not None:
                payload["channel_id"] = args.channel_id
            if args.topic_name:
                payload["topic_name"] = args.topic_name
            status, data = asyncio.run(_task_api_request("POST", "/inject", payload))
            _print_json({"status": status, "data": data})
            return


if __name__ == "__main__":
    main()
