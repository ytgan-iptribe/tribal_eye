

"""Shared SQLite snapshots used by producer, classifier and Flask."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from core.queue_db import DB_PATH, init_db


QUEUED = "QUEUED_SIMULATION"
EXECUTING = "EXECUTING_CLASSIFICATION"
COMPLETED = "COMPLETED_CLASSIFICATION"
FAILED = "FAILED_CLASSIFICATION"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect():
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row

    try:
        with db:
            yield db
    finally:
        db.close()


def init_status_db():
    init_db()

    with connect() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS simulation_runs (
                simulation_run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                total INTEGER,
                enqueued INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                error TEXT
            )
            """
        )

        db.execute(
            """
            CREATE TABLE IF NOT EXISTS email_status (
                email_id TEXT PRIMARY KEY,
                simulation_run_id TEXT NOT NULL,
                email_json TEXT NOT NULL,
                status TEXT NOT NULL,
                is_alert TEXT,
                is_5012 TEXT,
                routed_actionable INTEGER,
                queued_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                updated_at TEXT NOT NULL,
                error TEXT,
                version INTEGER NOT NULL DEFAULT 1
            )
            """
        )

        db.execute(
            """
            CREATE INDEX IF NOT EXISTS email_status_run
            ON email_status(simulation_run_id)
            """
        )


def create_run(run_id):
    now = utc_now()

    with connect() as db:
        db.execute(
            """
            INSERT INTO simulation_runs (
                simulation_run_id,
                status,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (run_id, "QUEUED", now, now),
        )


def update_run(run_id, status, *, total=None, error=None):
    with connect() as db:
        db.execute(
            """
            UPDATE simulation_runs
            SET status=?,
                total=COALESCE(?, total),
                error=?,
                updated_at=?
            WHERE simulation_run_id=?
            """,
            (status, total, error, utc_now(), run_id),
        )


def enqueue_emails(run_id, emails):
    """Publish queue items and queued snapshots atomically."""

    with connect() as db:
        db.execute("BEGIN IMMEDIATE")

        for email in emails:
            raw = json.dumps(email)
            now = email["ingested_at"]

            db.execute(
                """
                INSERT INTO email_status (
                    email_id,
                    simulation_run_id,
                    email_json,
                    status,
                    queued_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    email["email_id"],
                    run_id,
                    raw,
                    QUEUED,
                    now,
                    now,
                ),
            )

            db.execute(
                """
                INSERT INTO queue_items(queue_name, data)
                VALUES (?, ?)
                """,
                ("email_queue", raw),
            )

        db.execute(
            """
            UPDATE simulation_runs
            SET enqueued=enqueued+?,
                updated_at=?
            WHERE simulation_run_id=?
            """,
            (len(emails), utc_now(), run_id),
        )


def update_email(
    email_id,
    status,
    *,
    is_alert=None,
    is_5012=None,
    routed_actionable=None,
    error=None,
):
    if status not in (EXECUTING, COMPLETED, FAILED):
        raise ValueError("Invalid classification status")

    now = utc_now()

    with connect() as db:
        count = db.execute(
            """
            UPDATE email_status
            SET status=?,
                is_alert=?,
                is_5012=?,
                routed_actionable=?,
                error=?,
                updated_at=?,
                version=version+1,
                started_at=CASE
                    WHEN ?=? THEN ?
                    ELSE started_at
                END,
                finished_at=CASE
                    WHEN ? IN (?, ?) THEN ?
                    ELSE NULL
                END
            WHERE email_id=?
            """,
            (
                status,
                is_alert,
                is_5012,
                routed_actionable,
                error,
                now,
                status,
                EXECUTING,
                now,
                status,
                COMPLETED,
                FAILED,
                now,
                email_id,
            ),
        ).rowcount

        if count != 1:
            raise ValueError(
                f"No queued status exists for email {email_id}"
            )


def decode(row):
    value = dict(row)
    value["email"] = json.loads(value.pop("email_json"))

    for field in (
        "routed_actionable",
        "modem_all_checks_passed",
    ):
        if value.get(field) is not None:
            value[field] = bool(value[field])

    return value


def get_email(email_id):
    with connect() as db:
        row = db.execute(
            """
            SELECT *
            FROM email_status
            WHERE email_id=?
            """,
            (email_id,),
        ).fetchone()

    return decode(row) if row else None


def get_emails(run_id):
    with connect() as db:
        rows = db.execute(
            """
            SELECT *
            FROM email_status
            WHERE simulation_run_id=?
            ORDER BY queued_at, email_id
            """,
            (run_id,),
        ).fetchall()

    return [decode(row) for row in rows]


def get_run(run_id):
    """Read ingestion progress and counters consistently."""

    with connect() as db:
        db.execute("BEGIN")

        row = db.execute(
            """
            SELECT *
            FROM simulation_runs
            WHERE simulation_run_id=?
            """,
            (run_id,),
        ).fetchone()

        if not row:
            return None

        rows = db.execute(
            """
            SELECT
                status,
                is_alert,
                is_5012,
                routed_actionable
            FROM email_status
            WHERE simulation_run_id=?
            """,
            (run_id,),
        ).fetchall()

    run = dict(row)

    run["ingestion_completed"] = (
        run["status"] == "INGESTION_COMPLETED"
    )

    run["classification_counts"] = {
        key: {"YES": 0, "NO": 0, "UNKNOWN": 0}
        for key in ("IS_ALERT", "IS_5012")
    }

    run.update(
        total_classified=0,
        total_filtered=0,
        total_actionable=0,
        total_failed=0,
    )

    for item in rows:
        run["total_classified"] += int(
            item["status"] in (COMPLETED, FAILED)
        )

        run["total_failed"] += int(
            item["status"] == FAILED
        )

        run["total_filtered"] += int(
            item["is_alert"] == "NO"
        )

        run["total_actionable"] += int(
            bool(item["routed_actionable"])
        )

        for field, key in (
            ("is_alert", "IS_ALERT"),
            ("is_5012", "IS_5012"),
        ):
            prediction = item[field]

            if prediction in ("YES", "NO", "UNKNOWN"):
                run["classification_counts"][key][prediction] += 1

    if run["ingestion_completed"]:
        run["status"] = (
            "COMPLETED"
            if run["total_classified"] == run["enqueued"]
            else "CLASSIFYING"
        )

    return run
    

def init_modem_status_db():
    """Add modem snapshot columns to the existing email_status table."""
    init_status_db()

    columns = {
        "modem_status": "TEXT",
        "modem_message": "TEXT",
        "modem_step": "TEXT",
        "modem_check_number": "INTEGER",
        "modem_phase": "TEXT",
        "modem_started_at": "TEXT",
        "modem_finished_at": "TEXT",
        "modem_updated_at": "TEXT",
        "modem_error": "TEXT",
        "modem_all_checks_passed": "INTEGER",
    }

    with connect() as db:
        db.execute("BEGIN IMMEDIATE")

        existing = {
            row["name"]
            for row in db.execute(
                "PRAGMA table_info(email_status)"
            )
        }

        for name, sql_type in columns.items():
            if name not in existing:
                # Names and types come only from the fixed mapping above.
                db.execute(
                    f'ALTER TABLE email_status '
                    f'ADD COLUMN "{name}" {sql_type}'
                )


def update_modem_status(
    email_id,
    status,
    message="",
    *,
    step=None,
    check_number=None,
    phase=None,
    error=None,
    all_checks_passed=None,
):
    now = utc_now()

    terminal = status in (
        "COMPLETED_MODEM",
        "FAILED_MODEM",
    )

    with connect() as db:
        count = db.execute(
            """
            UPDATE email_status
            SET modem_status=?,
                modem_message=?,
                modem_step=?,
                modem_check_number=?,
                modem_phase=?,
                modem_started_at=COALESCE(modem_started_at, ?),
                modem_finished_at=?,
                modem_updated_at=?,
                modem_error=?,
                modem_all_checks_passed=?,
                version=version+1
            WHERE email_id=?
            """,
            (
                status,
                message,
                step,
                check_number,
                phase,
                now,
                now if terminal else None,
                now,
                error,
                (
                    int(all_checks_passed)
                    if all_checks_passed is not None
                    else None
                ),
                email_id,
            ),
        ).rowcount

        if count != 1:
            raise ValueError(
                f"No email_status row exists for email {email_id}"
            )


def init_downstream_status_db():
    # Creates existing modem columns as well.
    init_modem_status_db()

    columns = {
        "modem_queued_at": "TEXT",
        "verification_status": "TEXT",
        "verification_queued_at": "TEXT",
        "verification_updated_at": "TEXT",
    }

    with connect() as db:
        db.execute("BEGIN IMMEDIATE")

        existing = {
            row["name"]
            for row in db.execute(
                "PRAGMA table_info(email_status)"
            )
        }

        for name, sql_type in columns.items():
            if name not in existing:
                db.execute(
                    f'ALTER TABLE email_status '
                    f'ADD COLUMN "{name}" {sql_type}'
                )


def enqueue_downstream(email):
    """Atomically enqueue both branches and publish their queued states."""
    email_id = email["email_id"]
    raw = json.dumps(email)
    now = utc_now()

    with connect() as db:
        db.execute("BEGIN IMMEDIATE")

        updated = db.execute(
            """
            UPDATE email_status
            SET modem_status='QUEUED_MODEM',
                modem_message='Waiting for modem worker',
                modem_queued_at=?,
                modem_updated_at=?,
                verification_status='QUEUED_VERIFICATION',
                verification_queued_at=?,
                verification_updated_at=?,
                version=version+1
            WHERE email_id=?
            """,
            (now, now, now, now, email_id),
        ).rowcount

        if updated != 1:
            raise ValueError(
                f"No email_status row exists for email {email_id}"
            )

        db.executemany(
            """
            INSERT INTO queue_items(queue_name, data)
            VALUES (?, ?)
            """,
            [
                ("modem_queue", raw),
                ("verification_queue", raw),
            ],
        )