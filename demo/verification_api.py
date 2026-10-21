import json
import time
from flask import Response,jsonify,request,stream_with_context
from core.email_status import get_email
from core.verification.telemetry import init_verification_db,read_logs


def register_verification_routes(app):
    init_verification_db()
    @app.get('/api/emails/<email_id>/verification-logs/stream')
    def verification_logs_stream(email_id):
        if get_email(email_id) is None:
            return jsonify(error='Email not found'),404
        try:
            after=max(0,int(request.args.get('after',request.headers.get('Last-Event-ID','0'))))
        except ValueError:
            return jsonify(error='Invalid cursor'),400
        @stream_with_context
        def generate():
            cursor=after
            heartbeat=time.monotonic()
            yield 'retry: 2000\n\n'
            while True:
                rows=read_logs(email_id,cursor)
                for row in rows:
                    cursor=row['cursor']
                    yield f'id: {cursor}\nevent: verification_log\ndata: {json.dumps(row)}\n\n'
                    heartbeat=time.monotonic()
                if time.monotonic()-heartbeat>=15:
                    yield ': keep-alive\n\n'
                    heartbeat=time.monotonic()
                if not rows:
                    time.sleep(.25)
        return Response(generate(),mimetype='text/event-stream',
                        headers={'Cache-Control':'no-store','X-Accel-Buffering':'no'})
