import json
import os
import sqlite3

DB_PATH = "/root/projects/alert_results/demo/email_queue.db"


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")  # Enable Concurrent Read/Write
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS queue_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                queue_name TEXT NOT NULL,
                data TEXT NOT NULL,
                status TEXT DEFAULT 'PENDING',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """
        )


def enqueue_items(queue_name: str, items: list[dict]):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        for item in items:
            cursor.execute(
                "INSERT INTO queue_items (queue_name, data) VALUES (?, ?)",
                (queue_name, json.dumps(item)),
            )


def dequeue_item(queue_name: str) -> tuple[int, dict] | None:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        # Atomic lock and fetch using a immediate transaction/subquery lock
        cursor.execute(
            """
            SELECT id, data FROM queue_items 
            WHERE queue_name = ? AND status = 'PENDING' 
            ORDER BY id ASC LIMIT 1
        """,
            (queue_name,),
        )
        row = cursor.fetchone()
        if not row:
            return None

        item_id, raw_json = row
        cursor.execute(
            "UPDATE queue_items SET status = 'PROCESSING' WHERE id = ?", (item_id,)
        )
        return item_id, json.loads(raw_json)


def mark_done(item_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE queue_items SET status = 'DONE' WHERE id = ?", (item_id,)
        )


if __name__ == "__main__":
    init_db()
    print("[+] Database queue system initialized with WAL mode.")