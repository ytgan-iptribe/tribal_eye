
from __future__ import annotations

import csv
import io
import json
import math
import os
import platform
import subprocess
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo
import ipaddress
import json
import socket
import subprocess
import shutil
import tempfile
import signal
from datetime import timezone

from urllib.parse import urlparse

from core.verification.schemas import *

from core.verification.workflow_verification import *

import logging
import threading
import time
from core.queue_db import dequeue_item,mark_done
from core.verification.telemetry import (
    init_verification_db,update_verification,bind_monitor,
)



class SimulatedOpenSearchMonitor(OpenSearchMonitor):

    SCENARIOS = {'sustained', 'escalating', 'resolving_stop', 'intermittent_stop'}

    def __init__(self, *args, scenario='sustained', seed=1, **kwargs):
        super().__init__(*args, **kwargs)
        if scenario not in self.SCENARIOS:
            raise ValueError(f'Unknown simulation scenario: {scenario}')
        baseline = self.email_context.initial_count
        if baseline is None or not math.isfinite(float(baseline)) or baseline <= 0:
            raise ValueError('Simulation requires a positive email initial_count.')
        self.scenario = scenario
        self.seed = seed
        self._simulation_started = None
        self._simulation_wall_start = None
        self._sim_counts = {}
        self.log_observation('SCENARIO: ', self.scenario)

    def _minute_count(self, bucket):
        """Full minute count; chosen once so repeated searches stay consistent."""
        if bucket in self._sim_counts:
            return self._sim_counts[bucket]
        elapsed = (bucket - self._simulation_wall_start).total_seconds() / 60
        base = float(self.email_context.initial_count) / 5
        noise = random.Random(f'{self.seed}:{bucket.isoformat()}').uniform(.97, 1.03)
        if elapsed < 0:
            factor = 1.0  # Five-minute prehistory near the email total.
        elif self.scenario == 'sustained':
            factor = 1.0
        elif self.scenario == 'escalating':
            factor = 1 + .15 * elapsed
        elif self.scenario == 'resolving_stop':
            # Declines, then stops generating events after about six minutes.
            factor = max(0.0, 1 - elapsed / 6)
        else:
            # Initial activity, silence, a short recurrence, then permanent silence.
            factor = 1.0 if elapsed < 2 else (1.3 if 5 <= elapsed < 7 else 0.0)
        count = max(0, round(base * factor * noise))
        self._sim_counts[bucket] = count
        return count

    @staticmethod
    def _upsert(path, incoming):
        rows = {}
        if path.exists():
            with path.open(newline='', encoding='utf-8-sig') as handle:
                for row in csv.DictReader(handle):
                    rows[(row['Timestamp'], row['error_code: Descending'])] = row
        for row in incoming:
            rows[(row['Timestamp'], row['error_code: Descending'])] = row
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', newline='', encoding='utf-8') as handle:
                writer = csv.DictWriter(handle, fieldnames=[
                    'Timestamp', 'error_code: Descending', 'Sum of count'
                ], quoting=csv.QUOTE_ALL)
                writer.writeheader()
                writer.writerows(rows[key] for key in sorted(rows))
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    # def execute_powershell_fetch(self):
    #     try:
    #         now = datetime.now(self.csv_zone)
    #         if self._simulation_started is None:
    #             self._simulation_started = time.monotonic()
    #             self._simulation_wall_start = now.replace(second=0, microsecond=0)
    #             self.log_observation('SIMULATION_STARTED',
    #                 f'Scenario={self.scenario}; rolling 5-minute query, 1-minute buckets. '
    #                 'Historical email count supplies synthetic scale; this is not historical replay.')
    #         start = now - timedelta(minutes=5)
    #         bucket = start.replace(second=0, microsecond=0)
    #         by_date = {}
    #         while bucket < now:
    #             end = bucket + timedelta(minutes=1)
    #             covered = (min(end, now) - max(bucket, start)).total_seconds()
    #             # Uniform event distribution within each synthetic minute.
    #             # Edge buckets are partial, as in an unaligned production query.
    #             count = round(self._minute_count(bucket) * covered / 60)
    #             if count > 0:  # Like min_doc_count=1: no fabricated zero rows.
    #                 date = bucket.strftime('%Y-%m-%d')
    #                 by_date.setdefault(date, []).append({
    #                     'Timestamp': bucket.strftime('%Y-%m-%d %H:%M:%S'),
    #                     'error_code: Descending': str(self.email_context.error_code),
    #                     'Sum of count': str(count),
    #                 })
    #             bucket = end
    #         for date, rows in by_date.items():
    #             path = Path(self.output_dir) / 'Gy - Error Code Wise' / f'Gy - Error Code Wise_{date}.csv'
    #             self._upsert(path, rows)
    #         total_rows = sum(map(len, by_date.values()))
    #         self.log_state_change('simulation_data', bool(total_rows),
    #             'SIMULATED_DATA_FOUND' if total_rows else 'SIMULATED_NO_BUCKETS',
    #             'Synthetic query returned buckets.' if total_rows else
    #             'No synthetic events in the latest five minutes; existing CSV retained. '
    #             'Absence of new rows alone does not establish resolution.')
    #         self.log_state_change('fetch', ('SIMULATED_SUCCEEDED',), 'FETCH_SUCCEEDED',
    #                               'Simulated fetch succeeded; PowerShell was not executed.')
    #         return True
    #     except Exception as exc:
    #         self.log_state_change('fetch', ('SIMULATED_FAILED', str(exc)),
    #                               'FETCH_FAILED', f'Simulation failed: {exc}')
    #         return False

    def execute_powershell_fetch(self):
        try:
            now = datetime.now(self.csv_zone)

            if self._simulation_started is None:
                self._simulation_started = time.monotonic()
                self._simulation_wall_start = now.replace(
                    second=0,
                    microsecond=0,
                )

                self.log_observation(
                    "SIMULATION_STARTED",
                    f"Scenario={self.scenario}; last five complete minutes, "
                    "1-minute buckets. Historical email count supplies "
                    "synthetic scale; this is not historical replay.",
                )

            # Exclude the current, incomplete minute.
            window_end = now.replace(second=0, microsecond=0)
            window_start = window_end - timedelta(minutes=5)

            self.log_state_change(
                "simulation_window",
                (window_start.isoformat(), window_end.isoformat()),
                "SIMULATED_QUERY_WINDOW",
                f"Complete-minute window: "
                f"{window_start.isoformat()} inclusive to "
                f"{window_end.isoformat()} exclusive.",
            )

            by_date = {}
            bucket = window_start

            while bucket < window_end:
                # Every bucket covers a complete minute.
                count = self._minute_count(bucket)

                # Preserve the existing behavior: no fabricated zero rows.
                if count > 0:
                    date = bucket.strftime("%Y-%m-%d")

                    by_date.setdefault(date, []).append({
                        "Timestamp": bucket.strftime("%Y-%m-%d %H:%M:%S"),
                        "error_code: Descending": str(
                            self.email_context.error_code
                        ),
                        "Sum of count": str(count),
                    })

                bucket += timedelta(minutes=1)

            for date, rows in by_date.items():
                path = (
                    Path(self.output_dir)
                    / "Gy - Error Code Wise"
                    / f"Gy - Error Code Wise_{date}.csv"
                )
                self._upsert(path, rows)

            total_rows = sum(len(rows) for rows in by_date.values())

            self.log_state_change(
                "simulation_data",
                bool(total_rows),
                (
                    "SIMULATED_DATA_FOUND"
                    if total_rows
                    else "SIMULATED_NO_BUCKETS"
                ),
                (
                    "Synthetic query returned complete-minute buckets."
                    if total_rows
                    else
                    "No synthetic events in the last five complete minutes; "
                    "existing CSV retained. Absence of new rows alone "
                    "does not establish resolution."
                ),
            )

            self.log_state_change(
                "fetch",
                ("SIMULATED_SUCCEEDED",),
                "FETCH_SUCCEEDED",
                "Simulated fetch succeeded; PowerShell was not executed.",
            )

            return True

        except Exception as exc:
            self.log_state_change(
                "fetch",
                ("SIMULATED_FAILED", str(exc)),
                "FETCH_FAILED",
                f"Simulation failed: {exc}",
            )
            return False






import csv
import math
import os
import random
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from core.queue_db import dequeue_item, init_db, mark_done


import math
import re
import time
from datetime import datetime, timezone
import logging
import time

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

SAMPLE_ALERTS = {
    datetime(2026, 9, 6, 10, 43, 12, tzinfo=timezone.utc): (1, 24673.0),
    datetime(2026, 9, 6, 10, 48, 12, tzinfo=timezone.utc): (2, 131574.0),
    datetime(2026, 9, 6, 10, 53, 12, tzinfo=timezone.utc): (3, 122433.0),
    datetime(2026, 9, 6, 10, 58, 12, tzinfo=timezone.utc): (4, 9060.0),
}


def create_sample_email_context(email: dict) -> tuple[int, EmailContext]:
    sender = email.get("sender", "")
    subject = email.get("subject", "")
    body = email.get("body", "")
    raw_timestamp = email.get("timestamp", "")

    if isinstance(raw_timestamp, datetime):
        timestamp = raw_timestamp
    else:
        timestamp = datetime.fromisoformat(
            str(raw_timestamp).strip().replace("Z", "+00:00")
        )

    if timestamp.tzinfo is None:
        raise ValueError("Sample email timestamp must include a timezone.")

    timestamp = timestamp.astimezone(timezone.utc)
    sample = SAMPLE_ALERTS.get(timestamp)

    if sample is None:
        raise ValueError(f"Unrecognized sample timestamp: {timestamp.isoformat()}")

    if (
        sender.strip().lower() != "mvne.support@ip-tribe.com"
        or subject.strip()
        != "[Critical] Singtel BSS || DRA Gy TimerExpiry 5012 Count"
    ):
        raise ValueError("Email sender or subject does not match the samples.")

    match = re.search(
        r"Actual\s+Error\s+Count\s*:\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
        body,
        flags=re.IGNORECASE,
    )

    if match is None:
        raise ValueError("Actual Error Count is missing from the sample email.")

    initial_count = float(match.group(1).replace(",", ""))
    sample_number, expected_count = sample

    if not math.isfinite(initial_count) or initial_count != expected_count:
        raise ValueError(
            f"Sample {sample_number}: expected count {expected_count}, "
            f"received {initial_count}."
        )

    context = EmailContext(
        sender=sender,
        timestamp=timestamp,
        subject=subject,
        body=body,
        error_code="5012",
        initial_count=initial_count,
    )

    return sample_number, context



import threading



# def execute_verification(item_id, sample_number, monitor):
#     try:
#         logging.info(
#             "[*] Starting verification: item_id=%s | sample=%s",
#             item_id,
#             sample_number,
#         )

#         result = monitor.run_monitoring_loop()

#         logging.info(
#             "[*] Verification result: item_id=%s\n%s",
#             item_id,
#             result.model_dump_json(indent=2),
#         )

#         mark_done(item_id)

#     except Exception:
#         logging.exception(
#             "[!] Verification failed: item_id=%s; item not marked done",
#             item_id,
#         )



# def run_verification_worker():

#     vpn_process = None

#     print("STARTING SHIT..")
#     scenarios = {
#         1: "sustained",
#         2: "escalating",
#         3: "resolving_stop",
#         4: "intermittent_stop",
#     }

#     try:

#         if vpn_already_connected(OPENSEARCH_URL):
#             print("[VPN] Existing VPN connection is usable; reusing it.")
#         else:
#             vpn_process = connect_vpn(VPN_PROFILE)

#         if not setup_opensearch_credentials(
#             username=USERNAME,
#             password=PASSWORD,
#             output_path=CRED_FILE,
#         ):
#             raise RuntimeError("OpenSearch credential setup failed.")
        
#         init_db()

#         logging.info(
#             "[*] Verification Worker started listening on 'verification_queue'"
#         )

#         idle_count = 0

#         while True:
#             try:

#                 item = dequeue_item("verification_queue")

#                 if not item:
#                     time.sleep(1.0)
#                     continue
                
#                 # if not item:
#                 #     time.sleep(1.0)
#                 #     idle_count += 1

#                 #     if idle_count > 100:
#                 #         logging.info("[*] Verification queue empty. Exiting worker.")
#                 #         break

#                 #     continue


#                 idle_count = 0
#                 item_id, email = item

#                 subject = email.get("subject", "")
#                 body = email.get("body", "")
#                 sender = email.get("sender", "")
#                 timestamp = email.get("timestamp", "")

#                 logging.info(
#                     "[*] Dequeued verification task: item_id=%s | timestamp=%s",
#                     item_id,
#                     timestamp,
#                 )

#                 sample_number, email_context = create_sample_email_context(email)

#                 # wf = OpenSearchMonitor(
#                 #     email_context=email_context,
#                 #     script_path=(
#                 #         "/root/projects/alert_results/demo/core/verification/"
#                 #         "Fetch-DashboardMetrics.ps1"
#                 #     ),
#                 #     output_dir=(
#                 #         "/root/projects/alert_results/demo/core/verification/"
#                 #         f"OpenSearch data/item_{item_id}"
#                 #     ),
#                 #     monitoring_duration_minutes=15,
#                 #     poll_interval_seconds=30,
#                 #     csv_timezone="Asia/Singapore",
#                 #     fetch_timeout_seconds=120,
#                 # )

#                 wf = SimulatedOpenSearchMonitor(
#                     email_context=email_context,
#                     scenario=scenarios[sample_number],
#                     seed=sample_number,
#                     output_dir=(
#                         "/root/projects/alert_results/demo/core/verification/"
#                         f"OpenSearch data/item_{item_id}"
#                     ),
#                     monitoring_duration_minutes=15,
#                     poll_interval_seconds=30,
#                     csv_timezone="Asia/Singapore",
#                 )


#                 # spawns a thread and execute run_monitoring_loop
#                 thread = threading.Thread(
#                     target=execute_verification,
#                     args=(item_id, sample_number, wf),
#                     name=f"verification-{item_id}",
#                     daemon=False,
#                 )
#                 thread.start()

#                 # Each dequeued email represents a separate verification instance.
#                 # TODO: Create EmailContext using subject, body, sender, timestamp.
#                 # TODO: Create and run a separate OpenSearchMonitor for this email.
#                 # TODO: Call mark_done(item_id) only after processing completes.
#                 # Queue completion means the task was dispatched to a monitoring thread.
#                 mark_done(item_id)

#                 logging.info(
#                     "[*] Verification dispatched; queue item marked complete: "
#                     "item_id=%s | sample=%s | thread=%s",
#                     item_id,
#                     sample_number,
#                     thread.name,
#                 )

#             except Exception as err:
#                 logging.error(
#                     "[!] Error in verification worker: %s",
#                     err,
#                     exc_info=True,
#                 )
#                 time.sleep(1.0)

#     finally:
#         if vpn_process is not None:
#             disconnect_vpn(vpn_process)


def execute_verification(item_id,sample_number,monitor):
    try:
        result=monitor.run_monitoring_loop()
        logging.info('Verification result: item_id=%s\n%s',item_id,result.model_dump_json(indent=2))
    except Exception as exc:
        update_verification(monitor.telemetry_email_id,'FAILED_VERIFICATION',
            'Verification thread failed',workflow_id=monitor.workflow_id,error=str(exc))
        logging.exception('Verification thread failed: item_id=%s',item_id)


def run_verification_worker():
    vpn_process=None
    threads=[]
    scenarios={1:'sustained',2:'escalating',3:'resolving_stop',4:'intermittent_stop'}
    try:
        if vpn_already_connected(OPENSEARCH_URL):
            print('[VPN] Existing VPN connection is usable; reusing it.')
        else:
            vpn_process=connect_vpn(VPN_PROFILE)
        if not setup_opensearch_credentials(username=USERNAME,password=PASSWORD,output_path=CRED_FILE):
            raise RuntimeError('OpenSearch credential setup failed.')
        init_verification_db()
        logging.info('Verification Worker listening on verification_queue')
        while True:
            item_id=None
            email=None
            dispatched=False
            try:
                item=dequeue_item('verification_queue')
                if item is None:
                    time.sleep(1)
                    continue
                item_id,email=item
                update_verification(email['email_id'],'EXECUTING_VERIFICATION','Verification task dequeued')
                sample_number,email_context=create_sample_email_context(email)
                monitor=SimulatedOpenSearchMonitor(
                    email_context=email_context,scenario=scenarios[sample_number],seed=sample_number,
                    output_dir=f'/root/projects/alert_results/demo/core/verification/OpenSearch data/item_{item_id}',
                    monitoring_duration_minutes=15,poll_interval_seconds=30,
                    csv_timezone='Asia/Singapore',fetch_timeout_seconds=120)
                # For live fetching, use OpenSearchMonitor with the same output_dir
                # and fixed script_path, omitting scenario and seed.
                bind_monitor(monitor,email)
                thread=threading.Thread(target=execute_verification,
                    args=(item_id,sample_number,monitor),name=f'verification-{item_id}',daemon=False)
                thread.start()
                dispatched=True
                threads=[t for t in threads if t.is_alive()]
                threads.append(thread)
                # Queue DONE means dispatched, as requested; DB status tracks execution.
                mark_done(item_id)
            except Exception as exc:
                if email and not dispatched:
                    update_verification(email['email_id'],'FAILED_VERIFICATION',
                                        'Unable to start verification',error=str(exc))
                logging.exception('Verification worker error: item_id=%s',item_id)
                time.sleep(1)
    except KeyboardInterrupt:
        logging.info('Verification worker stopping; allowing active monitors to finish')
    finally:
        # Keep an owned VPN alive until ongoing monitors finish. No joins in the dequeue loop.
        for thread in threads:
            thread.join()
        if vpn_process is not None:
            disconnect_vpn(vpn_process)


# asd

# cd /root/projects/alert_results/demo
# python -m core.verification.simulate_workflow_verification
if __name__ == "__main__":
    
    run_verification_worker()
