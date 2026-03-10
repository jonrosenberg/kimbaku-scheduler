#!/usr/bin/env python3
"""Kimbaku Scheduler — Event CLI."""

import argparse
import os
import sqlite3
import sys
from datetime import date

DB_PATH = os.environ.get(
    "DB_PATH",
    os.path.join(os.path.dirname(__file__), "../data/events.db"),
)


def get_db(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fmt_row(row: sqlite3.Row) -> str:
    r = dict(row)
    teachers_val = r.pop("teachers", None)
    lines = [f"[{r['id']}] {r['name']}"]
    if r.get("city"):
        loc = r["city"]
        if r.get("country"):
            loc += f", {r['country']}"
        lines.append(f"  Location : {loc}")
    if r.get("timezone"):
        lines.append(f"  Timezone : {r['timezone']}")
    dates = r.get("start_date", "")
    if r.get("end_date") and r["end_date"] != r.get("start_date"):
        dates += f" – {r['end_date']}"
    if dates:
        lines.append(f"  Dates    : {dates}")
    if r.get("start_time"):
        lines.append(f"  Time     : {r['start_time']}")
    lines.append(f"  Status   : {r.get('status','?')}")
    if r.get("venue"):
        lines.append(f"  Venue    : {r['venue']}")
    if r.get("cost_estimate"):
        lines.append(f"  Cost     : {r['cost_estimate']}")
    if r.get("url"):
        lines.append(f"  URL      : {r['url']}")
    if r.get("calendar_id"):
        lines.append(f"  GCal ID  : {r['calendar_id']}")
    if teachers_val:
        lines.append(f"  Teachers : {teachers_val}")
    if r.get("description"):
        # Wrap long descriptions at 80 chars
        desc = r["description"]
        prefix = "  Desc     : "
        indent = " " * len(prefix)
        words = desc.split()
        line, wrapped = [], []
        for word in words:
            if sum(len(w) + 1 for w in line) + len(word) > 80 - len(prefix if not wrapped else indent):
                wrapped.append((prefix if not wrapped else indent) + " ".join(line))
                line = [word]
            else:
                line.append(word)
        if line:
            wrapped.append((prefix if not wrapped else indent) + " ".join(line))
        lines.extend(wrapped)
    if r.get("notes"):
        lines.append(f"  Notes    : {r['notes']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Subcommand: list
# ---------------------------------------------------------------------------

def cmd_list(args: argparse.Namespace) -> None:
    conn = get_db()
    query = """
        SELECT e.*,
               GROUP_CONCAT(et.teacher, ', ') AS teachers
        FROM events e
        LEFT JOIN event_teachers et ON et.event_id = e.id
        WHERE 1=1
    """
    params: list = []

    if args.city:
        query += " AND LOWER(e.city) LIKE LOWER(?)"
        params.append(f"%{args.city}%")
    if args.country:
        query += " AND LOWER(e.country) = LOWER(?)"
        params.append(args.country)
    if args.status:
        query += " AND e.status = ?"
        params.append(args.status)
    if args.upcoming:
        today = date.today().isoformat()
        query += " AND e.start_date >= ?"
        params.append(today)
    if args.teacher:
        query += """
            AND e.id IN (
                SELECT event_id FROM event_teachers
                WHERE LOWER(teacher) LIKE LOWER(?)
            )
        """
        params.append(f"%{args.teacher}%")

    query += " GROUP BY e.id ORDER BY e.start_date ASC, e.name ASC"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    if not rows:
        print("No events found.")
        return

    for row in rows:
        print(fmt_row(row))
        print()


# ---------------------------------------------------------------------------
# Subcommand: add
# ---------------------------------------------------------------------------

def cmd_add(args: argparse.Namespace) -> None:
    conn = get_db()
    with conn:
        cur = conn.execute(
            """
            INSERT INTO events
                (name, city, country, timezone, venue, url, description,
                 start_date, end_date, start_time, end_time,
                 cost_estimate, registration_required, status, notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                args.name,
                args.city,
                args.country,
                args.timezone or "UTC",
                args.venue,
                args.url,
                args.description,
                args.start,
                args.end,
                args.start_time,
                args.end_time,
                args.cost,
                1 if args.registration_required else 0,
                args.status or "discovered",
                args.notes,
            ),
        )
        event_id = cur.lastrowid

        if args.teacher:
            for teacher in args.teacher:
                conn.execute(
                    "INSERT OR IGNORE INTO event_teachers (event_id, teacher) VALUES (?,?)",
                    (event_id, teacher.strip()),
                )

        if args.tag:
            for tag in args.tag:
                conn.execute(
                    "INSERT OR IGNORE INTO event_tags (event_id, tag) VALUES (?,?)",
                    (event_id, tag.strip()),
                )

    print(f"Added event ID {event_id}: {args.name}")
    conn.close()


# ---------------------------------------------------------------------------
# Subcommand: update
# ---------------------------------------------------------------------------

def cmd_update(args: argparse.Namespace) -> None:
    conn = get_db()

    row = conn.execute("SELECT * FROM events WHERE id = ?", (args.id,)).fetchone()
    if not row:
        print(f"Event ID {args.id} not found.")
        conn.close()
        sys.exit(1)

    fields: list[str] = []
    params: list = []

    for col in ("status", "notes", "timezone", "start_time", "end_time",
                "calendar_id", "city", "country", "start_date", "end_date",
                "venue", "url", "cost"):
        val = getattr(args, col, None)
        if val is not None:
            db_col = "cost_estimate" if col == "cost" else col
            fields.append(f"{db_col} = ?")
            params.append(val)

    if fields:
        fields.append("updated_at = datetime('now')")
        params.append(args.id)
        conn.execute(
            f"UPDATE events SET {', '.join(fields)} WHERE id = ?", params
        )

    if args.teacher is not None:
        conn.execute("DELETE FROM event_teachers WHERE event_id = ?", (args.id,))
        for teacher in args.teacher:
            conn.execute(
                "INSERT OR IGNORE INTO event_teachers (event_id, teacher) VALUES (?,?)",
                (args.id, teacher.strip()),
            )

    conn.commit()
    print(f"Updated event ID {args.id}.")
    conn.close()


# ---------------------------------------------------------------------------
# Subcommand: teachers
# ---------------------------------------------------------------------------

def cmd_teachers(args: argparse.Namespace) -> None:
    conn = get_db()
    rows = conn.execute(
        """
        SELECT teacher, COUNT(*) as event_count
        FROM event_teachers
        GROUP BY teacher
        ORDER BY event_count DESC, teacher ASC
        """
    ).fetchall()
    conn.close()

    if not rows:
        print("No teachers recorded yet.")
        return

    print(f"{'Teacher':<30} {'Events':>6}")
    print("-" * 38)
    for row in rows:
        print(f"{row['teacher']:<30} {row['event_count']:>6}")


# ---------------------------------------------------------------------------
# Subcommand: cities
# ---------------------------------------------------------------------------

def cmd_cities(args: argparse.Namespace) -> None:
    conn = get_db()
    rows = conn.execute(
        """
        SELECT city, country, timezone, COUNT(*) as event_count
        FROM events
        WHERE city IS NOT NULL
        GROUP BY city
        ORDER BY event_count DESC, city ASC
        """
    ).fetchall()
    conn.close()

    if not rows:
        print("No cities recorded yet.")
        return

    print(f"{'City':<20} {'Country':<8} {'Timezone':<25} {'Events':>6}")
    print("-" * 62)
    for row in rows:
        print(
            f"{row['city'] or '':<20} {row['country'] or '':<8} "
            f"{row['timezone'] or '':<25} {row['event_count']:>6}"
        )


# ---------------------------------------------------------------------------
# Subcommand: summary
# ---------------------------------------------------------------------------

def cmd_summary(args: argparse.Namespace) -> None:
    conn = get_db()

    status_rows = conn.execute(
        """
        SELECT status, COUNT(*) as cnt
        FROM events
        GROUP BY status
        ORDER BY cnt DESC
        """
    ).fetchall()

    payment_rows = conn.execute(
        """
        SELECT currency,
               SUM(CASE WHEN type != 'refund' THEN amount ELSE 0 END) as paid,
               SUM(CASE WHEN type = 'refund' THEN amount ELSE 0 END) as refunded,
               SUM(CASE WHEN type != 'refund' THEN amount ELSE -amount END) as net
        FROM payments
        WHERE status != 'refunded'
        GROUP BY currency
        """
    ).fetchall()

    conn.close()

    print("=== Event Status ===")
    for row in status_rows:
        print(f"  {row['status']:<15} {row['cnt']:>4}")

    if payment_rows:
        print("\n=== Payment Summary ===")
        print(f"  {'Currency':<10} {'Paid':>10} {'Refunded':>10} {'Net':>10}")
        print("  " + "-" * 42)
        for row in payment_rows:
            print(
                f"  {row['currency']:<10} {row['paid']:>10.2f} "
                f"{row['refunded']:>10.2f} {row['net']:>10.2f}"
            )
    else:
        print("\nNo payment records.")


# ---------------------------------------------------------------------------
# Subcommand: timezones
# ---------------------------------------------------------------------------

def cmd_timezones(args: argparse.Namespace) -> None:
    conn = get_db()
    rows = conn.execute(
        """
        SELECT timezone, COUNT(*) as cnt
        FROM events
        WHERE timezone IS NOT NULL
        GROUP BY timezone
        ORDER BY cnt DESC, timezone ASC
        """
    ).fetchall()
    conn.close()

    if not rows:
        print("No timezones recorded yet.")
        return

    print(f"{'Timezone':<30} {'Events':>6}")
    print("-" * 38)
    for row in rows:
        print(f"{row['timezone']:<30} {row['cnt']:>6}")


# ---------------------------------------------------------------------------
# Subcommand: payments add
# ---------------------------------------------------------------------------

def cmd_payments_add(args: argparse.Namespace) -> None:
    conn = get_db()

    row = conn.execute("SELECT name FROM events WHERE id = ?", (args.event_id,)).fetchone()
    if not row:
        print(f"Event ID {args.event_id} not found.")
        conn.close()
        sys.exit(1)

    with conn:
        cur = conn.execute(
            """
            INSERT INTO payments
                (event_id, amount, currency, type, description, status, paid_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                args.event_id,
                args.amount,
                args.currency or "USD",
                args.type,
                args.desc,
                args.status or "paid",
                args.paid_at,
            ),
        )
    print(
        f"Added {args.type} payment of {args.currency or 'USD'} {args.amount:.2f} "
        f"for event '{row['name']}' (payment ID {cur.lastrowid})."
    )
    conn.close()


# ---------------------------------------------------------------------------
# Subcommand: payments list
# ---------------------------------------------------------------------------

def cmd_payments_list(args: argparse.Namespace) -> None:
    conn = get_db()
    query = """
        SELECT p.*, e.name as event_name
        FROM payments p
        JOIN events e ON e.id = p.event_id
        WHERE 1=1
    """
    params: list = []
    if args.event_id:
        query += " AND p.event_id = ?"
        params.append(args.event_id)
    query += " ORDER BY p.created_at DESC"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    if not rows:
        print("No payments found.")
        return

    for row in rows:
        r = dict(row)
        print(
            f"[{r['id']}] {r['event_name']} | {r['type']} | "
            f"{r['currency']} {r['amount']:.2f} | {r['status']}"
        )
        if r.get("description"):
            print(f"     {r['description']}")
        print(f"     Created: {r['created_at']}")


# ---------------------------------------------------------------------------
# Subcommand: payments summary
# ---------------------------------------------------------------------------

def cmd_payments_summary(args: argparse.Namespace) -> None:
    conn = get_db()
    rows = conn.execute(
        """
        SELECT currency,
               SUM(CASE WHEN type != 'refund' THEN amount ELSE 0 END) as paid,
               SUM(CASE WHEN type = 'refund' THEN amount ELSE 0 END) as refunded,
               SUM(CASE WHEN type != 'refund' THEN amount ELSE -amount END) as net
        FROM payments
        WHERE status != 'refunded'
        GROUP BY currency
        """
    ).fetchall()
    conn.close()

    if not rows:
        print("No payments recorded.")
        return

    print(f"{'Currency':<10} {'Paid':>10} {'Refunded':>10} {'Net':>10}")
    print("-" * 42)
    for row in rows:
        print(
            f"{row['currency']:<10} {row['paid']:>10.2f} "
            f"{row['refunded']:>10.2f} {row['net']:>10.2f}"
        )


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="events.py",
        description="Kimbaku Scheduler — event database CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # -- list --
    p_list = sub.add_parser("list", help="List events")
    p_list.add_argument("--city", help="Filter by city (partial match)")
    p_list.add_argument("--country", help="Filter by country code")
    p_list.add_argument("--status", help="Filter by status")
    p_list.add_argument("--upcoming", action="store_true", help="Only future events")
    p_list.add_argument("--teacher", help="Filter by teacher name (partial match)")
    p_list.set_defaults(func=cmd_list)

    # -- add --
    p_add = sub.add_parser("add", help="Add a new event")
    p_add.add_argument("--name", required=True, help="Event name")
    p_add.add_argument("--city")
    p_add.add_argument("--country")
    p_add.add_argument("--timezone", help="IANA timezone (e.g. Europe/London)")
    p_add.add_argument("--venue")
    p_add.add_argument("--url")
    p_add.add_argument("--description")
    p_add.add_argument("--start", help="Start date YYYY-MM-DD")
    p_add.add_argument("--end", help="End date YYYY-MM-DD")
    p_add.add_argument("--start-time", dest="start_time", help="Start time HH:MM")
    p_add.add_argument("--end-time", dest="end_time", help="End time HH:MM")
    p_add.add_argument("--cost", help="Cost estimate (free text, e.g. '€200')")
    p_add.add_argument("--registration-required", action="store_true")
    p_add.add_argument("--status", default="discovered")
    p_add.add_argument("--notes")
    p_add.add_argument("--teacher", action="append", help="Teacher name (repeatable)")
    p_add.add_argument("--tag", action="append", help="Tag (repeatable)")
    p_add.set_defaults(func=cmd_add)

    # -- update --
    p_upd = sub.add_parser("update", help="Update an event")
    p_upd.add_argument("id", type=int, help="Event ID")
    p_upd.add_argument("--status")
    p_upd.add_argument("--notes")
    p_upd.add_argument("--timezone")
    p_upd.add_argument("--start-time", dest="start_time")
    p_upd.add_argument("--end-time", dest="end_time")
    p_upd.add_argument("--calendar-id", dest="calendar_id")
    p_upd.add_argument("--city")
    p_upd.add_argument("--country")
    p_upd.add_argument("--start-date", dest="start_date")
    p_upd.add_argument("--end-date", dest="end_date")
    p_upd.add_argument("--venue")
    p_upd.add_argument("--url")
    p_upd.add_argument("--cost")
    p_upd.add_argument("--teacher", action="append",
                       help="Replace all teachers (repeatable)")
    p_upd.set_defaults(func=cmd_update)

    # -- teachers --
    p_teach = sub.add_parser("teachers", help="List teachers by event count")
    p_teach.set_defaults(func=cmd_teachers)

    # -- cities --
    p_cities = sub.add_parser("cities", help="List cities with event counts")
    p_cities.set_defaults(func=cmd_cities)

    # -- summary --
    p_sum = sub.add_parser("summary", help="Status counts and payment totals")
    p_sum.set_defaults(func=cmd_summary)

    # -- timezones --
    p_tz = sub.add_parser("timezones", help="List timezones in use")
    p_tz.set_defaults(func=cmd_timezones)

    # -- payments --
    p_pay = sub.add_parser("payments", help="Payment subcommands")
    pay_sub = p_pay.add_subparsers(dest="payments_command", required=True)

    p_pay_add = pay_sub.add_parser("add", help="Record a payment")
    p_pay_add.add_argument("event_id", type=int)
    p_pay_add.add_argument("--amount", type=float, required=True)
    p_pay_add.add_argument("--currency", default="USD")
    p_pay_add.add_argument("--type", required=True,
                           choices=["deposit", "full", "partial", "refund"])
    p_pay_add.add_argument("--desc", help="Payment description")
    p_pay_add.add_argument("--status", default="paid",
                           choices=["paid", "pending", "refunded"])
    p_pay_add.add_argument("--paid-at", dest="paid_at", help="YYYY-MM-DD")
    p_pay_add.set_defaults(func=cmd_payments_add)

    p_pay_list = pay_sub.add_parser("list", help="List payments")
    p_pay_list.add_argument("event_id", type=int, nargs="?", help="Filter by event ID")
    p_pay_list.set_defaults(func=cmd_payments_list)

    p_pay_sum = pay_sub.add_parser("summary", help="Payment totals by currency")
    p_pay_sum.set_defaults(func=cmd_payments_summary)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
