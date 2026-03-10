#!/usr/bin/env python3
"""Ingest events from URLs or text via a local Ollama model."""

import argparse
import json
import os
import sys

import httpx
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(__file__))
from init_db import get_db

DB_PATH = os.environ.get(
    "DB_PATH",
    os.path.join(os.path.dirname(__file__), "../data/events.db"),
)

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")

SYSTEM_PROMPT = """You are an event data extractor for a shibari/kinbaku artist scheduling assistant.

Extract event information from the provided text and return ONLY a valid JSON object with these fields:
{
  "name": "string (event name)",
  "city": "string or null",
  "country": "string (ISO 3166-1 alpha-2 code) or null",
  "start_date": "YYYY-MM-DD or null",
  "end_date": "YYYY-MM-DD or null",
  "start_time": "HH:MM (24h) or null",
  "end_time": "HH:MM (24h) or null",
  "timezone": "IANA timezone string (e.g. Europe/London) or null",
  "venue": "string or null",
  "url": "string or null",
  "description": "string — preserve as much detail as possible from the source: what the event covers, who it is for, skill level requirements, what participants will learn or experience, any special notes about format or structure. Do not summarize — copy or closely paraphrase the full original description.",
  "cost_estimate": "string (e.g. '€200', 'Free', '¥45000') or null",
  "registration_required": true/false,
  "tags": ["array", "of", "strings"],
  "teachers": ["array", "of", "teacher/rigger names"],
  "confidence": 0.0-1.0,
  "notes": "any caveats or uncertainties"
}

Return ONLY the JSON object, no markdown, no explanation."""


def fetch_url(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    with httpx.Client(follow_redirects=True, timeout=30) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)[:12000]


def extract_event(text: str) -> dict:
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "stream": False,
        "format": "json",
    }
    with httpx.Client(timeout=120) as client:
        resp = client.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload)
        resp.raise_for_status()
    raw = resp.json()["message"]["content"].strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw)


def confidence_icon(conf: float) -> str:
    if conf >= 0.8:
        return "🟢"
    if conf >= 0.5:
        return "🟡"
    return "🔴"


def check_duplicate(conn, name: str, city: str) -> list:
    return conn.execute(
        """
        SELECT id, name, city, start_date
        FROM events
        WHERE LOWER(name) LIKE LOWER(?) AND LOWER(city) LIKE LOWER(?)
        """,
        (f"%{name[:20]}%", f"%{city}%") if city else (f"%{name[:20]}%", "%"),
    ).fetchall()


def insert_event(conn, data: dict) -> int:
    with conn:
        cur = conn.execute(
            """
            INSERT INTO events
                (name, city, country, timezone, venue, url, description,
                 start_date, end_date, start_time, end_time,
                 cost_estimate, registration_required, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                data.get("name"),
                data.get("city"),
                data.get("country"),
                data.get("timezone", "UTC"),
                data.get("venue"),
                data.get("url"),
                data.get("description"),
                data.get("start_date"),
                data.get("end_date"),
                data.get("start_time"),
                data.get("end_time"),
                data.get("cost_estimate"),
                1 if data.get("registration_required") else 0,
                "discovered",
            ),
        )
        event_id = cur.lastrowid

        for tag in data.get("tags") or []:
            conn.execute(
                "INSERT OR IGNORE INTO event_tags (event_id, tag) VALUES (?,?)",
                (event_id, tag),
            )

        for teacher in data.get("teachers") or []:
            conn.execute(
                "INSERT OR IGNORE INTO event_teachers (event_id, teacher) VALUES (?,?)",
                (event_id, teacher),
            )

    return event_id


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest an event from URL or text")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="URL to fetch and extract")
    source.add_argument("--text", help="Raw text to extract from")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    print(f"Extracting event data via Ollama ({OLLAMA_MODEL})...")

    if args.url:
        print(f"Fetching: {args.url}")
        text = fetch_url(args.url)
    else:
        text = args.text

    data = extract_event(text)

    conf = data.get("confidence", 0)
    icon = confidence_icon(conf)

    print(f"\n{icon} Confidence: {conf:.0%}")
    print(f"Name       : {data.get('name', '?')}")
    print(f"City       : {data.get('city', '?')}")
    print(f"Country    : {data.get('country', '?')}")
    print(f"Dates      : {data.get('start_date', '?')} – {data.get('end_date', '?')}")
    print(f"Time       : {data.get('start_time', '?')}")
    print(f"Timezone   : {data.get('timezone', '?')}")
    print(f"Venue      : {data.get('venue', '?')}")
    print(f"Cost       : {data.get('cost_estimate', '?')}")
    teachers = data.get("teachers") or []
    if teachers:
        print(f"Teachers   : {', '.join(teachers)}")
    tags = data.get("tags") or []
    if tags:
        print(f"Tags       : {', '.join(tags)}")
    if data.get("notes"):
        print(f"Notes      : {data['notes']}")

    conn = get_db(DB_PATH)
    duplicates = check_duplicate(conn, data.get("name", ""), data.get("city") or "")
    if duplicates:
        print(f"\n⚠️  Possible duplicates found:")
        for d in duplicates:
            print(f"  [{d['id']}] {d['name']} — {d['city']} ({d['start_date']})")

    if not args.yes:
        answer = input("\nAdd this event to the database? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            conn.close()
            return

    event_id = insert_event(conn, data)
    conn.close()
    print(f"\nAdded event ID {event_id}: {data.get('name')}")
    if teachers:
        print(f"Teachers recorded: {', '.join(teachers)}")


if __name__ == "__main__":
    main()
