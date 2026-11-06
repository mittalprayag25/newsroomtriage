from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from app.attachments import AttachmentError, extract_text
from app.draft import draft_article
from app.mcp_client import resource_id
from app.models import InboundEmail, TriageOutput
from app.settings import Settings
from app.workspace import GoogleWorkspace


class Hold(Exception):
    """The route was calendar, but there is no start time to book."""


@dataclass(frozen=True)
class ActionResult:
    route: str
    disposition: str
    event_id: str | None = None
    doc_link: str | None = None


def handle_message(
    email: InboundEmail,
    gmail_message: dict,
    decision: TriageOutput,
    settings: Settings,
    workspace: GoogleWorkspace,
    style_path: Path,
) -> ActionResult:
    if decision.disposition != "dispatch":
        return ActionResult(decision.route, decision.disposition)
    if decision.route == "calendar":
        return _book(email, decision, workspace)
    if decision.route == "article":
        return _file(email, gmail_message, decision, settings, workspace, style_path)
    return ActionResult(decision.route, decision.disposition)


def event_arguments(email: InboundEmail, decision: TriageOutput) -> dict:
    fields = decision.fields
    start = fields.get("start")
    if not isinstance(start, str) or not start.strip():
        raise Hold("No start time.")
    end = fields.get("end")
    if not isinstance(end, str) or not end.strip():
        end = _hour_later(start)
    title = fields.get("title") if isinstance(fields.get("title"), str) else ""
    summary = title.strip() or email.subject.strip() or "Newsroom event"
    arguments = {
        "summary": summary[:200],
        "startTime": start,
        "endTime": end,
        "timeZone": "Asia/Singapore",
        "calendarId": "primary",
        "notificationLevel": "NONE",
        "description": f"Gmail message {email.message_id}\nFrom: {email.from_address}",
    }
    location = fields.get("location")
    if isinstance(location, str) and location.strip():
        arguments["location"] = location.strip()[:300]
    return arguments


def collect_attachment_text(message: dict, workspace: GoogleWorkspace) -> str:
    """Read PDF and Word bytes before the draft agent runs."""
    parts: list[str] = []
    message_id = message.get("id") if isinstance(message.get("id"), str) else ""
    for item in message.get("attachments") or []:
        if not isinstance(item, dict):
            continue
        filename = item.get("filename") if isinstance(item.get("filename"), str) else "attachment"
        lower = filename.lower()
        if not lower.endswith((".pdf", ".docx", ".doc")):
            continue
        attachment_id = item.get("id")
        if not isinstance(attachment_id, str) or not attachment_id:
            parts.append(f"{filename}: no attachment id was returned.")
            continue
        data = workspace.attachment_bytes(message_id, attachment_id)
        try:
            text = extract_text(filename, data)
        except AttachmentError as exc:
            parts.append(f"{filename}: {exc}")
            continue
        if text.strip():
            parts.append(f"{filename}\n{text.strip()}")
        else:
            parts.append(f"{filename}: no text found.")
    return "\n\n".join(parts)


def _book(email: InboundEmail, decision: TriageOutput, workspace: GoogleWorkspace) -> ActionResult:
    try:
        arguments = event_arguments(email, decision)
    except Hold:
        return ActionResult("calendar", "review")
    event = workspace.create_event(arguments)
    return ActionResult("calendar", "dispatch", event_id=resource_id(event))


def _file(
    email: InboundEmail,
    gmail_message: dict,
    decision: TriageOutput,
    settings: Settings,
    workspace: GoogleWorkspace,
    style_path: Path,
) -> ActionResult:
    attachment_text = collect_attachment_text(gmail_message, workspace)
    draft = draft_article(email, decision, attachment_text, settings, style_path)
    link = workspace.create_document(draft.headline, draft.text)
    return ActionResult("article", "dispatch", doc_link=link)


def _hour_later(start: str) -> str:
    try:
        parsed = datetime.fromisoformat(start)
    except ValueError as exc:
        raise Hold("Start time was not a date.") from exc
    return (parsed + timedelta(hours=1)).isoformat()
