import random
import time
from uuid import uuid4

import pandas as pd

from core.email_status import (
    create_run,
    enqueue_emails,
    init_status_db,
    update_run,
    utc_now,
)


DEFAULT_CSV_PATH = (
    "/root/projects/alert_results/demo/"
    "sampled_100_emails.csv"
)


def run_producer(
    csv_path=DEFAULT_CSV_PATH,
    *,
    simulation_run_id=None,
    on_progress=None,
):
    init_status_db()

    run_id = simulation_run_id or uuid4().hex

    # The API creates the run before launching this function.
    # Direct execution creates its own run.
    if simulation_run_id is None:
        create_run(run_id)

    enqueued = 0
    total = None

    def progress():
        value = {
            "simulation_run_id": run_id,
            "total": total,
            "enqueued": enqueued,
        }

        if on_progress:
            on_progress(value)

        return value

    try:
        records = (
            pd.read_csv(csv_path)
            .fillna("")
            .to_dict(orient="records")
        )

        random.shuffle(records)
        total = len(records)

        update_run(
            run_id,
            "RUNNING",
            total=total,
        )

        progress()

        while enqueued < total:
            batch = records[
                enqueued:enqueued + random.randint(1, 5)
            ]

            for email in batch:
                email.update(
                    email_id=uuid4().hex,
                    simulation_run_id=run_id,
                    ingested_at=utc_now(),
                )

            # Writes QUEUED_SIMULATION and inserts queue items
            # in the same SQLite transaction.
            enqueue_emails(run_id, batch)

            enqueued += len(batch)
            progress()

            print(
                f"[Producer] Enqueued {enqueued}/{total}; "
                f"run={run_id}",
                flush=True,
            )

            if enqueued < total:
                time.sleep(random.uniform(0.1, 0.5))

        update_run(run_id, "INGESTION_COMPLETED")

        return progress()

    except Exception as exc:
        update_run(
            run_id,
            "FAILED",
            error=str(exc),
        )
        raise


if __name__ == "__main__":
    run_producer()