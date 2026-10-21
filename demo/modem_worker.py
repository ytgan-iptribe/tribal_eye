import logging
import time

from core.queue_db import dequeue_item, mark_done
from core.email_status import (
    init_modem_status_db,
    update_modem_status,
)
from core.modem.workflow_modem import modem_execution
from core.modem.telemetry import start_sender


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
)


def run_modem_worker():
    init_modem_status_db()

    # Existing stdout/stderr telemetry delivery remains enabled.
    start_sender()

    logging.info(
        "Modem Worker listening on 'modem_queue'"
    )

    while True:
        try:
            item = dequeue_item("modem_queue")

        except Exception:
            logging.exception(
                "Unable to read modem queue; retrying"
            )
            time.sleep(1)
            continue

        if item is None:
            time.sleep(1)
            continue

        item_id, email = item
        email_id = email.get("email_id")

        try:
            if not email_id:
                raise ValueError(
                    "Modem queue item has no email_id"
                )

            update_modem_status(
                email_id,
                "EXECUTING_MODEM",
                "Modem task dequeued",
            )

            modem_execution(
                email_id=email_id,
                simulation_run_id=email.get(
                    "simulation_run_id"
                ),
            )

            mark_done(item_id)

        except KeyboardInterrupt:
            if email_id:
                try:
                    update_modem_status(
                        email_id,
                        "FAILED_MODEM",
                        "Modem workflow interrupted",
                        error="KeyboardInterrupt",
                    )
                except Exception:
                    logging.exception(
                        "Unable to record modem interruption"
                    )

            logging.info("Modem Worker interrupted")
            return

        except Exception as exc:
            if email_id:
                try:
                    update_modem_status(
                        email_id,
                        "FAILED_MODEM",
                        "Modem workflow failed",
                        error=str(exc),
                    )
                except Exception:
                    logging.exception(
                        "Unable to record modem failure"
                    )

            logging.exception(
                "Modem task failed: item_id=%s email_id=%s; "
                "queue item remains PROCESSING",
                item_id,
                email_id,
            )

        # Shared hardware continues to be tested serially.


if __name__ == "__main__":
    run_modem_worker()