"""Durable, ordered per-email delivery; HTTP runs outside SSH execution."""
import json
import logging
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
import requests
from .schemas import ModemTelemetryEvent

DB = os.getenv('MODEM_TELEMETRY_DB', '/root/projects/alert_results/demo/modem_telemetry.db')
API = os.getenv('SIMULATION_API_URL', 'http://127.0.0.1:8001').rstrip('/')
_started = False
_lock = threading.Lock()

def connection():
    os.makedirs(os.path.dirname(os.path.abspath(DB)), exist_ok=True)
    return sqlite3.connect(DB, timeout=30)

def init():
    with connection() as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE IF NOT EXISTS modem_outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)')

def flush():
    with connection() as db:
        rows = db.execute('SELECT id,payload FROM modem_outbox ORDER BY id LIMIT 100').fetchall()
    for key, raw in rows:
        event = json.loads(raw)
        event.setdefault('kind', 'log' if event['event'] in ('STDOUT','STDERR') else 'status')
        event.setdefault('stream', event['event'].lower() if event['kind'] == 'log' else None)
        event.setdefault('current_status', event['kind'] == 'status' and event.get('state') is not None)
        try:
            response = requests.post(f'{API}/api/modem-events', json=event, timeout=5)
            response.raise_for_status()
            if not response.json().get('accepted'):
                raise ValueError('Event not accepted')
        except (requests.RequestException, ValueError) as exc:
            logging.warning('Modem telemetry retained for retry: %s', exc)
            break
        with connection() as db:
            db.execute('DELETE FROM modem_outbox WHERE id=?', (key,))

def start_sender():
    global _started
    with _lock:
        if _started:
            return
        init()
        def send():
            while True:
                try:
                    flush()
                except Exception:
                    logging.exception('Modem telemetry sender error')
                threading.Event().wait(0.25)
        threading.Thread(target=send, name='modem-telemetry', daemon=True).start()
        _started = True

class Emitter:
    def __init__(self, email_id, simulation_run_id=None):
        if not email_id:
            raise ValueError('email_id is required')
        start_sender()
        self.email_id = str(email_id)
        self.run_id = simulation_run_id
        self.execution_id = uuid.uuid4().hex
        self.sequence = 0
    def emit(self, event, message='', **fields):
        self.sequence += 1
        payload = dict(event_id=uuid.uuid4().hex, email_id=self.email_id,
                       simulation_run_id=self.run_id, execution_id=self.execution_id,
                       sequence=self.sequence, timestamp=datetime.now(timezone.utc).isoformat(),
                       event=event, message=message, **fields)
        payload['kind'] = 'log' if event in ('STDOUT', 'STDERR') else 'status'
        payload['stream'] = event.lower() if payload['kind'] == 'log' else None
        payload['current_status'] = payload['kind'] == 'status' and payload.get('state') is not None
        payload = ModemTelemetryEvent.model_validate(payload).model_dump(mode='json')
        with connection() as db:
            db.execute('INSERT INTO modem_outbox(payload) VALUES (?)', (json.dumps(payload),))
        if event not in ('STDOUT', 'STDERR'):
            logging.info('[%s] %s: %s', self.email_id, event, message)
