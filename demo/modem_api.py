"""Separate status/log storage, shared ordered SSE cursor; register once per app."""
import json
import os
import sqlite3
import time
from flask import Response, jsonify, request, stream_with_context
from pydantic import ValidationError
from core.modem.schemas import ModemTelemetryEvent

DB = os.getenv('MODEM_EVENT_DB', '/root/projects/alert_results/demo/modem_events.db')

def connect():
    db = sqlite3.connect(DB, timeout=30)
    db.execute('PRAGMA foreign_keys=ON')
    return db

def store_event(db, event):
    existing = db.execute('SELECT id FROM modem_event_index WHERE event_id=?', (event['event_id'],)).fetchone()
    if existing:
        return existing[0], True
    cursor = db.execute('INSERT INTO modem_event_index(event_id,email_id,execution_id,kind) VALUES (?,?,?,?)',
        (event['event_id'], event['email_id'], event['execution_id'], event['kind']))
    key = cursor.lastrowid
    common = (key, event['email_id'], event['execution_id'], event.get('simulation_run_id'),
              event['timestamp'], event['sequence'], event.get('step'), event.get('check_number'),
              event.get('attempt'), json.dumps(event))
    if event['kind'] == 'log':
        db.execute('INSERT INTO modem_execution_logs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            common + (event['stream'], event['message']))
    else:
        db.execute('INSERT INTO modem_status_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            common + (event['event'], event.get('state'), event['message'], int(event.get('current_status', False))))
    return key, False

def register_modem_routes(app):
    os.makedirs(os.path.dirname(os.path.abspath(DB)), exist_ok=True)
    with connect() as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('''CREATE TABLE IF NOT EXISTS modem_event_index (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
            email_id TEXT NOT NULL, execution_id TEXT NOT NULL, kind TEXT NOT NULL)''')
        for table, additional in (
            ('modem_status_events', 'event TEXT NOT NULL, state TEXT, message TEXT NOT NULL, current_status INTEGER NOT NULL'),
            ('modem_execution_logs', 'stream TEXT NOT NULL, message TEXT NOT NULL')):
            db.execute(f'''CREATE TABLE IF NOT EXISTS {table} (
                cursor INTEGER PRIMARY KEY REFERENCES modem_event_index(id),
                email_id TEXT NOT NULL, execution_id TEXT NOT NULL, simulation_run_id TEXT,
                timestamp TEXT NOT NULL, sequence INTEGER NOT NULL, step TEXT,
                check_number INTEGER, attempt INTEGER, payload TEXT NOT NULL, {additional})''')
            db.execute(f'CREATE INDEX IF NOT EXISTS {table}_email ON {table}(email_id,execution_id,step,cursor)')
        db.execute('CREATE INDEX IF NOT EXISTS modem_event_email ON modem_event_index(email_id,id)')
        # Upgrade previous API storage once, preserving its event order and IDs.
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='modem_events'").fetchone():
            for key, raw in db.execute('SELECT id,payload FROM modem_events ORDER BY id').fetchall():
                event = json.loads(raw)
                event.setdefault('sequence', key)
                event['kind'] = 'log' if event['event'] in ('STDOUT','STDERR') else 'status'
                event['stream'] = event['event'].lower() if event['kind'] == 'log' else None
                event['current_status'] = event['kind'] == 'status' and event.get('state') is not None
                store_event(db, event)
        # Old modem_events retained as legacy backup; all new writes use the new tables.

    @app.post('/api/modem-events')
    def receive_modem_event():
        try:
            event = ModemTelemetryEvent.model_validate(request.get_json(silent=True)).model_dump(mode='json')
        except ValidationError as exc:
            return jsonify(error='Invalid modem event', details=str(exc)), 400
        with connect() as db:
            # Serialize lookup + insertion so concurrent duplicate posts are safe.
            db.execute('BEGIN IMMEDIATE')
            cursor, duplicate = store_event(db, event)
        return jsonify(accepted=True, duplicate=duplicate, cursor=cursor)

    def filters():
        after = int(request.args.get('after', 0))
        if after < 0:
            raise ValueError('after must be nonnegative')
        values = {k: request.args.get(k) for k in ('execution_id','step','check_number','attempt','stream')}
        if values['stream'] not in (None,'stdout','stderr'):
            raise ValueError('stream must be stdout or stderr')
        return after, {k:v for k,v in values.items() if v is not None}

    def page(email_id, table, after, selected=None, descending=False, limit=200):
        where, params = ['email_id=?','cursor>?'], [email_id,after]
        for key, value in (selected or {}).items():
            if key == 'stream' and table != 'modem_execution_logs':
                raise ValueError('stream is only valid for logs')
            where.append(f'{key}=?')
            params.append(value)
        order = 'DESC' if descending else 'ASC'
        with connect() as db:
            rows = db.execute(f'SELECT cursor,payload FROM {table} WHERE {" AND ".join(where)} ORDER BY cursor {order} LIMIT ?', params+[limit]).fetchall()
        return [dict(json.loads(raw), cursor=key) for key, raw in rows]

    @app.get('/api/emails/<email_id>/modem-status')
    def get_current_status(email_id):
        execution = request.args.get('execution_id')
        with connect() as db:
            if not execution:
                row = db.execute('SELECT execution_id FROM modem_event_index WHERE email_id=? ORDER BY id DESC LIMIT 1',(email_id,)).fetchone()
                execution = row[0] if row else None
        if execution is None:
            return jsonify(status=None)
        events = page(email_id,'modem_status_events',0,{'execution_id':execution,'current_status':1},descending=True,limit=1)
        return jsonify(status=events[0] if events else None)

    def history(email_id, table):
        try:
            after, selected = filters()
            events = page(email_id,table,after,selected)
        except ValueError as exc:
            return jsonify(error=str(exc)),400
        return jsonify(events=events,next_cursor=events[-1]['cursor'] if events else after)

    @app.get('/api/emails/<email_id>/modem-status/history')
    def status_history(email_id):
        return history(email_id,'modem_status_events')

    @app.get('/api/emails/<email_id>/modem-logs')
    def log_history(email_id):
        return history(email_id,'modem_execution_logs')

    def merged(email_id,after):
        # Read one snapshot, preserving a single cursor across the two tables.
        with connect() as db:
            rows = db.execute('''SELECT i.id, COALESCE(s.payload,l.payload)
                FROM modem_event_index i
                LEFT JOIN modem_status_events s ON s.cursor=i.id
                LEFT JOIN modem_execution_logs l ON l.cursor=i.id
                WHERE i.email_id=? AND i.id>? ORDER BY i.id LIMIT 200''',(email_id,after)).fetchall()
        return [dict(json.loads(raw),cursor=key) for key,raw in rows]

    @app.get('/api/emails/<email_id>/modem-events')
    def all_events(email_id):
        try:
            after,_=filters()
        except ValueError as exc:
            return jsonify(error=str(exc)),400
        events=merged(email_id,after)
        return jsonify(events=events,next_cursor=events[-1]['cursor'] if events else after)

    @app.get('/api/emails/<email_id>/modem-events/stream')
    def stream_modem_events(email_id):
        try:
            after=int(request.headers.get('Last-Event-ID') or request.args.get('after',0))
            if after<0:
                raise ValueError()
        except ValueError:
            return jsonify(error='Invalid cursor'),400
        @stream_with_context
        def generate():
            cursor=after
            heartbeat=time.monotonic()
            yield 'retry: 2000\n\n'
            while True:
                events=merged(email_id,cursor)
                for event in events:
                    cursor=event['cursor']
                    name='modem_log' if event['kind']=='log' else 'modem_status'
                    yield f'id: {cursor}\nevent: {name}\ndata: {json.dumps(event)}\n\n'
                if time.monotonic()-heartbeat>=15:
                    yield ': keepalive\n\n'
                    heartbeat=time.monotonic()
                if not events:
                    time.sleep(0.25)
        return Response(generate(),mimetype='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})
