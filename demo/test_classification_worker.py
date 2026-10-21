"""Read-only monitor for one-shot live email ingestion tests."""

import json
import logging
import sqlite3
import time

from core.queue_db import DB_PATH


POLL_SECONDS = 0.25


def connect_db():
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only = ON")
    return db


def emit(event, **data):
    print(
        json.dumps(
            {"event": event, **data},
            ensure_ascii=False,
            default=str,
        ),
        flush=True,
    )


def read_baseline():
    db = connect_db()

    try:
        # Verify both tables exist before beginning the test.
        db.execute("SELECT email_id FROM email_status LIMIT 0")

        return db.execute(
            """
            SELECT COALESCE(MAX(rowid), 0)
            FROM outlook_ingested_messages
            """
        ).fetchone()[0]

    finally:
        db.close()


def read_snapshot(after_cursor, tracked_ids):
    """Read discovery and status data from the same database snapshot."""

    db = connect_db()

    try:
        db.execute("BEGIN")

        discovered = [
            dict(row)
            for row in db.execute(
                """
                SELECT
                    rowid AS ingestion_cursor,
                    email_id,
                    source_key,
                    source_message_id,
                    ingested_at
                FROM outlook_ingested_messages
                WHERE rowid > ?
                ORDER BY rowid
                """,
                (after_cursor,),
            )
        ]

        ids = sorted(
            set(tracked_ids)
            | {row["email_id"] for row in discovered}
        )

        statuses = {}

        # Stay below SQLite parameter limits.
        for offset in range(0, len(ids), 500):
            batch = ids[offset:offset + 500]
            placeholders = ",".join("?" for _ in batch)

            rows = db.execute(
                f"""
                SELECT *
                FROM email_status
                WHERE email_id IN ({placeholders})
                """,
                batch,
            ).fetchall()

            for row in rows:
                value = dict(row)
                statuses[value["email_id"]] = value

        return discovered, statuses

    finally:
        db.rollback()
        db.close()


def status_fields(row):
    """Exclude the large email payload from repeated status messages."""

    return {
        key: value
        for key, value in row.items()
        if key != "email_json"
    }


def run_test_worker():
    cursor = read_baseline()

    tracked = {}
    previous = {}
    reported_missing = set()

    emit(
        "TEST_STARTED",
        database=DB_PATH,
        baseline_cursor=cursor,
        num_ingested=0,
        message="Now run email_worker once.",
    )

    try:
        while True:
            try:
                discovered, statuses = read_snapshot(
                    cursor,
                    tracked.keys(),
                )

                for record in discovered:
                    email_id = record["email_id"]
                    tracked[email_id] = record

                    status_row = statuses.get(email_id)
                    raw = (
                        status_row.get("email_json")
                        if status_row
                        else None
                    )

                    try:
                        email = json.loads(raw) if raw else None
                    except (TypeError, ValueError):
                        email = {"unparsed_email_json": raw}

                    emit(
                        "EMAIL_INGESTED",
                        num_ingested=len(tracked),
                        email_id=email_id,
                        source_key=record["source_key"],
                        source_message_id=record["source_message_id"],
                        ingested_at=record["ingested_at"],
                        email=email,
                    )

                    cursor = record["ingestion_cursor"]

                if discovered:
                    emit(
                        "INGESTION_SUMMARY",
                        num_ingested=len(tracked),
                        email_ids=list(tracked),
                    )

                for email_id in tracked:
                    row = statuses.get(email_id)

                    if row is None:
                        if email_id not in reported_missing:
                            emit(
                                "STATUS_ROW_MISSING",
                                email_id=email_id,
                                message=(
                                    "Ingestion record exists but "
                                    "email_status row is missing."
                                ),
                            )
                            reported_missing.add(email_id)
                        continue

                    reported_missing.discard(email_id)
                    current = status_fields(row)
                    old = previous.get(email_id)

                    if old is None:
                        emit(
                            "STATUS_INITIAL",
                            email_id=email_id,
                            data=current,
                        )

                    elif current != old:
                        changes = {
                            key: {
                                "before": old.get(key),
                                "after": current.get(key),
                            }
                            for key in old.keys() | current.keys()
                            if old.get(key) != current.get(key)
                        }

                        emit(
                            "STATUS_CHANGED",
                            email_id=email_id,
                            changes=changes,
                        )

                    previous[email_id] = current

            except sqlite3.Error:
                logging.exception(
                    "Database read failed; retrying"
                )

            time.sleep(POLL_SECONDS)

    except KeyboardInterrupt:
        emit(
            "TEST_FINISHED",
            num_ingested=len(tracked),
            emails=[
                {
                    "email_id": email_id,
                    "last_observed_status": previous.get(email_id),
                }
                for email_id in tracked
            ],
        )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
    )

    try:
        run_test_worker()
    except sqlite3.Error:
        logging.exception(
            "Cannot start test. Initialize ingestion/status tables "
            "first and verify DB_PATH."
        )
        raise SystemExit(1)