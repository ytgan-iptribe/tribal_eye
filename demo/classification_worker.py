import logging
import time
from typing import Literal

import requests
from pydantic import BaseModel, ValidationError

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from core.queue_db import dequeue_item, mark_done

from core.email_status import (
    COMPLETED,
    EXECUTING,
    FAILED,
    enqueue_downstream,
    init_downstream_status_db,
    update_email,
)

from core.jira.client import create_issue, add_attachment

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
)

JIRA_EVIDENCE_ROOT = (
    Path(__file__).resolve().parent / "jira_evidence"
)

SYSTEM_PROMPT = """Classify the email for the IS_ALERT task.

Return YES if it is an L1 monitoring alert, including a notification that
its monitored condition has resolved. Return NO for tickets, correspondence,
and summary reports that are not themselves alerts.

Respond with exactly {"label":"YES"} or {"label":"NO"}."""


class IsAlertPrediction(BaseModel):
    label: Literal["YES", "NO"]


def is_5012_alert(sender, subject, body):
    sender, subject, body = (
        str(value or "").lower()
        for value in (sender, subject, body)
    )

    return (
        "jira" not in sender
        and "jira" not in subject
        and "eclipse" not in subject
        and "eclipse" not in body
        and (
            "mvne.support" in sender
            or "opensearch" in body
        )
        and all(
            term in subject or term in body
            for term in (
                "singtel bss",
                "dra gy",
                "timerexpiry",
                "5012",
            )
        )
    )


def call_llama_server(
    subject,
    body,
    endpoint="http://127.0.0.1:8000/v1/chat/completions",
):
    response = requests.post(
        endpoint,
        json={
            "messages": [
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": (
                        f"Subject: {subject}\n\n"
                        f"Body:\n{body}"
                    ),
                },
            ],
            "temperature": 0.0,
            "max_tokens": 32,
        },
        timeout=5,
    )

    response.raise_for_status()

    return (
        response.json()["choices"][0]["message"]["content"]
        .strip()
    )


def process_email(item_id, email):
    email_id = email["email_id"]

    label = "UNKNOWN"
    rule_label = "UNKNOWN"
    actionable = False
    errors = []

    try:
        update_email(email_id, EXECUTING)

        logging.info(
            "%s email=%s",
            EXECUTING,
            email_id,
        )

        subject, body, sender = (
            email.get(key, "")
            for key in ("subject", "body", "sender")
        )

        try:
            raw = call_llama_server(subject, body)

            label = (
                IsAlertPrediction
                .model_validate_json(raw)
                .label
            )

        except (
            requests.RequestException,
            ValidationError,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            AttributeError,
        ) as exc:
            errors.append(
                f"IS_ALERT prediction failed: {exc}"
            )

        rule_label = (
            "YES"
            if is_5012_alert(sender, subject, body)
            else "NO"
        )

        # Preserve the existing fail-open policy.
        should_route = (
            label != "NO"
            and rule_label == "YES"
        )

        if should_route:
            if label == "YES" and rule_label == "YES":
                metadata = create_alert_jira_ticket(email)
            enqueue_downstream(email)
            actionable = True

        status = FAILED if errors else COMPLETED
        
        update_email(
            email_id,
            status,
            is_alert=label,
            is_5012=rule_label,
            routed_actionable=actionable,
            error="; ".join(errors) or None,
        )

        mark_done(item_id)

        logging.info(
            "%s email=%s IS_ALERT=%s IS_5012=%s",
            status,
            email_id,
            label,
            rule_label,
        )
    
    except Exception as exc:
        errors.append(str(exc))

        update_email(
            email_id,
            FAILED,
            is_alert=label,
            is_5012=rule_label,
            routed_actionable=actionable,
            error="; ".join(errors),
        )

        logging.exception(
            "Classification processing failed: email=%s",
            email_id,
        )

        # A downstream enqueue may have partially succeeded.
        # Leave this queue item PROCESSING for explicit recovery,
        # avoiding automatic duplicate downstream tasks.






def save_jira_metadata(path: Path, metadata: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temporary.replace(path)


def create_alert_jira_ticket(email: dict) -> dict:
    """Create one ticket for this email and attach its original email data."""

    email_id = str(email.get("email_id") or "")

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", email_id):
        raise ValueError("A valid email_id is required for Jira evidence.")

    directory = JIRA_EVIDENCE_ROOT / email_id
    directory.mkdir(parents=True, exist_ok=True)

    email_path = directory / "email.txt"
    metadata_path = directory / "jira.json"

    # Preserve the supplied subject/body without truncating the attachment.
    if not email_path.exists():
        email_path.write_text(
            f"Email ID: {email_id}\n"
            f"Simulation Run ID: {email.get('simulation_run_id', '')}\n"
            f"Sender: {email.get('sender', '')}\n"
            f"Received: {email.get('timestamp', '')}\n"
            f"Subject: {email.get('subject', '')}\n"
            f"\nBody:\n{email.get('body', '')}",
            encoding="utf-8",
        )

    metadata = (
        json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata_path.exists()
        else {}
    )

    if not metadata.get("issue_key"):
        if metadata.get("status") == "CREATION_PENDING":
            raise RuntimeError(
                f"Previous Jira creation outcome is uncertain for {email_id}. "
                "Check Jira before attempting another creation."
            )

        # These must match the preset values in Issue Types MR MVNO.
        issue_types = [
            value.strip()
            for value in os.getenv("JIRA_MR_MVNO_OPTIONS", "").split(",")
            if value.strip()
        ]

        if not issue_types:
            raise ValueError(
                "Set JIRA_MR_MVNO_OPTIONS to a valid "
                "'Issue Types MR MVNO' field value."
            )

        updated_at = datetime.now(timezone.utc).isoformat()

        subject = str(email.get("subject") or "DRA Gy TimerExpiry 5012 alert")
        summary = " ".join(subject.split())[:255]

        description = (
            f"Alert summary: {subject}\n\n"
            "This ticket is actively being updated by the automated "
            "L1 verification and modem-check workflow.\n"
            "Verification and modem evidence will be added in subsequent "
            "workflow updates.\n"
            f"Last updated: {updated_at}\n\n"
            f"Email ID: {email_id}\n"
            f"Simulation Run ID: {email.get('simulation_run_id', '')}\n"
            f"Sender: {email.get('sender', '')}\n"
            f"Alert received: {email.get('timestamp', '')}\n"
            "Classification: IS_ALERT=YES; IS_5012=YES\n\n"
            "The original email is attached as email.txt."
        )

        # Record the attempt before making the external request.
        metadata = {
            "email_id": email_id,
            "status": "CREATION_PENDING",
            "email_path": str(email_path),
            "updated_at": updated_at,
        }
        save_jira_metadata(metadata_path, metadata)

        issue = create_issue(
            issue_types=issue_types,
            description=description,
            summary=summary,
            project_key=os.getenv("JIRA_PROJECT", "MSSTP"),
            jira_issue_type=os.getenv("JIRA_ISSUE_TYPE", "Incident"),
        )

        # Save the key BEFORE attachment upload. An attachment failure
        # must not cause another ticket to be created.
        metadata.update(
            issue_key=issue.key,
            issue_url=issue.url,
            status="CREATED",
            email_attachment_uploaded=False,
        )
        save_jira_metadata(metadata_path, metadata)

        logging.info(
            "[JIRA CREATED] email_id=%s | issue=%s | url=%s",
            email_id,
            issue.key,
            issue.url,
        )

    if not metadata.get("email_attachment_uploaded"):
        add_attachment(
            issue_key=metadata["issue_key"],
            file_path=email_path,
            project_key=os.getenv("JIRA_PROJECT", "MSSTP"),
        )

        metadata.update(
            status="EMAIL_ATTACHED",
            email_attachment_uploaded=True,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        save_jira_metadata(metadata_path, metadata)

        logging.info(
            "[JIRA ATTACHED] email_id=%s | issue=%s | file=%s",
            email_id,
            metadata["issue_key"],
            email_path,
        )

    # Carry the ticket reference into both downstream queue payloads.
    email["jira_issue_key"] = metadata["issue_key"]
    email["jira_issue_url"] = metadata["issue_url"]
    email["jira_evidence_dir"] = str(directory)

    return metadata



def run_classification_worker():
    # init_status_db()
    init_downstream_status_db()

    logging.info(
        "Classification worker listening on email_queue"
    )
    
    while True:
        try:
            item = dequeue_item("email_queue")

            if not item:
                time.sleep(1)
                continue

            process_email(*item)

        except Exception:
            logging.exception(
                "Worker error; continuing"
            )
            time.sleep(1)


if __name__ == "__main__":
    run_classification_worker()