#!/usr/bin/env python3
"""Kimbaku Scheduler — Telegram bot with local intent routing and model selection."""

import asyncio
import logging
import os
import subprocess
import sys
import time

import httpx
from aiohttp import web as aiohttp_web
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

TOKEN = os.environ["TELEGRAM_TOKEN"]
PROJECT_DIR = os.environ.get(
    "PROJECT_DIR",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
)
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID")
GCAL_POLL_MINUTES = int(os.environ.get("GCAL_POLL_MINUTES", "15"))
GCAL_WEBHOOK_URL   = os.environ.get("GCAL_WEBHOOK_URL")
GCAL_WEBHOOK_PORT  = int(os.environ.get("GCAL_WEBHOOK_PORT", "8080"))
GCAL_WEBHOOK_TOKEN = os.environ.get("GCAL_WEBHOOK_TOKEN", "")

logging.basicConfig(
    format="%(asctime)s — %(name)s — %(levelname)s — %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

try:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    from claude_agent_sdk.types import AssistantMessage, TextBlock
except ImportError:
    logger.error("claude-agent-sdk not installed. Run: pip install claude-agent-sdk")
    sys.exit(1)

try:
    sys.path.insert(0, os.path.join(PROJECT_DIR, "scripts"))
    from gcal_poller import GCalPoller
    _OAUTH_CREDS = os.environ.get("GOOGLE_OAUTH_CREDENTIALS", "")
    _poller = GCalPoller(_OAUTH_CREDS) if _OAUTH_CREDS else None
except Exception as _exc:
    logger.warning("GCalPoller not available: %s", _exc)
    _poller = None


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

MODELS = {
    "haiku":  "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "ollama": None,
}

# Tags users can prefix messages with to force a model
MODEL_TAGS: dict[str, str] = {
    "!claude": "haiku",
    "!c":      "haiku",
    "!haiku":  "haiku",
    "!sonnet": "sonnet",
    "!s":      "sonnet",
    "!ollama": "ollama",
    "!o":      "ollama",
    "!local":  "ollama",
}

ALLOWED_TOOLS = [
    "Bash", "Read", "Glob", "Grep", "WebSearch", "WebFetch",
    "mcp__google-calendar__list-events",
    "mcp__google-calendar__create-event",
    "mcp__google-calendar__get-freebusy",
    "mcp__google-calendar__search-events",
    "mcp__google-calendar__update-event",
    "mcp__google-calendar__list-calendars",
]


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

# Keyed by (user_id, model_key) so each model gets its own conversation context
user_sessions: dict[tuple[int, str], ClaudeSDKClient] = {}

# Stores the original message while we wait for the user to pick a model
pending_queries: dict[int, str] = {}

# Simple TTL cache for cheap CLI calls
_cache: dict[str, tuple[float, str]] = {}
CACHE_TTL = 300  # seconds


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def run_events_cli(*args: str) -> str:
    result = subprocess.run(
        [sys.executable, os.path.join(PROJECT_DIR, "scripts", "events.py"), *args],
        capture_output=True, text=True, cwd=PROJECT_DIR,
    )
    return result.stdout or result.stderr or "(no output)"


def cached_cli(*args: str) -> str:
    key = " ".join(args)
    if key in _cache:
        ts, result = _cache[key]
        if time.time() - ts < CACHE_TTL:
            return result
    result = run_events_cli(*args)
    _cache[key] = (time.time(), result)
    return result


# ---------------------------------------------------------------------------
# Local intent router — no API needed
# ---------------------------------------------------------------------------

def local_intent(text: str) -> list[str] | None:
    t = text.lower().strip()
    if any(w in t for w in ["upcoming", "next event", "coming up", "what's next", "what events"]):
        return ["list", "--upcoming"]
    if any(w in t for w in ["summary", "spending", "payments", "how much", "cost"]):
        return ["summary"]
    if t in ("teachers", "teacher list") or (t.startswith("list") and "teacher" in t):
        return ["teachers"]
    if t in ("cities", "city list") or (t.startswith("list") and "cit" in t):
        return ["cities"]
    if "timezone" in t:
        return ["timezones"]
    return None


# ---------------------------------------------------------------------------
# Model tag parser
# ---------------------------------------------------------------------------

def parse_model_tag(text: str) -> tuple[str | None, str]:
    """Return (model_key, cleaned_text) if a tag is found, else (None, text)."""
    lower = text.lower()
    for tag, model_key in MODEL_TAGS.items():
        if lower.startswith(tag + " ") or lower == tag:
            return model_key, text[len(tag):].strip()
    return None, text


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def chunk_message(text: str, max_len: int = 4096) -> list[str]:
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


async def send_chunks_to_chat(chat, text: str) -> None:
    for chunk in chunk_message(text):
        await chat.send_message(chunk)


# ---------------------------------------------------------------------------
# Ollama query (no tool use — uses DB summary as context)
# ---------------------------------------------------------------------------

async def query_ollama(prompt: str) -> str:
    context = run_events_cli("summary")
    system = (
        "You are a scheduling assistant for a shibari artist. "
        "Answer questions about events, travel, and planning based on this database summary:\n\n"
        f"{context}\n\n"
        "For detailed event lists tell the user to ask for 'upcoming' or 'summary'."
    )
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload)
        resp.raise_for_status()
    return resp.json()["message"]["content"].strip()


# ---------------------------------------------------------------------------
# Claude session management
# ---------------------------------------------------------------------------

def make_options(model_key: str) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=MODELS[model_key],
        setting_sources=["project"],
        cwd=PROJECT_DIR,
        mcp_servers=".mcp.json",
        allowed_tools=ALLOWED_TOOLS,
        permission_mode="acceptEdits",
        max_turns=15,
    )


async def get_session(user_id: int, model_key: str) -> ClaudeSDKClient:
    key = (user_id, model_key)
    if key not in user_sessions:
        client = ClaudeSDKClient(options=make_options(model_key))
        await client.connect()
        user_sessions[key] = client
    return user_sessions[key]


async def reset_session(user_id: int) -> None:
    to_remove = [k for k in user_sessions if k[0] == user_id]
    for k in to_remove:
        try:
            await user_sessions[k].disconnect()
        except Exception:
            pass
        del user_sessions[k]
    pending_queries.pop(user_id, None)


async def query_claude(user_id: int, model_key: str, text: str) -> str:
    client = await get_session(user_id, model_key)
    parts: list[str] = []
    await client.query(text)
    async for msg in client.receive_response():
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    parts.append(block.text)
    return "".join(parts).strip() or "(No response)"


# ---------------------------------------------------------------------------
# Model selection keyboard
# ---------------------------------------------------------------------------

def model_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🖥 Ollama (free)", callback_data="model:ollama"),
        InlineKeyboardButton("⚡ Haiku (~$0.001)", callback_data="model:haiku"),
        InlineKeyboardButton("🧠 Sonnet (~$0.01)", callback_data="model:sonnet"),
    ]])


async def handle_model_choice(update: Update, context) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    model_key = query.data.split(":")[1]

    original_text = pending_queries.pop(user_id, None)
    if not original_text:
        await query.edit_message_text("Session expired — please resend your message.")
        return

    label = {"ollama": "Ollama", "haiku": "Haiku", "sonnet": "Sonnet"}[model_key]
    await query.edit_message_text(f"Using {label}...")

    try:
        if model_key == "ollama":
            response = await query_ollama(original_text)
        else:
            response = await query_claude(user_id, model_key, original_text)
        await send_chunks_to_chat(query.message.chat, response)
    except Exception as exc:
        logger.exception("Error in model choice for user %s", user_id)
        await query.message.reply_text(f"Error: {exc}")


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

_HELP_FILE = os.path.join(os.path.dirname(__file__), "help.md")

def load_help() -> str:
    try:
        with open(_HELP_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return "Help file not found. See bot/help.md."


async def cmd_start(update: Update, context) -> None:
    logger.info("Chat ID: %s", update.effective_chat.id)
    await send_chunks(update, load_help())


async def cmd_help(update: Update, context) -> None:
    await send_chunks(update, load_help())


async def cmd_upcoming(update: Update, context) -> None:
    output = cached_cli("list", "--upcoming")
    await send_chunks(update, f"```\n{output}\n```")


async def cmd_summary(update: Update, context) -> None:
    output = cached_cli("summary")
    await send_chunks(update, f"```\n{output}\n```")


async def cmd_reset(update: Update, context) -> None:
    await reset_session(update.effective_user.id)
    await update.message.reply_text("Conversation reset.")


async def cmd_gcal(update: Update, context) -> None:
    await update.message.chat.send_action("typing")
    try:
        prompt = (
            "List all my upcoming Google Calendar events for the next 30 days. "
            "Show name, date, time, and any location. Format as a clean list."
        )
        response = await query_claude(update.effective_user.id, "haiku", prompt)
        await send_chunks(update, response)
    except Exception as exc:
        logger.exception("Error in /gcal for user %s", update.effective_user.id)
        await update.message.reply_text(f"Calendar error: {exc}")


_aiohttp_runner = None  # set in on_startup


async def gcal_webhook_handler(request: aiohttp_web.Request) -> aiohttp_web.Response:
    """Handle incoming GCal push notifications."""
    if GCAL_WEBHOOK_TOKEN:
        if request.headers.get("X-Goog-Channel-Token") != GCAL_WEBHOOK_TOKEN:
            return aiohttp_web.Response(status=403)
    state = request.headers.get("X-Goog-Resource-State", "")
    if state != "exists":
        return aiohttp_web.Response(status=200)
    if _poller and OWNER_CHAT_ID:
        loop = asyncio.get_event_loop()
        new_events = await loop.run_in_executor(None, _poller.poll)
        bot = request.app["bot"]
        for event in new_events:
            summary = event.get("summary", "Untitled")
            start_info = event.get("start", {})
            start = start_info.get("dateTime") or start_info.get("date", "?")
            await bot.send_message(
                chat_id=OWNER_CHAT_ID,
                text=f"New calendar event: {summary}\n{start}",
            )
    return aiohttp_web.Response(status=200)


async def poll_gcal_job(context) -> None:
    """Background job: poll GCal for new events and notify OWNER_CHAT_ID."""
    if not OWNER_CHAT_ID or not _poller:
        return
    try:
        loop = asyncio.get_event_loop()
        new_events = await loop.run_in_executor(None, _poller.poll)
        for event in new_events:
            summary = event.get("summary", "Untitled")
            start_info = event.get("start", {})
            start = start_info.get("dateTime") or start_info.get("date", "?")
            await context.bot.send_message(
                chat_id=OWNER_CHAT_ID,
                text=f"New calendar event: {summary}\n{start}",
            )
    except Exception:
        logger.exception("GCal poll job error")


# ---------------------------------------------------------------------------
# Main message handler
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context) -> None:
    user_id = update.effective_user.id
    text = update.message.text

    # 1. Strip explicit model tag if present
    model_key, clean_text = parse_model_tag(text)

    # 2. Try local intent (free, no API)
    if model_key is None:
        cli_args = local_intent(clean_text)
        if cli_args:
            output = cached_cli(*cli_args)
            await send_chunks(update, f"```\n{output}\n```")
            return

    await update.message.chat.send_action("typing")

    # 3. Explicit model tag — run immediately
    if model_key is not None:
        try:
            if model_key == "ollama":
                response = await query_ollama(clean_text)
            else:
                response = await query_claude(user_id, model_key, clean_text)
            await send_chunks(update, response)
        except Exception as exc:
            logger.exception("Error for user %s", user_id)
            await reset_session(user_id)
            await update.message.reply_text(f"Error: {exc}\n\nSession reset.")
        return

    # 4. Complex query — ask which model to use
    pending_queries[user_id] = clean_text
    await update.message.reply_text(
        "This needs AI to answer. Pick a model:",
        reply_markup=model_keyboard(),
    )


async def renew_gcal_watch_job(context) -> None:
    """Renew GCal push notification channel if expiring within 48h."""
    if not _poller or not GCAL_WEBHOOK_URL:
        return
    try:
        if _poller.watch_expires_within(48 * 3600):
            loop = asyncio.get_event_loop()
            webhook_endpoint = GCAL_WEBHOOK_URL.rstrip("/") + "/webhook/gcal"
            await loop.run_in_executor(
                None, lambda: _poller.register_watch(webhook_endpoint, GCAL_WEBHOOK_TOKEN)
            )
            logger.info("GCal watch renewed")
    except Exception:
        logger.exception("Watch renewal failed")


# ---------------------------------------------------------------------------
# Startup / Shutdown
# ---------------------------------------------------------------------------

async def on_startup(app: Application) -> None:
    global _aiohttp_runner
    if GCAL_WEBHOOK_URL and _poller:
        web_app = aiohttp_web.Application()
        web_app["bot"] = app.bot
        web_app.router.add_post("/webhook/gcal", gcal_webhook_handler)
        _aiohttp_runner = aiohttp_web.AppRunner(web_app)
        await _aiohttp_runner.setup()
        site = aiohttp_web.TCPSite(_aiohttp_runner, "0.0.0.0", GCAL_WEBHOOK_PORT)
        await site.start()
        logger.info("GCal webhook server listening on port %d", GCAL_WEBHOOK_PORT)
        if _poller.watch_expires_within(0):
            webhook_endpoint = GCAL_WEBHOOK_URL.rstrip("/") + "/webhook/gcal"
            _poller.register_watch(webhook_endpoint, GCAL_WEBHOOK_TOKEN)
            logger.info("GCal watch registered → %s", webhook_endpoint)


async def on_shutdown(app: Application) -> None:
    if _poller and GCAL_WEBHOOK_URL:
        try:
            await asyncio.get_event_loop().run_in_executor(None, _poller.stop_watch)
        except Exception:
            pass
    if _aiohttp_runner:
        await _aiohttp_runner.cleanup()
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
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("upcoming", cmd_upcoming))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("gcal", cmd_gcal))
    app.add_handler(CallbackQueryHandler(handle_model_choice, pattern="^model:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    if _poller and GCAL_WEBHOOK_URL:
        app.job_queue.run_repeating(renew_gcal_watch_job, interval=24 * 3600, first=60)
        logger.info("GCal webhook mode — renewal job registered")
    elif _poller and OWNER_CHAT_ID:
        app.job_queue.run_repeating(
            poll_gcal_job,
            interval=GCAL_POLL_MINUTES * 60,
            first=30,
        )
        logger.info(
            "GCal polling mode — interval=%dm, notifying chat_id=%s",
            GCAL_POLL_MINUTES,
            OWNER_CHAT_ID,
        )
    else:
        logger.info(
            "GCal disabled (poller=%s, OWNER_CHAT_ID=%s, GCAL_WEBHOOK_URL=%s)",
            _poller is not None,
            bool(OWNER_CHAT_ID),
            bool(GCAL_WEBHOOK_URL),
        )

    logger.info("Bot starting... PROJECT_DIR=%s", PROJECT_DIR)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
