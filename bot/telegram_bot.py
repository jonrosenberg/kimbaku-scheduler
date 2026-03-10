#!/usr/bin/env python3
"""Kimbaku Scheduler — Telegram bot bridging to Claude Agent SDK."""

import asyncio
import logging
import os
import subprocess
import sys

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

TOKEN = os.environ["TELEGRAM_TOKEN"]
PROJECT_DIR = os.environ.get("PROJECT_DIR", os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

logging.basicConfig(
    format="%(asctime)s — %(name)s — %(levelname)s — %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Import Claude Agent SDK
try:
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
    from claude_agent_sdk.types import AssistantMessage, TextBlock
except ImportError:
    logger.error("claude-agent-sdk not installed. Run: pip install claude-agent-sdk")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

user_sessions: dict[int, ClaudeSDKClient] = {}

ALLOWED_TOOLS = [
    "Bash", "Read", "Glob", "Grep", "WebSearch", "WebFetch",
    "mcp__google-calendar__list-events",
    "mcp__google-calendar__create-event",
    "mcp__google-calendar__get-freebusy",
    "mcp__google-calendar__search-events",
    "mcp__google-calendar__update-event",
    "mcp__google-calendar__list-calendars",
]


def make_options() -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model="claude-haiku-4-5-20251001",
        setting_sources=["project"],
        cwd=PROJECT_DIR,
        mcp_servers=".mcp.json",
        allowed_tools=ALLOWED_TOOLS,
        permission_mode="acceptEdits",
    )


async def get_session(user_id: int) -> ClaudeSDKClient:
    if user_id not in user_sessions:
        client = ClaudeSDKClient(options=make_options())
        await client.connect()
        user_sessions[user_id] = client
    return user_sessions[user_id]


async def reset_session(user_id: int) -> None:
    if user_id in user_sessions:
        try:
            await user_sessions[user_id].disconnect()
        except Exception:
            pass
        del user_sessions[user_id]


# ---------------------------------------------------------------------------
# Chunking helper
# ---------------------------------------------------------------------------

def chunk_message(text: str, max_len: int = 4096) -> list[str]:
    """Split text into chunks at newline boundaries."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


async def send_chunks(update: Update, text: str) -> None:
    for chunk in chunk_message(text):
        await update.message.reply_text(chunk)


# ---------------------------------------------------------------------------
# Quick CLI helpers
# ---------------------------------------------------------------------------

def run_events_cli(*args: str) -> str:
    result = subprocess.run(
        [sys.executable, os.path.join(PROJECT_DIR, "scripts", "events.py"), *args],
        capture_output=True, text=True, cwd=PROJECT_DIR,
    )
    output = result.stdout or result.stderr or "(no output)"
    return output


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context) -> None:
    await update.message.reply_text(
        "Kimbaku Scheduler bot online.\n\n"
        "Commands:\n"
        "  /upcoming — list upcoming events\n"
        "  /summary  — event counts and payment totals\n"
        "  /help     — show this message\n"
        "  /reset    — start a fresh conversation\n\n"
        "Or just type naturally to talk to your scheduling assistant."
    )


async def cmd_help(update: Update, context) -> None:
    await cmd_start(update, context)


async def cmd_upcoming(update: Update, context) -> None:
    output = run_events_cli("list", "--upcoming")
    await send_chunks(update, f"```\n{output}\n```")


async def cmd_summary(update: Update, context) -> None:
    output = run_events_cli("summary")
    await send_chunks(update, f"```\n{output}\n```")


async def cmd_reset(update: Update, context) -> None:
    await reset_session(update.effective_user.id)
    await update.message.reply_text("Conversation reset. Starting fresh.")


# ---------------------------------------------------------------------------
# Message handler
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context) -> None:
    user_id = update.effective_user.id
    user_text = update.message.text

    await update.message.chat.send_action("typing")

    try:
        client = await get_session(user_id)
        response_parts: list[str] = []

        await client.query(user_text)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        response_parts.append(block.text)

        full_response = "".join(response_parts).strip()
        if not full_response:
            full_response = "(No response from assistant)"

        await send_chunks(update, full_response)

    except Exception as exc:
        logger.exception("Error handling message for user %s", user_id)
        # Reset session on error so next message gets a fresh start
        await reset_session(user_id)
        await update.message.reply_text(
            f"An error occurred: {exc}\n\nSession reset — please try again."
        )


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------

async def on_shutdown(app: Application) -> None:
    logger.info("Shutting down — disconnecting %d session(s)...", len(user_sessions))
    for client in list(user_sessions.values()):
        try:
            await client.disconnect()
        except Exception:
            pass
    user_sessions.clear()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    app = (
        Application.builder()
        .token(TOKEN)
        .post_shutdown(on_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("upcoming", cmd_upcoming))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot starting... PROJECT_DIR=%s", PROJECT_DIR)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
