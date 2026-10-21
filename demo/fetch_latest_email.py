"""
Fetch the latest inbox email from Outlook via Microsoft Graph.

Auth order:
1. Username/password from .env (ROPC) — only works if MFA/basic-auth policy allows it
2. Device-code interactive login — works with MFA (open the URL and enter the code)
"""

from __future__ import annotations

import re
import json
import os
import sys
import csv

from html import unescape
from html.parser import HTMLParser

import msal
import requests

from datetime import datetime, timezone

from dotenv import load_dotenv
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from html import unescape
from html.parser import HTMLParser


# Avoid UnicodeEncodeError on Windows cp1252 consoles.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def safe_text(value: object) -> str:
    return str(value).encode("utf-8", errors="replace").decode("utf-8")


# Public client used by Microsoft Graph PowerShell.
DEFAULT_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
GRAPH_SCOPES = ["https://graph.microsoft.com/Mail.Read"]
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOKEN_CACHE_PATH = ".msal_token_cache.json"


class EmailAddress(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    address: str | None = None


class EmailSender(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
        populate_by_name=True,
    )

    email_address: EmailAddress = Field(
        alias="emailAddress",
    )


class EmailMessage(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
        populate_by_name=True,
    )

    subject: str = "(no subject)"

    sender: EmailSender | None = Field(
        default=None,
        alias="from",
    )

    received_date_time: str = Field(
        default="(unknown time)",
        alias="receivedDateTime",
    )

    body_preview: str | None = Field(
        default=None,
        alias="bodyPreview",
    )

    body: dict = Field(
        default_factory=dict,
    )

    is_read: bool | None = Field(
        default=None,
        alias="isRead",
    )

    has_attachments: bool | None = Field(
        default=None,
        alias="hasAttachments",
    )

    internet_message_id: str | None = Field(
        default=None,
        alias="internetMessageId",
    )

    @property
    def sender_address(self) -> str:
        if not self.sender:
            return "(unknown)"

        email_address = self.sender.email_address

        return (
            email_address.address
            or email_address.name
            or "(unknown)"
        )
    
    @property
    def body_text(self) -> str:
        content = (
            self.body.get("content")
            or self.body_preview
            or ""
        )

        content_type = (
            self.body.get("contentType")
            or ""
        ).lower()

        if content_type == "html":
            return remove_html(content)
        
        return content.strip()


class HTMLTextRemover(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        tag = tag.lower()

        if tag in {
            "style",
            "script",
            "head",
            "title",
            "meta",
            "svg",
            "noscript",
        }:
            self._skip_depth += 1
            return

        if self._skip_depth == 0 and tag in {
            "br",
            "p",
            "div",
            "section",
            "article",
            "tr",
            "li",
        }:
            self._chunks.append("\n")

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()

        if tag in {
            "style",
            "script",
            "head",
            "title",
            "meta",
            "svg",
            "noscript",
        }:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return

        if self._skip_depth == 0 and tag in {
            "p",
            "div",
            "section",
            "article",
            "tr",
            "li",
        }:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._chunks.append(data)

    def handle_comment(self, data: str) -> None:
        # Ignore HTML comments, including Outlook CSS comments.
        return

    def get_text(self) -> str:
        return "".join(self._chunks)


def remove_html(value: str | None) -> str:
    """
    Convert HTML email content into clean plain text.

    Removes:
    - CSS and JavaScript
    - HTML comments
    - Head metadata
    - SVG and hidden script content
    - Excessive whitespace
    """
    if not value:
        return ""

    # Remove Outlook conditional comments and CSS comments.
    value = re.sub(
        r"<!--.*?-->",
        "",
        value,
        flags=re.DOTALL,
    )

    # Remove complete style/script blocks before parsing.
    value = re.sub(
        r"<(style|script|head|noscript|svg)\b[^>]*>.*?</\1\s*>",
        "",
        value,
        flags=re.IGNORECASE | re.DOTALL,
    )

    parser = HTMLTextRemover()
    parser.feed(value)
    parser.close()

    text = unescape(parser.get_text())

    # Normalize non-breaking spaces and line endings.
    text = text.replace("\xa0", " ")
    text = text.replace("\r\n", "\n")
    text = text.replace("\r", "\n")

    # Remove whitespace around lines.
    lines = [
        re.sub(r"[ \t]+", " ", line).strip()
        for line in text.split("\n")
    ]

    # Remove empty lines while preserving paragraph separation.
    cleaned_lines: list[str] = []

    for line in lines:
        if line:
            cleaned_lines.append(line)
        elif cleaned_lines and cleaned_lines[-1] != "":
            cleaned_lines.append("")

    return "\n".join(cleaned_lines).strip()


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        self._chunks.append(data)

    def get_text(self) -> str:
        return unescape(" ".join(self._chunks))


def html_to_text(html: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(html or "")
    return " ".join(parser.get_text().split())


def load_env() -> tuple[str, str]:
    load_dotenv()
    email = os.getenv("OUTLOOK_EMAIL", "").strip()
    password = os.getenv("OUTLOOK_PASSWORD", "").strip()
    if not email:
        raise SystemExit("Missing OUTLOOK_EMAIL in .env")
    return email, password


def build_msal_app() -> msal.PublicClientApplication:
    client_id = os.getenv("AZURE_CLIENT_ID", DEFAULT_CLIENT_ID).strip()
    tenant = os.getenv("AZURE_TENANT_ID", "organizations").strip()
    cache = msal.SerializableTokenCache()
    if os.path.exists(TOKEN_CACHE_PATH):
        cache.deserialize(open(TOKEN_CACHE_PATH, encoding="utf-8").read())

    app = msal.PublicClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant}",
        token_cache=cache,
    )
    app._tribal_cache = cache  # noqa: SLF001 - attach for persist helper
    return app


def persist_cache(app: msal.PublicClientApplication) -> None:
    cache = getattr(app, "_tribal_cache", None)
    if cache is not None and cache.has_state_changed:
        with open(TOKEN_CACHE_PATH, "w", encoding="utf-8") as fh:
            fh.write(cache.serialize())


def acquire_token(email: str, password: str) -> str:
    app = build_msal_app()

    accounts = app.get_accounts(username=email)
    if accounts:
        result = app.acquire_token_silent(GRAPH_SCOPES, account=accounts[0])
        if result and "access_token" in result:
            persist_cache(app)
            print("Using cached Graph token.")
            return result["access_token"]

    if password and "--device-code" not in sys.argv:
        print("Trying email/password against Microsoft Graph…", flush=True)
        result = app.acquire_token_by_username_password(
            username=email,
            password=password,
            scopes=GRAPH_SCOPES,
        )
        if result and "access_token" in result:
            persist_cache(app)
            return result["access_token"]
        print(
            safe_text(
                f"[Graph] Password auth failed: "
                f"{result.get('error')} - {result.get('error_description')}"
            ),
            file=sys.stderr,
            flush=True,
        )
        print(
            "Your tenant requires MFA (or blocks ROPC). "
            "Switching to device-code login...",
            file=sys.stderr,
            flush=True,
        )

    flow = app.initiate_device_flow(scopes=GRAPH_SCOPES)
    if "user_code" not in flow:
        raise SystemExit(
            safe_text(f"Failed to create device flow: {json.dumps(flow, indent=2)}")
        )

    print(safe_text(flow["message"]), flush=True)
    result = app.acquire_token_by_device_flow(flow)
    if not result or "access_token" not in result:
        raise SystemExit(
            safe_text(
                "Device-code login failed: "
                f"{result.get('error') if result else 'unknown'} - "
                f"{result.get('error_description') if result else ''}"
            )
        )

    persist_cache(app)
    return result["access_token"]


def fetch_latest_inbox_email(token: str) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "$top": 1,
        "$orderby": "receivedDateTime desc",
        "$select": (
            "subject,from,receivedDateTime,bodyPreview,body,"
            "isRead,hasAttachments,internetMessageId"
        ),
    }
    url = f"{GRAPH_BASE}/me/mailFolders/inbox/messages"
    response = requests.get(url, headers=headers, params=params, timeout=60)
    response.raise_for_status()
    messages = response.json().get("value", [])
    if not messages:
        raise RuntimeError("Inbox is empty.")
    return messages[0]


def format_message(msg: dict) -> str:

    from_info = msg.get("from") or {}
    email_addr = from_info.get("emailAddress") or {}
    sender = email_addr.get("address") or email_addr.get("name") or "(unknown)"
    subject = msg.get("subject") or "(no subject)"
    received = msg.get("receivedDateTime") or "(unknown time)"
    body = msg.get("body") or {}
    content = body.get("content") or msg.get("bodyPreview") or ""
    if (body.get("contentType") or "").lower() == "html":
        content = html_to_text(content)

    return "\n".join(
        [
            f"From: {sender}",
            f"Subject: {subject}",
            f"Received: {received}",
            f"Read: {msg.get('isRead')}",
            f"Attachments: {msg.get('hasAttachments')}",
            "",
            "--- Body ---",
            content.strip() or "(empty body)",
        ]
    )



def fetch_latest_inbox_emails(
    token: str,
    count: int,
) -> list[dict[str, Any]]:

    headers = {
        "Authorization": f"Bearer {token}",
    }

    url = f"{GRAPH_BASE}/me/mailFolders/inbox/messages"

    params = {
        "$top": min(1000, count),
        "$orderby": "receivedDateTime desc",
        "$select": (
            "subject,from,receivedDateTime,bodyPreview,"
            "body,isRead,hasAttachments,"
            "internetMessageId"
        ),
    }

    messages = []

    while url and len(messages) < count:

        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=60,
        )

        response.raise_for_status()

        payload = response.json()

        messages.extend(
            payload.get("value", [])
        )

        url = payload.get(
            "@odata.nextLink"
        )

        # nextLink already contains query params
        params = None

        print(
            f"Fetched {len(messages)} emails..."
        )

    return messages[:count]



def fetch_inbox_emails_by_month(
    token: str,
    year: int,
    month: int,
) -> list[dict[str, Any]]:
    """
    Fetch all Inbox emails received during a specific month.

    Example:
        fetch_inbox_emails_by_month(token, 2026, 1)
    """

    headers = {
        "Authorization": f"Bearer {token}",
    }

    # First day of requested month
    start_dt = datetime(year, month, 1, tzinfo=timezone.utc)

    # First day of next month
    if month == 12:
        end_dt = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end_dt = datetime(year, month + 1, 1, tzinfo=timezone.utc)

    start_str = start_dt.isoformat().replace("+00:00", "Z")
    end_str = end_dt.isoformat().replace("+00:00", "Z")

    url = f"{GRAPH_BASE}/me/mailFolders/inbox/messages"

    params = {
        "$top": 1000,
        "$orderby": "receivedDateTime desc",
        "$filter": (
            f"receivedDateTime ge {start_str} and "
            f"receivedDateTime lt {end_str}"
        ),
        "$select": (
            "subject,from,receivedDateTime,bodyPreview,"
            "body,isRead,hasAttachments,"
            "internetMessageId"
        ),
    }

    messages = []

    while url:
        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=60,
        )

        response.raise_for_status()

        payload = response.json()

        batch = payload.get("value", [])
        messages.extend(batch)

        print(
            f"Fetched {len(messages)} emails "
            f"for {year}-{month:02d}..."
        )

        url = payload.get("@odata.nextLink")

        # nextLink already contains all query parameters
        params = None

    return messages



def save_emails_to_csv(
    emails: list[EmailMessage],
    output_csv: str,
) -> None:
    """
    Save EmailMessage objects to CSV.

    Columns:
        sender
        timestamp
        subject
        body
    """

    with open(
        output_csv,
        "w",
        newline="",
        encoding="utf-8",
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "sender",
                "timestamp",
                "subject",
                "body",
            ],
        )

        writer.writeheader()

        for email in emails:
            writer.writerow(
                {
                    "sender": email.sender_address,
                    "timestamp": email.received_date_time,
                    "subject": email.subject,
                    "body": email.body_text,
                }
            )

    print(
        f"Saved {len(emails)} emails to {output_csv}"
    )



def main() -> None:

    email, password = load_env()
    print(f"Connecting as {email}...", flush=True)

    token = acquire_token(email, password)
    # msg = fetch_latest_inbox_email(token)

    # messages = fetch_latest_inbox_emails(
    #     token,
    #     count=3000,
    # )
    
    emails = fetch_inbox_emails_by_month(
        token,
        year=2026,
        month=9,
    )

    print(f"Found {len(emails)} emails")
    print("Sample: ", emails[0].get('subject'))

    # for i, message in enumerate(emails):
    #     print(f"{i + 1}. {message.get('subject')}")

    # input("")
    
    # email = EmailMessage.model_validate(msg)
    emails = [
        EmailMessage.model_validate(email)
        for email in emails
    ]

    
    save_emails_to_csv(
        emails,
        "emails_2026_09.csv",
    )

    # print("SENDER: ", email.sender_address)
    # print("SUBJECT: ", email.subject)
    # print("RECEIVED: ", email.received_date_time)
    # print("BODY: ", email.body_text)
    # print("\n\n")

    # print(safe_text(format_message(msg)), flush=True)
    # if "--json" in sys.argv:
    #     print("\n--- JSON ---", flush=True)
    #     print(safe_text(json.dumps(msg, indent=2, default=str)), flush=True)


# Need to curate a dataset of emails that are not DRA Gy
# prediction on non curated - accuracy 
# prediction on curated - accuracy



# 
# 
# process: 
# Alert Received in Outlook -> email extractor -> model classify -> verify against type \
# -> perform service validation -> compile information -> create jira alerts
# 
# Verify against type 
# - this would require specialized knowledge on what parameters to look out for
# 
# Procedure: 
# App will poll for new emails every x seconds
# filter out new emails 
# types = model_classify(emails)
# for type in types: 
#   if type == 'DRA GY": 
#       init Agent, Agent contains tool to verify against opensearch, graph, or api, and perform service validation (Test call and SMS)
#       execute Agent(context)
#       info = compile_information(context)
#       create_jira_alerts(info)
# 
# Scaling: 
# once the machine learning model is trained on all types, automatically be able to discern
# - how to scale to customers, this would require them to provide us with data
# 
# agent design, 
# - goal is to have the user provide all the references for the Agent's Task. 
# i.e. 
# 

if __name__ == "__main__":
    main()
