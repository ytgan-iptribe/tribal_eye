import json
import logging
import os
import threading
import time
from uuid import uuid4

from flask import (
    Flask,
    Response,
    jsonify,
    request,
    stream_with_context,
)

from simulate_producer import (
    DEFAULT_CSV_PATH,
    run_producer,
)

from core.email_status import (
    connect,
    create_run,
    get_email,
    get_emails,
    get_run,
    init_status_db,
    update_run,
)


app = Flask(__name__)

from verification_api import register_verification_routes
register_verification_routes(app)

HOST = os.getenv(
    "SIMULATION_API_HOST",
    "127.0.0.1",
)

PORT = int(
    os.getenv("SIMULATION_API_PORT", "8001")
)

CSV_PATH = os.getenv(
    "SIMULATION_CSV_PATH",
    DEFAULT_CSV_PATH,
)

FRONTEND_ORIGIN = os.getenv(
    "SIMULATION_FRONTEND_ORIGIN",
    "http://192.168.1.85:5173",
)

lock = threading.Lock()
active_run_id = None

# init_status_db()
from core.email_status import init_downstream_status_db

init_downstream_status_db()


@app.after_request
def headers(response):
    response.headers["Cache-Control"] = "no-store"

    if (
        FRONTEND_ORIGIN
        and request.headers.get("Origin") == FRONTEND_ORIGIN
    ):
        response.headers[
            "Access-Control-Allow-Origin"
        ] = FRONTEND_ORIGIN

        response.headers[
            "Access-Control-Allow-Methods"
        ] = "GET,POST,OPTIONS"

        response.headers[
            "Access-Control-Allow-Headers"
        ] = "Content-Type"

        response.vary.add("Origin")

    return response


def execute_simulation(run_id):
    global active_run_id

    try:
        run_producer(
            CSV_PATH,
            simulation_run_id=run_id,
        )

    except Exception:
        logging.exception(
            "Simulation failed: %s",
            run_id,
        )

    finally:
        with lock:
            if active_run_id == run_id:
                active_run_id = None


@app.get("/api/health")
def health():
    return jsonify(status="OK")


@app.post("/api/simulations")
def start_simulation():
    global active_run_id

    origin = request.headers.get("Origin")

    if origin and origin not in (
        FRONTEND_ORIGIN,
        request.host_url.rstrip("/"),
    ):
        return jsonify(
            error="Frontend origin not allowed"
        ), 403

    with lock:
        if active_run_id:
            return jsonify(
                error="A producer is already running",
                simulation_run_id=active_run_id,
            ), 409

        run_id = uuid4().hex
        create_run(run_id)

        active_run_id = run_id

        try:
            threading.Thread(
                target=execute_simulation,
                args=(run_id,),
                daemon=False,
            ).start()

        except Exception as exc:
            active_run_id = None

            update_run(
                run_id,
                "FAILED",
                error=str(exc),
            )

            return jsonify(error=str(exc)), 500

    return jsonify(get_run(run_id)), 202


@app.get("/api/simulations")
def list_simulations():
    with connect() as db:
        ids = [
            row[0]
            for row in db.execute(
                """
                SELECT simulation_run_id
                FROM simulation_runs
                ORDER BY created_at DESC
                """
            )
        ]

    return jsonify(
        runs=[get_run(run_id) for run_id in ids]
    )


@app.get("/api/simulations/<run_id>")
def run_status(run_id):
    value = get_run(run_id)

    if value is None:
        return jsonify(
            error="Simulation not found"
        ), 404

    return jsonify(value)


@app.get("/api/emails/<email_id>/status")
def email_status(email_id):
    value = get_email(email_id)

    if value is None:
        return jsonify(
            error="Email not found"
        ), 404

    return jsonify(value)


def snapshot_stream(read, event):
    """Send current snapshots immediately and when they change."""

    previous = None
    heartbeat = time.monotonic()

    yield "retry: 2000\n\n"

    while True:
        snapshot = read()

        if snapshot is None:
            yield (
                "event: unavailable\n"
                'data: {"error":"Snapshot unavailable"}\n\n'
            )
            return

        if snapshot != previous:
            yield (
                f"event: {event}\n"
                f"data: {json.dumps(snapshot)}\n\n"
            )

            previous = snapshot
            heartbeat = time.monotonic()

        if time.monotonic() - heartbeat >= 15:
            yield ": keep-alive\n\n"
            heartbeat = time.monotonic()

        time.sleep(0.25)


def sse(read, event):
    return Response(
        stream_with_context(
            snapshot_stream(read, event)
        ),
        mimetype="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/emails/<email_id>/stream")
def email_stream(email_id):
    if get_email(email_id) is None:
        return jsonify(
            error="Email not found"
        ), 404

    return sse(
        lambda: get_email(email_id),
        "email_progress",
    )


@app.get("/api/simulations/<run_id>/emails/stream")
def run_emails_stream(run_id):
    if get_run(run_id) is None:
        return jsonify(
            error="Simulation not found"
        ), 404

    def read():
        if get_run(run_id) is None:
            return None

        return {
            "simulation_run_id": run_id,
            "emails": get_emails(run_id),
        }

    return sse(
        read,
        "emails_progress",
    )


@app.get("/api/simulations/<run_id>/stream")
def run_progress_stream(run_id):
    if get_run(run_id) is None:
        return jsonify(
            error="Simulation not found"
        ), 404

    return sse(
        lambda: get_run(run_id),
        "simulation_progress",
    )


# Retain your existing modem route registration if used:
from modem_api import register_modem_routes
register_modem_routes(app)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    app.run(
        host=HOST,
        port=PORT,
        threaded=True,
        debug=False,
        use_reloader=False,
    )