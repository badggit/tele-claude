#!/usr/bin/env python3
"""
Unified entry point for Claude Code Bot.

Usage:
    python main.py telegram           # Global mode (project picker)
    python main.py telegram --local   # Local mode (CWD-anchored)
    python main.py discord            # Discord bot
"""
import argparse
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Claude Code Bot - Bridge chat platforms to Claude Agent SDK"
    )
    subparsers = parser.add_subparsers(dest="platform", required=True)

    # Telegram subcommand
    tg_parser = subparsers.add_parser("telegram", help="Run Telegram bot")
    tg_parser.add_argument(
        "--local", nargs="?", const=".", default=None, metavar="PATH",
        help="Local project mode: anchor to PATH (default: CWD), use .env.telebot"
    )

    # Discord subcommand
    subparsers.add_parser("discord", help="Run Discord bot")

    args = parser.parse_args()

    if args.platform == "telegram":
        if args.local is not None:
            _run_telegram_local(args.local)
        else:
            _run_telegram_global()
    elif args.platform == "discord":
        _run_discord()


def _run_telegram_global() -> None:
    """Run Telegram bot in global mode."""
    # Load .env from project root (standard behavior)
    from dotenv import load_dotenv
    load_dotenv()

    from platforms.telegram.runner import run_global
    run_global()


def _run_telegram_local(path: str) -> None:
    """Run Telegram bot in local mode (anchored to given path)."""
    # Resolve path (handles "." for CWD)
    local_cwd = Path(path).resolve()

    # Load .env.telebot from CWD
    from dotenv import load_dotenv
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

    from platforms.telegram.runner import run_local
    run_local(local_cwd)


def _run_discord() -> None:
    """Run Discord bot."""
    # Load .env from project root
    from dotenv import load_dotenv
    load_dotenv()

    from platforms.discord.runner import run
    run()


if __name__ == "__main__":
    main()
