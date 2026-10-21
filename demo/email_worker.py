"""Continuously ingest ProductMgmt Inbox emails into email_queue."""

import time
import hashlib
import json
import logging
import os
import signal
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import requests

from core.queue_db import DB_PATH, init_db
from fetch_latest_email import (
    GRAPH_BASE,
    EmailMessage,
    acquire_token,
    load_env,
)

from core.email_status import init_downstream_status_db

INGESTED = "INGESTED"

POLL_SECONDS = float(os.getenv("EMAIL_POLL_SECONDS", "30"))
OVERLAP_SECONDS = int(os.getenv("EMAIL_OVERLAP_SECONDS", "120"))
INITIAL_LOOKBACK_MINUTES = int(
    os.getenv("EMAIL_INITIAL_LOOKBACK_MINUTES", "5")
)

STOP = threading.Event()

SELECT_FIELDS = (
    "id,subject,from,receivedDateTime,bodyPreview,body,"
    "isRead,hasAttachments,internetMessageId"
)


def utc_now():
    return datetime.now(timezone.utc)


def graph_time(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def connect_db():
    return sqlite3.connect(DB_PATH, timeout=30)


def init_ingestion_db():
    init_downstream_status_db()

    with connect_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS outlook_ingestion_sources (
                source_key TEXT PRIMARY KEY,
                completed_until TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS outlook_ingested_messages (
                source_key TEXT NOT NULL,
                source_message_id TEXT NOT NULL,
                email_id TEXT NOT NULL UNIQUE,
                ingested_at TEXT NOT NULL,
                PRIMARY KEY (source_key, source_message_id)
            );
        """)


def get_checkpoint(source_key):
    with connect_db() as db:
        row = db.execute(
            """
            SELECT completed_until
            FROM outlook_ingestion_sources
            WHERE source_key = ?
            """,
            (source_key,),
        ).fetchone()

        if row:
            return datetime.fromisoformat(row[0])

        # Persist this boundary once, so a restart does not move it forward.
        boundary = utc_now() - timedelta(
            minutes=INITIAL_LOOKBACK_MINUTES
        )

        db.execute(
            """
            INSERT INTO outlook_ingestion_sources
                (source_key, completed_until)
            VALUES (?, ?)
            """,
            (source_key, boundary.isoformat()),
        )

        return boundary


def normalize_message(raw, source_key):
    source_message_id = raw.get("id")

    if not source_message_id:
        raise ValueError("Graph message is missing its id")

    message = EmailMessage.model_validate(raw)

    # Stable across repeated polls of this source message.
    email_id = hashlib.sha256(
        json.dumps(
            [source_key, source_message_id],
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    return {
        "email_id": email_id,
        "sender": message.sender_address,
        "timestamp": message.received_date_time,
        "subject": message.subject,
        "body": message.body_text,
        "ingested_at": utc_now().isoformat(),
        "source_type": "outlook_inbox",
        "source_id": source_key,
        "source_message_id": source_message_id,
        "internet_message_id": raw.get("internetMessageId"),
        "ingestion_mode": "LIVE",
    }


def enqueue_page(messages, source_key):
    """Atomically deduplicate, store INGESTED status, and enqueue."""

    emails = [
        normalize_message(raw, source_key)
        for raw in messages
    ]

    timestamps = []

    for email in emails:
        value = datetime.fromisoformat(
            email["timestamp"].replace("Z", "+00:00")
        )

        if value.tzinfo is None:
            raise ValueError(
                f"Email timestamp has no timezone: {email['timestamp']}"
            )

        timestamps.append(value.astimezone(timezone.utc))

    inserted = 0
    duplicates = 0

    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")

        for email in emails:
            cursor = db.execute(
                """
                INSERT INTO outlook_ingested_messages (
                    source_key,
                    source_message_id,
                    email_id,
                    ingested_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source_key, source_message_id)
                DO NOTHING
                """,
                (
                    source_key,
                    email["source_message_id"],
                    email["email_id"],
                    email["ingested_at"],
                ),
            )

            if cursor.rowcount == 0:
                duplicates += 1
                continue

            raw = json.dumps(email, ensure_ascii=False)

            # Same email_id as the queue payload.
            # Empty simulation_run_id temporarily supports the existing
            # NOT NULL schema without creating a simulation run.
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
                    "",
                    raw,
                    INGESTED,
                    email["ingested_at"],
                    email["ingested_at"],
                ),
            )

            db.execute(
                """
                INSERT INTO queue_items (
                    queue_name,
                    data,
                    status
                )
                VALUES (?, ?, 'PENDING')
                """,
                ("email_queue", raw),
            )

            inserted += 1

    return {
        "fetched": len(emails),
        "new": inserted,
        "duplicates": duplicates,
        "earliest": min(timestamps) if timestamps else None,
        "latest": max(timestamps) if timestamps else None,
    }


class GraphInboxReader:
    def __init__(self, email, password):
        self.email = email
        self.password = password
        self.token = None
        self.session = requests.Session()

    def refresh_token(self):
        self.token = acquire_token(self.email, self.password)

    def get_page(self, url, params=None):
        if not self.token:
            self.refresh_token()

        for attempt in range(6):
            if STOP.is_set():
                raise InterruptedError("Worker stopping")

            response = self.session.get(
                url,
                params=params,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Prefer": 'IdType="ImmutableId"',
                },
                timeout=(10, 60),
            )

            if response.status_code == 401 and attempt == 0:
                logging.warning("Graph token rejected; renewing")
                self.refresh_token()
                continue

            if response.status_code in (429, 500, 502, 503, 504):
                retry_after = response.headers.get("Retry-After")

                try:
                    delay = max(1, float(retry_after))
                except (TypeError, ValueError):
                    delay = min(60, 2 ** (attempt + 1))

                logging.warning(
                    "Graph HTTP %s; retrying in %.1fs",
                    response.status_code,
                    delay,
                )

                if STOP.wait(delay):
                    raise InterruptedError("Worker stopping")

                continue

            response.raise_for_status()

            payload = response.json()

            if not isinstance(payload.get("value"), list):
                raise ValueError("Graph response has no message collection")

            return payload

        raise RuntimeError("Graph request exhausted its retry attempts")

    def close(self):
        self.session.close()


def poll_inbox(reader, source_key):
    checkpoint = get_checkpoint(source_key)
    start = checkpoint - timedelta(seconds=OVERLAP_SECONDS)
    end = utc_now()

    if end <= checkpoint:
        logging.warning(
            "[INGESTION SKIPPED] Current time is not ahead of checkpoint: "
            "checkpoint=%s | now=%s",
            graph_time(checkpoint),
            graph_time(end),
        )
        return

    started = time.monotonic()

    url = f"{GRAPH_BASE}/me/mailFolders/inbox/messages"

    params = {
        "$top": 100,
        "$select": SELECT_FIELDS,
        "$orderby": "receivedDateTime asc",
        "$filter": (
            f"receivedDateTime ge {graph_time(start)} and "
            f"receivedDateTime lt {graph_time(end)}"
        ),
    }

    totals = {
        "pages": 0,
        "fetched": 0,
        "new": 0,
        "duplicates": 0,
        "earliest": None,
        "latest": None,
    }

    logging.info(
        "[FETCH START] source=%s | window=[%s, %s)",
        source_key,
        graph_time(start),
        graph_time(end),
    )

    def timestamp_label(value):
        return graph_time(value) if value else "N/A"

    try:
        while url:
            if STOP.is_set():
                raise InterruptedError("Worker stopping")

            payload = reader.get_page(url, params)
            stats = enqueue_page(payload["value"], source_key)

            totals["pages"] += 1

            for field in ("fetched", "new", "duplicates"):
                totals[field] += stats[field]

            if stats["earliest"] is not None:
                totals["earliest"] = (
                    stats["earliest"]
                    if totals["earliest"] is None
                    else min(totals["earliest"], stats["earliest"])
                )

                totals["latest"] = (
                    stats["latest"]
                    if totals["latest"] is None
                    else max(totals["latest"], stats["latest"])
                )

            logging.info(
                "[FETCH PAGE] page=%s | returned=%s | new=%s | "
                "dedup=%s | earliest=%s | latest=%s",
                totals["pages"],
                stats["fetched"],
                stats["new"],
                stats["duplicates"],
                timestamp_label(stats["earliest"]),
                timestamp_label(stats["latest"]),
            )

            url = payload.get("@odata.nextLink")
            params = None

        with connect_db() as db:
            db.execute(
                """
                UPDATE outlook_ingestion_sources
                SET completed_until = ?
                WHERE source_key = ?
                """,
                (end.isoformat(), source_key),
            )

    except Exception:
        logging.warning(
            "[FETCH INCOMPLETE] committed_pages=%s | returned=%s | "
            "new=%s | dedup=%s | earliest=%s | latest=%s | "
            "duration=%.2fs | checkpoint retained=%s",
            totals["pages"],
            totals["fetched"],
            totals["new"],
            totals["duplicates"],
            timestamp_label(totals["earliest"]),
            timestamp_label(totals["latest"]),
            time.monotonic() - started,
            graph_time(checkpoint),
        )
        raise

    logging.info(
        "[FETCH COMPLETE] pages=%s | returned=%s | new=%s | "
        "dedup=%s | earliest=%s | latest=%s | "
        "duration=%.2fs | checkpoint=%s",
        totals["pages"],
        totals["fetched"],
        totals["new"],
        totals["duplicates"],
        timestamp_label(totals["earliest"]),
        timestamp_label(totals["latest"]),
        time.monotonic() - started,
        graph_time(end),
    )


def run_email_worker():
    email, password = load_env()

    if POLL_SECONDS <= 0:
        raise ValueError("EMAIL_POLL_SECONDS must be positive")

    if OVERLAP_SECONDS < 0 or INITIAL_LOOKBACK_MINUTES < 0:
        raise ValueError("Lookback and overlap must be nonnegative")

    # Keep this stable across restarts.
    source_key = os.getenv(
        "EMAIL_SOURCE_KEY",
        f"productmgmt:{email.lower()}:inbox",
    )

    init_ingestion_db()
    reader = GraphInboxReader(email, password)

    logging.info(
        "Live email worker started: account=%s | interval=%.1fs",
        email,
        POLL_SECONDS,
    )

    try:

        poll_inbox(reader, source_key)
        
        # while not STOP.is_set():
        #     try:
        #         poll_inbox(reader, source_key)

        #     except InterruptedError:
        #         break

        #     except Exception:
        #         logging.exception(
        #             "Inbox poll failed; checkpoint retained for retry"
        #         )

        #     STOP.wait(POLL_SECONDS)

    finally:
        reader.close()
        logging.info("Email worker stopped")


def request_stop(signum, frame):
    STOP.set()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
    )

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    run_email_worker()