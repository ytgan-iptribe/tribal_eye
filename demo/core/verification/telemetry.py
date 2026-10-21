"""Verification status snapshots and append-only stdout/stderr in the shared DB."""
import logging
from core.email_status import connect, init_downstream_status_db, utc_now

STATES = {
    'EXECUTING_VERIFICATION', 'EXECUTING_VERIFICATION_POWERSHELL',
    'EXECUTING_VERIFICATION_LOCATE_CSV', 'EXECUTING_VERIFICATION_PARSE_CSV',
    'EXECUTING_VERIFICATION_EVALUATE_TREND', 'COMPLETE_VERIFICATION',
    'FAILED_VERIFICATION',
}

def init_verification_db():
    init_downstream_status_db()
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        existing = {row['name'] for row in db.execute('PRAGMA table_info(email_status)')}
        for name in ('verification_message','verification_started_at',
                     'verification_finished_at','verification_error','verification_workflow_id'):
            if name not in existing:
                db.execute(f'ALTER TABLE email_status ADD COLUMN "{name}" TEXT')
        db.execute('''CREATE TABLE IF NOT EXISTS verification_logs(
            cursor INTEGER PRIMARY KEY AUTOINCREMENT,email_id TEXT NOT NULL,
            simulation_run_id TEXT,workflow_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,stream TEXT NOT NULL,message TEXT NOT NULL)''')
        db.execute('CREATE INDEX IF NOT EXISTS verification_log_email ON verification_logs(email_id,cursor)')


def update_verification(email_id,status,message='',*,workflow_id=None,error=None):
    if status not in STATES:
        raise ValueError(f'Invalid verification status: {status}')
    now = utc_now()
    with connect() as db:
        count = db.execute('''UPDATE email_status SET verification_status=?,
            verification_message=?,verification_updated_at=?,verification_error=?,
            verification_workflow_id=COALESCE(?,verification_workflow_id),
            verification_started_at=COALESCE(verification_started_at,?),
            verification_finished_at=?,version=version+1 WHERE email_id=?''',
            (status,message,now,error,workflow_id,now,
             now if status in ('COMPLETE_VERIFICATION','FAILED_VERIFICATION') else None,email_id)).rowcount
        if count != 1:
            raise ValueError(f'No status row exists for email {email_id}')


def append_output(email_id,run_id,workflow_id,stream,message):
    if stream not in ('stdout','stderr','observation'):
        raise ValueError('Invalid output stream')
    with connect() as db:
        db.execute('''INSERT INTO verification_logs
            (email_id,simulation_run_id,workflow_id,timestamp,stream,message)
            VALUES(?,?,?,?,?,?)''',(email_id,run_id,workflow_id,utc_now(),stream,message))


def read_logs(email_id,after):
    with connect() as db:
        return [dict(row) for row in db.execute('''SELECT * FROM verification_logs
            WHERE email_id=? AND cursor>? ORDER BY cursor LIMIT 200''',(email_id,after))]


def bind_monitor(monitor,email):
    monitor.telemetry_email_id = email['email_id']
    monitor.telemetry_run_id = email.get('simulation_run_id')
    if getattr(monitor, '_observation_sse_bound', False):
        return
    monitor._observation_sse_bound = True

    # Include observations produced by the constructor before binding.
    history = getattr(getattr(monitor, 'observation_log', None), 'observations', [])
    for observation in list(history):
        emit_observation(monitor, observation.event, observation.message)

    original = monitor.log_observation
    def log_and_stream(event, message):
        # Retain the existing console and JSONL behavior.
        result = original(event, message)
        emit_observation(monitor, event, message)
        return result
    monitor.log_observation = log_and_stream


def emit_stage(monitor,status,message='',error=None):
    email_id = getattr(monitor,'telemetry_email_id',None)
    if email_id:
        update_verification(email_id,status,message,workflow_id=monitor.workflow_id,error=error)


def emit_output(monitor,stream,text):
    email_id = getattr(monitor,'telemetry_email_id',None)
    if email_id:
        append_output(email_id,getattr(monitor,'telemetry_run_id',None),
                      monitor.workflow_id,stream,text)


def emit_observation(monitor, event, message):
    """Mirror Python observations into the existing cursor-based log SSE."""
    email_id = getattr(monitor, 'telemetry_email_id', None)
    if not email_id:
        return
    try:
        append_output(
            email_id, getattr(monitor, 'telemetry_run_id', None),
            monitor.workflow_id, 'observation', f'{event}: {message}',
        )
    except Exception:
        # Avoid calling log_observation here, which would recurse.
        logging.exception('Unable to persist verification observation: email=%s event=%s',
                          email_id, event)
