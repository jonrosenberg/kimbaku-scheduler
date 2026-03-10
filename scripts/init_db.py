#!/usr/bin/env python3
"""Initialize the kimbaku-scheduler SQLite database."""

import os
import sqlite3

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "../data/events.db"))


def get_db(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: str = DB_PATH) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = get_db(path)
    with conn:
        conn.executescript("""
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT NOT NULL,
                city          TEXT,
                country       TEXT,
                timezone      TEXT DEFAULT 'UTC',
                venue         TEXT,
                url           TEXT,
                description   TEXT,
                start_date    TEXT,
                end_date      TEXT,
                start_time    TEXT,
                end_time      TEXT,
                cost_estimate TEXT,
                registration_required INTEGER DEFAULT 0,
                status        TEXT DEFAULT 'discovered'
                              CHECK(status IN ('discovered','interested','registered',
                                               'attended','skipped','cancelled')),
                notes         TEXT,
                calendar_id   TEXT,
                created_at    TEXT DEFAULT (datetime('now')),
                updated_at    TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS event_tags (
                event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                tag      TEXT NOT NULL,
                PRIMARY KEY (event_id, tag)
            );

            CREATE TABLE IF NOT EXISTS event_teachers (
                event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                teacher  TEXT NOT NULL,
                PRIMARY KEY (event_id, teacher)
            );

            CREATE TABLE IF NOT EXISTS payments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id    INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                amount      REAL NOT NULL,
                currency    TEXT DEFAULT 'USD',
                type        TEXT NOT NULL
                            CHECK(type IN ('deposit','full','partial','refund')),
                description TEXT,
                status      TEXT DEFAULT 'paid'
                            CHECK(status IN ('paid','pending','refunded')),
                paid_at     TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_events_status
                ON events(status);
            CREATE INDEX IF NOT EXISTS idx_events_start_date
                ON events(start_date);
            CREATE INDEX IF NOT EXISTS idx_events_city
                ON events(city);
            CREATE INDEX IF NOT EXISTS idx_event_teachers_teacher
                ON event_teachers(teacher);
        """)
    conn.close()
    print(f"Database initialized at: {os.path.abspath(path)}")


if __name__ == "__main__":
    init_db()
