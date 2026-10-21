"""
CRUD helpers for Jira Cloud issues on project MSSTP.

Environment (loaded from the repo ``.env``):
    JIRA_LINK     Site host, with or without https://
    JIRA_EMAIL    Atlassian account email that owns the API token
    JIRA_API_KEY  API token from https://id.atlassian.com/manage-profile/security/api-tokens

"Issue Types MR MVNO" is a multi-select custom field. Its allowed values are
loaded from Jira and rejected when the caller passes anything else.
Description is free text.

Jira's own issue type is separate (Incident, Query, Support on this project).
New tickets use Incident unless another project issue type is passed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import requests
from dotenv import load_dotenv

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ROOT = Path(__file__).resolve().parents[1]
# load_dotenv(ROOT / ".env")

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

DEFAULT_PROJECT = "MSSTP"
MR_MVNO_FIELD_NAME = "Issue Types MR MVNO"
DEFAULT_JIRA_ISSUE_TYPE = "Incident"
# Reporter is marked required in createmeta, and Jira fills it from the API user.
_AUTO_FIELDS = {"reporter"}


class JiraError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


@dataclass(frozen=True)
class IssueType:
    """A Jira issue type such as Incident, Query, or Support."""

    id: str
    name: str
    subtask: bool = False


@dataclass(frozen=True)
class FieldOption:
    """One allowed value of the Issue Types MR MVNO field."""

    id: str
    value: str


@dataclass(frozen=True)
class Issue:
    key: str
    id: str
    summary: str
    description: str
    issue_types: tuple[str, ...]
    jira_issue_type: str
    status: str
    url: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "id": self.id,
            "summary": self.summary,
            "description": self.description,
            "issue_types": list(self.issue_types),
            "jira_issue_type": self.jira_issue_type,
            "status": self.status,
            "url": self.url,
        }


def _site_url() -> str:
    raw = os.environ.get("JIRA_LINK", "").strip().rstrip("/")
    if not raw:
        raise JiraError("JIRA_LINK is not set.")
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    return raw


def _credentials() -> tuple[str, str]:
    email = os.environ.get("JIRA_EMAIL", "").strip()
    token = os.environ.get("JIRA_API_KEY", "").strip()
    if not email or not token:
        raise JiraError("JIRA_EMAIL and JIRA_API_KEY must both be set.")
    return email, token


def text_to_adf(text: str) -> dict[str, Any]:
    """Plain text to Atlassian Document Format (Jira Cloud description)."""
    paragraphs = []
    for block in (text or "").split("\n"):
        if block:
            paragraphs.append(
                {"type": "paragraph", "content": [{"type": "text", "text": block}]}
            )
        else:
            paragraphs.append({"type": "paragraph"})
    if not paragraphs:
        paragraphs.append({"type": "paragraph"})
    return {"type": "doc", "version": 1, "content": paragraphs}


def adf_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return str(value)

    chunks: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "text":
                chunks.append(node.get("text") or "")
            elif node.get("type") == "hardBreak":
                chunks.append("\n")
            for child in node.get("content") or []:
                walk(child)
            if node.get("type") in {"paragraph", "heading", "listItem", "blockquote"}:
                chunks.append("\n")
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return "".join(chunks).strip()


def _summary_from_description(description: str) -> str:
    for line in description.splitlines():
        line = line.strip()
        if line:
            return line[:120]
    return "New MSSTP ticket"


def _as_names(issue_types: str | Sequence[str]) -> list[str]:
    if isinstance(issue_types, str):
        names = [issue_types]
    else:
        names = [str(item) for item in issue_types]
    cleaned = [name.strip() for name in names if name and name.strip()]
    if not cleaned:
        raise ValueError("At least one Issue Types MR MVNO value is required.")
    return cleaned


class JiraClient:
    """Create, read, update, and delete issues on one Jira project."""

    def __init__(self, project_key: str = DEFAULT_PROJECT, timeout: float = 30) -> None:
        self.project_key = project_key.strip() or DEFAULT_PROJECT
        self.timeout = timeout
        self.base_url = _site_url()
        email, token = _credentials()
        self._session = requests.Session()
        self._session.auth = (email, token)
        self._session.headers["Accept"] = "application/json"
        self._issue_types: list[IssueType] | None = None
        self._mr_mvno_field_id: str | None = None
        self._mr_mvno_options: list[FieldOption] | None = None

    def jira_issue_types(self, *, refresh: bool = False) -> list[IssueType]:
        """Jira issue types on the project (Incident, Query, Support)."""
        if self._issue_types is not None and not refresh:
            return list(self._issue_types)

        payload = self._request(
            "GET",
            f"/rest/api/3/issue/createmeta/{self.project_key}/issuetypes",
            params={"maxResults": 100},
        )
        raw_types = _extract_list(payload, ("issueTypes", "values", "results"))
        types = [
            IssueType(
                id=str(item["id"]),
                name=str(item["name"]),
                subtask=bool(item.get("subtask")),
            )
            for item in raw_types
            if item.get("id") and item.get("name")
        ]
        if not types:
            raise JiraError(
                f"No issue types were returned for project {self.project_key}."
            )
        self._issue_types = types
        return list(types)

    def resolve_jira_issue_type(self, name: str) -> IssueType:
        wanted = (name or "").strip()
        folded = wanted.casefold()
        for item in self.jira_issue_types():
            if item.name.casefold() == folded:
                return item
        names = ", ".join(item.name for item in self.jira_issue_types())
        raise ValueError(
            f"Jira issue type {wanted!r} is not valid for {self.project_key}. "
            f"Allowed: {names}"
        )

    def mr_mvno_options(self, *, refresh: bool = False) -> list[FieldOption]:
        """Allowed values for the Issue Types MR MVNO field."""
        if self._mr_mvno_options is not None and not refresh:
            return list(self._mr_mvno_options)

        field = self._mr_mvno_field(refresh=refresh)
        options = []
        for item in field.get("allowedValues") or []:
            if not isinstance(item, dict) or item.get("disabled"):
                continue
            value = item.get("value")
            option_id = item.get("id")
            if value and option_id:
                options.append(FieldOption(id=str(option_id), value=str(value)))
        if not options:
            raise JiraError(f"{MR_MVNO_FIELD_NAME!r} has no selectable values.")
        self._mr_mvno_options = options
        return list(options)

    def resolve_mr_mvno(self, issue_types: str | Sequence[str]) -> list[FieldOption]:
        """Map caller text onto the field's preset options."""
        options = self.mr_mvno_options()
        by_name = {item.value.casefold(): item for item in options}
        resolved: list[FieldOption] = []
        seen: set[str] = set()
        unknown: list[str] = []
        for name in _as_names(issue_types):
            match = by_name.get(name.casefold())
            if match is None:
                unknown.append(name)
                continue
            if match.id not in seen:
                seen.add(match.id)
                resolved.append(match)
        if unknown:
            allowed = ", ".join(item.value for item in options)
            raise ValueError(
                "Unknown Issue Types MR MVNO value(s): "
                + ", ".join(repr(name) for name in unknown)
                + f". Allowed: {allowed}"
            )
        return resolved



    def create_issue(
        self,
        issue_types: str | Sequence[str],
        description: str,
        summary: str | None = None,
        *,
        jira_issue_type: str = DEFAULT_JIRA_ISSUE_TYPE,
    ) -> Issue:
        """Create an issue. ``issue_types`` must be preset MR MVNO values."""
        chosen_type = self.resolve_jira_issue_type(jira_issue_type)
        if chosen_type.subtask:
            raise ValueError(
                f"Jira issue type {chosen_type.name!r} is a subtask and needs a parent."
            )
        selected = self.resolve_mr_mvno(issue_types)
        summary_text = (summary or "").strip() or _summary_from_description(description)
        fields: dict[str, Any] = {
            "project": {"key": self.project_key},
            "issuetype": {"id": chosen_type.id},
            "summary": summary_text,
            "description": text_to_adf(description),
            self._mr_mvno_field_id_value(): [{"id": item.id} for item in selected],
        }
        self._check_required_fields(chosen_type, fields)
        created = self._request("POST", "/rest/api/3/issue", json={"fields": fields})
        key = created.get("key") if isinstance(created, dict) else None
        if not key:
            raise JiraError("Jira did not return an issue key.", payload=created)
        return self.get_issue(key)

    def get_issue(self, issue_key: str) -> Issue:
        data = self._request("GET", f"/rest/api/3/issue/{issue_key.strip()}")
        return self._parse_issue(data)

    def update_issue(
        self,
        issue_key: str,
        *,
        issue_types: str | Sequence[str] | None = None,
        description: str | None = None,
        summary: str | None = None,
        jira_issue_type: str | None = None,
    ) -> Issue:
        """Update fields on an existing issue. Omitted fields stay unchanged."""
        fields: dict[str, Any] = {}
        if issue_types is not None:
            selected = self.resolve_mr_mvno(issue_types)
            fields[self._mr_mvno_field_id_value()] = [{"id": item.id} for item in selected]
        if description is not None:
            fields["description"] = text_to_adf(description)
        if summary is not None:
            fields["summary"] = summary
        if jira_issue_type is not None:
            chosen = self.resolve_jira_issue_type(jira_issue_type)
            fields["issuetype"] = {"id": chosen.id}
        if not fields:
            raise ValueError("Provide at least one field to update.")

        key = issue_key.strip()
        self._request("PUT", f"/rest/api/3/issue/{key}", json={"fields": fields})
        return self.get_issue(key)

    def delete_issue(self, issue_key: str) -> None:
        self._request("DELETE", f"/rest/api/3/issue/{issue_key.strip()}")

    def search_issues(self, jql: str | None = None, *, limit: int = 20) -> list[Issue]:
        field_id = self._mr_mvno_field_id_value()
        query = jql or f"project = {self.project_key} ORDER BY created DESC"
        payload = self._request(
            "GET",
            "/rest/api/3/search/jql",
            params={
                "jql": query,
                "maxResults": limit,
                "fields": f"summary,description,issuetype,status,{field_id}",
            },
        )
        issues = payload.get("issues") if isinstance(payload, dict) else None
        return [self._parse_issue(item) for item in issues or []]


    def add_attachment(self, issue_key: str, file_path: str | Path) -> list[dict[str, Any]]:
        """Upload a file as an attachment, replacing any existing file with the same name."""
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"Attachment file not found: {path}")

        key = issue_key.strip()
        
        # 1. Fetch existing issue details to check for old attachments with the same name
        issue_data = self._request("GET", f"/rest/api/3/issue/{key}?fields=attachment")
        existing_attachments = issue_data.get("fields", {}).get("attachment", [])
        
        # 2. Delete any existing attachments matching this filename
        for att in existing_attachments:
            if att.get("filename") == path.name:
                att_id = att.get("id")
                if att_id:
                    self._request("DELETE", f"/rest/api/3/attachment/{att_id}")

        # 3. Upload the fresh file
        headers = {"X-Atlassian-Token": "no-check"}
        with open(path, "rb") as f:
            files = {"file": (path.name, f)}
            payload = self._request("POST", f"/rest/api/3/issue/{key}/attachments", headers=headers, files=files)
            
        return payload if isinstance(payload, list) else [payload]

    


    def _mr_mvno_field(self, *, refresh: bool = False) -> dict[str, Any]:
        for issue_type in self.jira_issue_types(refresh=refresh):
            for field in self._create_fields(issue_type.id):
                if field.get("name") == MR_MVNO_FIELD_NAME and field.get("fieldId"):
                    self._mr_mvno_field_id = str(field["fieldId"])
                    return field
        raise JiraError(
            f"Field {MR_MVNO_FIELD_NAME!r} was not found on project {self.project_key}."
        )

    def _mr_mvno_field_id_value(self) -> str:
        if not self._mr_mvno_field_id:
            self._mr_mvno_field()
        assert self._mr_mvno_field_id
        return self._mr_mvno_field_id

    def _create_fields(self, issue_type_id: str) -> list[dict[str, Any]]:
        start_at = 0
        collected: list[dict[str, Any]] = []
        while True:
            meta = self._request(
                "GET",
                f"/rest/api/3/issue/createmeta/{self.project_key}/issuetypes/{issue_type_id}",
                params={"startAt": start_at, "maxResults": 50},
            )
            if not isinstance(meta, dict):
                break
            batch = [item for item in (meta.get("fields") or []) if isinstance(item, dict)]
            collected.extend(batch)
            total = int(meta.get("total") or len(collected))
            start_at += len(batch) or total
            if not batch or start_at >= total:
                break
        return collected

    def _check_required_fields(self, issue_type: IssueType, fields: dict[str, Any]) -> None:
        missing: list[str] = []
        for field in self._create_fields(issue_type.id):
            field_id = str(field.get("fieldId") or "")
            if not field.get("required") or not field_id:
                continue
            if field_id in fields or field_id in _AUTO_FIELDS:
                continue
            missing.append(str(field.get("name") or field_id))
        if missing:
            raise ValueError(
                f"Jira issue type {issue_type.name!r} also requires: {', '.join(missing)}."
            )

    def _parse_issue(self, data: dict[str, Any]) -> Issue:
        fields = data.get("fields") or {}
        field_id = self._mr_mvno_field_id_value()
        selected = []
        for item in fields.get(field_id) or []:
            if isinstance(item, dict) and item.get("value"):
                selected.append(str(item["value"]))
        key = str(data.get("key") or "")
        return Issue(
            key=key,
            id=str(data.get("id") or ""),
            summary=str(fields.get("summary") or ""),
            description=adf_to_text(fields.get("description")),
            issue_types=tuple(selected),
            jira_issue_type=str((fields.get("issuetype") or {}).get("name") or ""),
            status=str((fields.get("status") or {}).get("name") or ""),
            url=f"{self.base_url}/browse/{key}" if key else "",
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._session.request(
            method,
            self.base_url + path,
            timeout=self.timeout,
            **kwargs,
        )
        if response.status_code == 204 or not response.content:
            if response.ok:
                return None
        try:
            payload = response.json() if response.content else None
        except ValueError:
            payload = response.text

        if response.status_code == 401:
            raise JiraError(
                "Jira rejected JIRA_EMAIL / JIRA_API_KEY. "
                "The token must be an API token created by that Atlassian account.",
                status_code=401,
                payload=payload,
            )
        if not response.ok:
            raise JiraError(
                _error_message(response.status_code, payload),
                status_code=response.status_code,
                payload=payload,
            )
        return payload


def _extract_list(payload: Any, keys: Sequence[str]) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _error_message(status_code: int, payload: Any) -> str:
    if isinstance(payload, dict):
        messages = [str(item) for item in payload.get("errorMessages") or [] if item]
        errors = payload.get("errors") or {}
        if isinstance(errors, dict):
            messages.extend(f"{key}: {value}" for key, value in errors.items())
        if messages:
            return f"Jira returned {status_code}: " + "; ".join(messages)
    if isinstance(payload, str) and payload.strip():
        return f"Jira returned {status_code}: {payload.strip()[:500]}"
    return f"Jira returned {status_code}."



# Wrappers 
def create_issue(
    issue_types: str | Sequence[str],
    description: str,
    summary: str | None = None,
    *,
    project_key: str = DEFAULT_PROJECT,
    jira_issue_type: str = DEFAULT_JIRA_ISSUE_TYPE,
) -> Issue:
    return JiraClient(project_key).create_issue(
        issue_types,
        description,
        summary,
        jira_issue_type=jira_issue_type,
    )


def get_issue(issue_key: str, *, project_key: str = DEFAULT_PROJECT) -> Issue:
    return JiraClient(project_key).get_issue(issue_key)


def update_issue(
    issue_key: str,
    *,
    issue_types: str | Sequence[str] | None = None,
    description: str | None = None,
    summary: str | None = None,
    jira_issue_type: str | None = None,
    project_key: str = DEFAULT_PROJECT,
) -> Issue:
    return JiraClient(project_key).update_issue(
        issue_key,
        issue_types=issue_types,
        description=description,
        summary=summary,
        jira_issue_type=jira_issue_type,
    )


def delete_issue(issue_key: str, *, project_key: str = DEFAULT_PROJECT) -> None:
    JiraClient(project_key).delete_issue(issue_key)


def add_attachment(issue_key: str, file_path: str | Path, *, project_key: str = DEFAULT_PROJECT) -> list[dict[str, Any]]:
    return JiraClient(project_key).add_attachment(issue_key, file_path)



def list_mr_mvno_options(project_key: str = DEFAULT_PROJECT) -> list[FieldOption]:
    return JiraClient(project_key).mr_mvno_options()


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Jira issue CRUD for project MSSTP")
    parser.add_argument("--project", default=DEFAULT_PROJECT, help="Project key")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("options", help="List allowed Issue Types MR MVNO values")
    sub.add_parser("jira-types", help="List Jira issue types on the project")

    create_parser = sub.add_parser("create", help="Create an issue")
    create_parser.add_argument(
        "--issue-type",
        action="append",
        required=True,
        dest="issue_types",
        help="Issue Types MR MVNO value. Repeat for more than one.",
    )
    create_parser.add_argument("--description", required=True)
    create_parser.add_argument("--summary")
    create_parser.add_argument(
        "--jira-type",
        default=DEFAULT_JIRA_ISSUE_TYPE,
        help="Jira issue type (default: Incident)",
    )

    get_parser = sub.add_parser("get", help="Read an issue")
    get_parser.add_argument("key")

    update_parser = sub.add_parser("update", help="Update an issue")
    update_parser.add_argument("key")
    update_parser.add_argument(
        "--issue-type",
        action="append",
        dest="issue_types",
        help="Replace Issue Types MR MVNO. Repeat for more than one.",
    )
    update_parser.add_argument("--description")
    update_parser.add_argument("--summary")
    update_parser.add_argument("--jira-type")

    delete_parser = sub.add_parser("delete", help="Delete an issue")
    delete_parser.add_argument("key")
    delete_parser.add_argument("--yes", action="store_true", help="Confirm deletion")

    search_parser = sub.add_parser("search", help="Search issues with JQL")
    search_parser.add_argument("--jql")
    search_parser.add_argument("--limit", type=int, default=20)

    attach_parser = sub.add_parser("attach", help="Upload a file attachment to an issue")
    attach_parser.add_argument("key", help="Jira issue key (e.g., MSSTP-123)")
    attach_parser.add_argument("file", help="Path to the file to upload")

    args = parser.parse_args(argv)
    client = JiraClient(args.project)

    if args.command == "options":
        _print_json(
            [{"id": item.id, "value": item.value} for item in client.mr_mvno_options()]
        )
    elif args.command == "attach":
            result = client.add_attachment(args.key, args.file)
            _print_json(result)
    elif args.command == "jira-types":
        _print_json(
            [
                {"id": item.id, "name": item.name, "subtask": item.subtask}
                for item in client.jira_issue_types()
            ]
        )
    elif args.command == "create":
        issue = client.create_issue(
            args.issue_types,
            args.description,
            args.summary,
            jira_issue_type=args.jira_type,
        )
        _print_json(issue.to_dict())
    elif args.command == "get":
        _print_json(client.get_issue(args.key).to_dict())
    elif args.command == "update":
        issue = client.update_issue(
            args.key,
            issue_types=args.issue_types,
            description=args.description,
            summary=args.summary,
            jira_issue_type=args.jira_type,
        )
        _print_json(issue.to_dict())
    elif args.command == "delete":
        if not args.yes:
            print("Re-run with --yes to delete this issue.", file=sys.stderr)
            return 2
        client.delete_issue(args.key)
        print(json.dumps({"deleted": args.key}))
    elif args.command == "search":
        issues = client.search_issues(args.jql, limit=args.limit)
        _print_json([issue.to_dict() for issue in issues])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (JiraError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
