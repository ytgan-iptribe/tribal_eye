"""Clear demo queues, classification reports, and modem telemetry.

Stop producers, all workers, and the API server before running this script.
Tables are preserved. Restart the API afterward to clear its in-memory runs.
"""
import os
import sqlite3
from pathlib import Path

from core.queue_db import DB_PATH, init_db


def clear_tables(path: str, tables: tuple[str, ...], label: str) -> None:
    if not Path(path).is_file():
        print(f"[DEMO RESET] No {label} database found at {path}.")
        return

    with sqlite3.connect(path, timeout=30) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        existing = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        counts = []
        for table in tables:
            if table in existing:
                # Table names come from the fixed lists below, not user input.
                deleted = db.execute(f'DELETE FROM "{table}"').rowcount
                counts.append((table, deleted))

    for table, deleted in counts:
        print(f"[DEMO RESET] Deleted {deleted} rows from {label}.{table}.")
    if not counts:
        print(f"[DEMO RESET] No matching tables in {label} database.")


def reset_demo() -> None:
    init_db()
    with sqlite3.connect(DB_PATH, timeout=30) as db:
        deleted = db.execute(
            """
            DELETE FROM queue_items
            WHERE queue_name IN (?, ?, ?)
            """,
            ("email_queue", "modem_queue", "verification_queue"),
        ).rowcount
    print(f"[DEMO RESET] Deleted {deleted} queue items.")

    clear_tables(
        os.environ.get(
            "CLASSIFICATION_OUTBOX_PATH",
            DB_PATH + ".classification_outbox.sqlite3",
        ),
        ("reports",),
        "classification outbox",
    )

    clear_tables(
        os.environ.get(
            "MODEM_TELEMETRY_DB",
            "/root/projects/alert_results/demo/modem_telemetry.db",
        ),
        ("modem_outbox",),
        "modem telemetry outbox",
    )

    clear_tables(
        os.environ.get(
            "MODEM_EVENT_DB",
            "/root/projects/alert_results/demo/modem_events.db",
        ),
        (
            "modem_status_events",
            "modem_execution_logs",
            "modem_event_index",  # Delete children before their parent index.
            "modem_events",       # Clear legacy rows to prevent re-import.
        ),
        "modem API events",
    )
    print("[DEMO RESET] Complete. Restart the API and workers before a new simulation.")


if __name__ == "__main__":
    reset_demo()
