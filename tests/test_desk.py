import base64
import json
import stat
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlparse

import httpx

from app.attachments import AttachmentError, extract_text
from app.auth import GoogleAuth, authorization_url, refresh_form
from app.draft import draft_article, messages_for_model
from app.job import run_once
from app.mcp_client import StreamableHttpMcp, parse_mcp_body
from app.memory import Memory, MemoryError
from app.models import InboundEmail
from app.settings import Settings
from app.specialists import event_arguments
from app.style import StyleError, load_house_style
from app.triage import run_triage
from app.workspace import (
    GMAIL_API,
    GoogleWorkspace,
    email_from_gmail,
)


ROOT = Path(__file__).resolve().parents[1]


def minimal_pdf(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 24 Tf 72 700 Td ({escaped}) Tj ET".encode("ascii")
    header = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
    objects = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n",
        b"4 0 obj\n<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream\nendobj\n",
        b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
    ]
    chunks = [header]
    offsets = [0]
    cursor = len(header)
    for obj in objects:
        offsets.append(cursor)
        chunks.append(obj)
        cursor += len(obj)
    xref = [b"xref\n0 6\n", b"0000000000 65535 f \n"]
    for offset in offsets[1:]:
        xref.append(f"{offset:010d} 00000 n \n".encode("ascii"))
    trailer = f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{cursor}\n%%EOF\n".encode("ascii")
    return b"".join(chunks + xref + [trailer])


def minimal_docx(text: str) -> bytes:
    document = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body>
</w:document>"""
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


class AttachmentTests(unittest.TestCase):
    def test_pdf_and_docx_become_text(self) -> None:
        self.assertIn("Tengah launch note", extract_text("hdb-release.pdf", minimal_pdf("Tengah launch note")))
        self.assertIn("5,000 flats", extract_text("note.docx", minimal_docx("The launch covers 5,000 flats.")))

    def test_legacy_doc_is_rejected(self) -> None:
        with self.assertRaises(AttachmentError):
            extract_text("note.doc", b"not a docx")


class MemoryTests(unittest.TestCase):
    def test_one_row_per_message_holds_event_or_doc(self) -> None:
        with TemporaryDirectory() as folder:
            memory = Memory(Path(folder) / "newsroom.sqlite")
            memory.record("msg-1", "calendar", "dispatch", event_id="evt-1")
            memory.record("msg-2", "article", "dispatch", doc_link="https://docs.google.com/document/d/doc-1/edit")
            self.assertEqual(memory.get("msg-1")["event_id"], "evt-1")
            self.assertIsNone(memory.get("msg-1")["doc_link"])
            self.assertEqual(memory.get("msg-2")["doc_link"], "https://docs.google.com/document/d/doc-1/edit")
            self.assertIsNone(memory.get("msg-2")["event_id"])
            with self.assertRaises(MemoryError):
                memory.record("msg-1", "calendar", "dispatch", event_id="evt-2")
            memory.close()


class AuthTests(unittest.TestCase):
    def test_authorization_asks_for_a_refresh_token(self) -> None:
        url = authorization_url("client", "http://127.0.0.1:8765/oauth/callback", "state-1")
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["access_type"], ["offline"])
        self.assertEqual(query["prompt"], ["consent"])
        self.assertEqual(query["response_type"], ["code"])
        self.assertIn("gmail.readonly", query["scope"][0])
        self.assertIn("calendar.events", query["scope"][0])

    def test_refresh_keeps_the_job_signed_in(self) -> None:
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.content.decode())
            return httpx.Response(200, json={"access_token": "ya29.test", "expires_in": 3600})

        with TemporaryDirectory() as folder:
            path = Path(folder) / "google-token.json"
            path.write_text(json.dumps({"refresh_token": "refresh-1"}), encoding="utf-8")
            auth = GoogleAuth(
                "client",
                "secret",
                path,
                http=httpx.Client(transport=httpx.MockTransport(handler)),
            )
            self.assertEqual(auth.access_token(), "ya29.test")
            self.assertEqual(auth.access_token(), "ya29.test")
            auth.close()
        self.assertEqual(len(calls), 1)
        self.assertIn("grant_type=refresh_token", calls[0])
        self.assertEqual(
            refresh_form("client", "secret", "refresh-1")["grant_type"],
            "refresh_token",
        )


class McpTests(unittest.TestCase):
    def test_streamable_http_uses_the_session_and_structured_result(self) -> None:
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            seen.append((body["method"], request.headers.get("mcp-session-id"), request.headers.get("authorization")))
            if body["method"] == "initialize":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {"protocolVersion": "2025-03-26", "capabilities": {}},
                    },
                    headers={"mcp-session-id": "sess-1"},
                )
            if body["method"] == "notifications/initialized":
                return httpx.Response(202)
            sse = (
                'event: message\n'
                'data: {"jsonrpc":"2.0","id":2,"result":'
                '{"structuredContent":{"id":"evt-1"},"isError":false}}\n\n'
            )
            return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

        client = StreamableHttpMcp(
            "https://calendarmcp.googleapis.com/mcp/v1",
            lambda: "ya",
            httpx.Client(transport=httpx.MockTransport(handler)),
        )
        self.assertEqual(client.call_tool("create_event", {"summary": "Interview"}), {"id": "evt-1"})
        client.close()
        self.assertEqual(seen[0][0], "initialize")
        self.assertEqual(seen[1][0], "notifications/initialized")
        self.assertEqual(seen[1][1], "sess-1")
        self.assertEqual(seen[2][0], "tools/call")
        self.assertEqual(seen[2][1], "sess-1")
        self.assertEqual(seen[2][2], "Bearer ya")

    def test_a_dropped_connection_retries_the_handshake(self) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise httpx.ConnectError("down")
            body = json.loads(request.content)
            if body["method"] == "initialize":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {"protocolVersion": "2025-03-26", "capabilities": {}},
                    },
                )
            if body["method"] == "notifications/initialized":
                return httpx.Response(202)
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"structuredContent": {"id": "ok"}, "isError": False},
                },
            )

        client = StreamableHttpMcp(
            "https://gmailmcp.googleapis.com/mcp/v1",
            lambda: "ya",
            httpx.Client(transport=httpx.MockTransport(handler)),
        )
        with self.assertRaises(Exception):
            client.call_tool("search_threads", {"query": "in:inbox"})
        self.assertEqual(client.call_tool("search_threads", {"query": "in:inbox"}), {"id": "ok"})
        client.close()

    def test_sse_json_rpc_error_is_visible(self) -> None:
        message = parse_mcp_body(
            "text/event-stream",
            'data: {"jsonrpc":"2.0","id":1,"error":{"message":"denied"}}\n\n',
        )
        self.assertEqual(message["error"]["message"], "denied")


class ToolTests(unittest.TestCase):
    def test_specialists_call_calendar_docs_and_download_bytes(self) -> None:
        calls = []

        class FakeMcp:
            def __init__(self, name: str):
                self.name = name

            def call_tool(self, tool: str, arguments: dict) -> dict:
                calls.append((self.name, tool, arguments))
                if tool == "search_threads":
                    return {"threads": [{"id": "thread-1", "messages": [{"id": "msg-1"}]}]}
                if tool == "get_message":
                    return {
                        "id": "msg-1",
                        "sender": "media@hdb.gov.sg",
                        "subject": "Release",
                        "plaintextBody": "Body",
                        "attachments": [{"id": "att-1", "filename": "hdb-release.pdf", "mimeType": "application/pdf"}],
                    }
                if tool == "create_file":
                    return {"id": "doc-9", "webViewLink": "https://docs.google.com/document/d/doc-9/edit"}
                if tool == "update_doc":
                    return {"replies": []}
                if tool == "create_event":
                    return {"id": "evt-9"}
                return {}

            def close(self) -> None:
                return None

        pdf = minimal_pdf("Tengah launch note")

        def download(request: httpx.Request) -> httpx.Response:
            self.assertTrue(request.url.path.endswith("/messages/msg-1/attachments/att-1"))
            self.assertEqual(request.headers["authorization"], "Bearer ya")
            encoded = base64.urlsafe_b64encode(pdf).decode("ascii")
            return httpx.Response(200, json={"size": len(pdf), "data": encoded})

        workspace = GoogleWorkspace(
            gmail=FakeMcp("gmail"),
            calendar=FakeMcp("calendar"),
            drive=FakeMcp("drive"),
            docs=FakeMcp("docs"),
            token_provider=lambda: "ya",
            http=httpx.Client(transport=httpx.MockTransport(download)),
            query="in:inbox newer_than:2d",
        )
        self.assertEqual(workspace.iter_inbox(), ["msg-1"])
        message = workspace.get_message("msg-1")
        data = workspace.attachment_bytes("msg-1", "att-1")
        self.assertTrue(data.startswith(b"%PDF"))
        link = workspace.create_document("HDB launches flats", "HDB launches flats\n\nSINGAPORE - Tengah.\n")
        event = workspace.create_event({"summary": "Interview", "notificationLevel": "NONE"})
        self.assertEqual(link, "https://docs.google.com/document/d/doc-9/edit")
        self.assertEqual(event["id"], "evt-9")
        self.assertEqual(calls[0][0:2], ("gmail", "search_threads"))
        self.assertEqual(calls[0][2]["query"], "in:inbox newer_than:2d")
        self.assertEqual(calls[1][0:2], ("gmail", "get_message"))
        self.assertEqual(calls[2][0:2], ("drive", "create_file"))
        self.assertEqual(calls[2][2]["contentMimeType"], "application/vnd.google-apps.document")
        self.assertEqual(calls[3][0:2], ("docs", "update_doc"))
        self.assertEqual(calls[3][2]["documentId"], "doc-9")
        self.assertIn("insertText", calls[3][2]["requests"][0])
        self.assertEqual(calls[4][0:2], ("calendar", "create_event"))
        self.assertEqual(message["attachments"][0]["filename"], "hdb-release.pdf")
        self.assertTrue(str(httpx.URL(GMAIL_API)).startswith("https://gmail.googleapis.com/"))


class DraftTests(unittest.TestCase):
    def test_article_agent_reads_the_house_style(self) -> None:
        style = load_house_style(ROOT / "house-style.md")
        self.assertIn("Sentence-case headlines", style)
        email = InboundEmail.model_validate(
            {
                "messageId": "local-press-release",
                "from": "media@hdb.gov.sg",
                "subject": "FOR IMMEDIATE RELEASE: HDB launches 5,000 flats in Tengah",
                "body": "For immediate release.\n\nHDB will launch 5,000 flats in Tengah next month.",
                "attachmentNames": ["hdb-release.pdf"],
            }
        )
        decision = run_triage(email, Settings(mode="heuristic"))
        messages = messages_for_model(style, email, decision, "Tengah launch note")
        self.assertIn("Sentence-case headlines", messages[0]["content"])
        self.assertIn("Tengah launch note", messages[1]["content"])
        draft = draft_article(email, decision, "Tengah launch note", Settings(mode="heuristic"), ROOT / "house-style.md")
        self.assertIn("Tengah launch note", draft.text)
        self.assertIn("SINGAPORE -", draft.text)
        with self.assertRaises(StyleError):
            draft_article(email, decision, "", Settings(mode="heuristic"), Path("/tmp/missing-house-style.md"))


class JobTests(unittest.TestCase):
    def test_dispatch_books_or_files_once(self) -> None:
        interview = json.loads((ROOT / "fixtures" / "interview.json").read_text(encoding="utf-8"))
        release = json.loads((ROOT / "fixtures" / "press-release.json").read_text(encoding="utf-8"))
        newsletter = json.loads((ROOT / "fixtures" / "newsletter.json").read_text(encoding="utf-8"))
        pdf = minimal_pdf("Tengah launch note")
        messages = {
            interview["messageId"]: {
                "id": interview["messageId"],
                "sender": interview["from"],
                "subject": interview["subject"],
                "plaintextBody": interview["body"],
                "attachments": [],
            },
            release["messageId"]: {
                "id": release["messageId"],
                "sender": release["from"],
                "subject": release["subject"],
                "plaintextBody": release["body"],
                "attachments": [{"id": "att-pdf", "filename": "hdb-release.pdf"}],
            },
            newsletter["messageId"]: {
                "id": newsletter["messageId"],
                "sender": newsletter["from"],
                "subject": newsletter["subject"],
                "plaintextBody": newsletter["body"],
                "attachments": [],
            },
        }

        class Desk:
            def __init__(self) -> None:
                self.calls: list[tuple] = []

            def iter_inbox(self) -> list[str]:
                return list(messages)

            def get_message(self, message_id: str) -> dict:
                self.calls.append(("get_message", message_id))
                return messages[message_id]

            def attachment_bytes(self, message_id: str, attachment_id: str) -> bytes:
                self.calls.append(("attachment_bytes", message_id, attachment_id))
                return pdf

            def create_event(self, arguments: dict) -> dict:
                self.calls.append(("create_event", arguments))
                return {"id": "evt-interview"}

            def create_document(self, title: str, text: str) -> str:
                self.calls.append(("create_document", title, text))
                return "https://docs.google.com/document/d/doc-release/edit"

        desk = Desk()
        settings = Settings(mode="heuristic")
        with TemporaryDirectory() as folder:
            memory = Memory(Path(folder) / "newsroom.sqlite")
            first = run_once(settings, desk, memory, ROOT / "house-style.md")
            second = run_once(settings, desk, memory, ROOT / "house-style.md")
            stored = {row["messageId"]: row for row in first["recorded"]}
            memory.close()

        self.assertEqual(first["failures"], [])
        self.assertEqual(second["skipped"], 3)
        self.assertEqual(second["recorded"], [])
        self.assertEqual(stored[interview["messageId"]]["eventId"], "evt-interview")
        self.assertIsNone(stored[interview["messageId"]]["docLink"])
        self.assertEqual(
            stored[release["messageId"]]["docLink"],
            "https://docs.google.com/document/d/doc-release/edit",
        )
        self.assertIsNone(stored[newsletter["messageId"]]["eventId"])
        self.assertIsNone(stored[newsletter["messageId"]]["docLink"])
        self.assertEqual(stored[newsletter["messageId"]]["disposition"], "skip")

        names = [call[0] for call in desk.calls]
        release_bytes = names.index("attachment_bytes")
        release_doc = names.index("create_document")
        self.assertLess(release_bytes, release_doc)
        doc_call = next(call for call in desk.calls if call[0] == "create_document")
        self.assertIn("Tengah launch note", doc_call[2])
        event_call = next(call for call in desk.calls if call[0] == "create_event")
        self.assertEqual(event_call[1]["notificationLevel"], "NONE")
        self.assertNotIn("attendees", event_call[1])
        self.assertEqual(event_call[1]["startTime"], "2026-11-07T15:00:00+08:00")
        self.assertEqual(len([call for call in desk.calls if call[0] == "create_event"]), 1)

    def test_calendar_event_does_not_invite_guests(self) -> None:
        email = email_from_gmail(
            {
                "id": "local-interview",
                "sender": "press@mnd.gov.sg",
                "subject": "Interview request: Minister Wong on the housing bill",
                "plaintextBody": "Friday 7 November 2026, 3:00 pm to 3:45 pm, at Parliament House.",
            }
        )
        decision = run_triage(email, Settings(mode="heuristic"))
        arguments = event_arguments(email, decision)
        self.assertEqual(arguments["notificationLevel"], "NONE")
        self.assertNotIn("attendees", arguments)
        self.assertEqual(arguments["location"], "Parliament House")


class TokenFileTests(unittest.TestCase):
    def test_refresh_token_file_is_not_world_readable(self) -> None:
        from app.auth import save_refresh_token

        with TemporaryDirectory() as folder:
            path = Path(folder) / "google-token.json"
            save_refresh_token(path, "refresh-1", "client")
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode & 0o077, 0)


if __name__ == "__main__":
    unittest.main()
