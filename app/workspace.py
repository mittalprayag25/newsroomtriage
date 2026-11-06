import base64
from urllib.parse import quote

import httpx

from app.mcp_client import (
    StreamableHttpMcp,
    document_link,
    resource_id,
)
from app.models import InboundEmail

GMAIL_MCP_URL = "https://gmailmcp.googleapis.com/mcp/v1"
CALENDAR_MCP_URL = "https://calendarmcp.googleapis.com/mcp/v1"
DOCS_MCP_URL = "https://docsmcp.googleapis.com/mcp/v1"
# Docs MCP edits a document. It has no create tool, so the empty Google Doc
# is created on Drive MCP and then filled with docs.update_doc.
DRIVE_MCP_URL = "https://drivemcp.googleapis.com/mcp/v1"

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
MAX_PAGES = 5


class WorkspaceError(Exception):
    pass


class GoogleWorkspace:
    """Mail, calendar, and docs tools the specialists call."""

    def __init__(
        self,
        gmail: StreamableHttpMcp,
        calendar: StreamableHttpMcp,
        drive: StreamableHttpMcp,
        docs: StreamableHttpMcp,
        token_provider,
        http: httpx.Client,
        query: str,
    ):
        self.gmail = gmail
        self.calendar = calendar
        self.drive = drive
        self.docs = docs
        self._token = token_provider
        self._http = http
        self.query = query

    def iter_inbox(self) -> list[str]:
        ids: list[str] = []
        seen: set[str] = set()
        page_token = ""
        for _ in range(MAX_PAGES):
            arguments: dict = {"query": self.query, "pageSize": 20}
            if page_token:
                arguments["pageToken"] = page_token
            page = self.gmail.call_tool("search_threads", arguments)
            for thread in page.get("threads") or []:
                if not isinstance(thread, dict):
                    continue
                messages = thread.get("messages") or []
                if not messages and thread.get("id"):
                    fetched = self.gmail.call_tool(
                        "get_thread",
                        {"threadId": thread["id"], "messageFormat": "MINIMAL"},
                    )
                    messages = fetched.get("messages") or []
                for message in messages:
                    if not isinstance(message, dict):
                        continue
                    message_id = message.get("id")
                    if isinstance(message_id, str) and message_id and message_id not in seen:
                        seen.add(message_id)
                        ids.append(message_id)
            page_token = page.get("nextPageToken") if isinstance(page.get("nextPageToken"), str) else ""
            if not page_token:
                break
        return ids

    def get_message(self, message_id: str) -> dict:
        payload = self.gmail.call_tool(
            "get_message",
            {"messageId": message_id, "messageFormat": "FULL_CONTENT"},
        )
        return as_gmail_message(payload, message_id)

    def attachment_bytes(self, message_id: str, attachment_id: str) -> bytes:
        # get_message returns attachment ids and filenames, not the file bytes.
        url = (
            f"{GMAIL_API}/messages/{quote(message_id, safe='')}"
            f"/attachments/{quote(attachment_id, safe='')}"
        )
        try:
            response = self._http.get(url, headers={"Authorization": f"Bearer {self._token()}"})
        except httpx.HTTPError as exc:
            raise WorkspaceError(f"Attachment download failed: {exc}") from exc
        if response.status_code >= 400:
            raise WorkspaceError(f"Attachment download returned HTTP {response.status_code}.")
        try:
            encoded = response.json().get("data")
        except ValueError as exc:
            raise WorkspaceError("Attachment download was not JSON.") from exc
        if not isinstance(encoded, str) or not encoded:
            raise WorkspaceError("Attachment download had no data.")
        padding = "=" * (-len(encoded) % 4)
        try:
            return base64.urlsafe_b64decode(encoded + padding)
        except ValueError as exc:
            raise WorkspaceError("Attachment data was not base64.") from exc

    def create_event(self, arguments: dict) -> dict:
        return self.calendar.call_tool("create_event", arguments)

    def create_document(self, title: str, text: str) -> str:
        created = self.drive.call_tool(
            "create_file",
            {
                "title": title[:200],
                "contentMimeType": "application/vnd.google-apps.document",
            },
        )
        document_id = resource_id(created)
        self.docs.call_tool(
            "update_doc",
            {
                "documentId": document_id,
                "requests": [
                    {
                        "insertText": {
                            "text": text,
                            "endOfSegmentLocation": {},
                        }
                    }
                ],
            },
        )
        return document_link(created, document_id)

    def close(self) -> None:
        for client in (self.gmail, self.calendar, self.drive, self.docs):
            client.close()


def as_gmail_message(payload: dict, message_id: str) -> dict:
    message = payload
    nested = payload.get("message")
    if isinstance(nested, dict) and nested.get("id"):
        message = nested
    if not message.get("id"):
        message = dict(message)
        message["id"] = message_id
    return message


def email_from_gmail(message: dict) -> InboundEmail:
    names: list[str] = []
    for item in message.get("attachments") or []:
        if isinstance(item, dict):
            filename = item.get("filename") or ""
            if isinstance(filename, str) and filename:
                names.append(filename[:200])
    sender = message.get("sender") or message.get("from") or ""
    if not isinstance(sender, str):
        sender = ""
    subject = message.get("subject") or ""
    if not isinstance(subject, str):
        subject = ""
    body = message.get("plaintextBody") or message.get("snippet") or ""
    if not isinstance(body, str):
        body = ""
    return InboundEmail.model_validate(
        {
            "messageId": str(message.get("id") or "")[:200],
            "from": sender[:320],
            "subject": subject[:500],
            "body": body[:20000],
            "attachmentNames": names[:20],
        }
    )
