import argparse
import json
import sys
import time
from pathlib import Path

import httpx

from app.auth import GoogleAuth
from app.mcp_client import StreamableHttpMcp
from app.memory import Memory
from app.paths import repo_path
from app.settings import Settings
from app.specialists import handle_message
from app.triage import run_triage
from app.workspace import (
    CALENDAR_MCP_URL,
    DOCS_MCP_URL,
    DRIVE_MCP_URL,
    GMAIL_MCP_URL,
    GoogleWorkspace,
    email_from_gmail,
    is_from_address,
)


def run_once(settings: Settings, workspace: GoogleWorkspace, memory: Memory, style_path: Path) -> dict:
    report = {"seen": 0, "skipped": 0, "recorded": [], "failures": []}
    seen: set[str] = set()
    for message_id in workspace.iter_inbox():
        if message_id in seen:
            continue
        seen.add(message_id)
        report["seen"] += 1
        if memory.get(message_id) is not None:
            report["skipped"] += 1
            continue
        try:
            message = workspace.get_message(message_id)
            sender = message.get("sender") or message.get("from") or ""
            if not isinstance(sender, str):
                sender = ""
            if settings.gmail_from and not is_from_address(sender, settings.gmail_from):
                memory.record(message_id, "ignore", "skip")
                result_route, result_disposition = "ignore", "skip"
                result_event_id = None
                result_doc_link = None
            else:
                email = email_from_gmail(message)
                decision = run_triage(email, settings)
                result = handle_message(
                    email,
                    message,
                    decision,
                    settings,
                    workspace,
                    style_path,
                )
                memory.record(
                    message_id=message_id,
                    route=result.route,
                    disposition=result.disposition,
                    event_id=result.event_id,
                    doc_link=result.doc_link,
                )
                result_route = result.route
                result_disposition = result.disposition
                result_event_id = result.event_id
                result_doc_link = result.doc_link
        except Exception as exc:
            report["failures"].append(f"{message_id}: {exc}")
            continue
        report["recorded"].append(
            {
                "messageId": message_id,
                "route": result_route,
                "disposition": result_disposition,
                "eventId": result_event_id,
                "docLink": result_doc_link,
            }
        )
    return report


def open_workspace(settings: Settings) -> tuple[GoogleWorkspace, GoogleAuth, httpx.Client]:
    auth = GoogleAuth(
        settings.google_client_id,
        settings.google_client_secret,
        repo_path(settings.google_token_path),
    )
    http = httpx.Client(timeout=60)

    def token() -> str:
        return auth.access_token()

    workspace = GoogleWorkspace(
        gmail=StreamableHttpMcp(GMAIL_MCP_URL, token, http),
        calendar=StreamableHttpMcp(CALENDAR_MCP_URL, token, http),
        drive=StreamableHttpMcp(DRIVE_MCP_URL, token, http),
        docs=StreamableHttpMcp(DOCS_MCP_URL, token, http),
        token_provider=token,
        http=http,
        query=settings.search_query(),
        sender=settings.gmail_from,
    )
    return workspace, auth, http


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read newsroom Gmail and run the calendar and article specialists."
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Wait JOB_INTERVAL_SECONDS after each pass. The default wait is 30 minutes.",
    )
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    style_path = repo_path(settings.house_style_path)
    memory = Memory(repo_path(settings.memory_path))
    try:
        workspace, auth, http = open_workspace(settings)
    except Exception as exc:
        print(exc, file=sys.stderr)
        memory.close()
        return 1
    try:
        while True:
            try:
                report = run_once(settings, workspace, memory, style_path)
            except Exception as exc:
                report = {"seen": 0, "skipped": 0, "recorded": [], "failures": [str(exc)]}
            print(json.dumps(report, indent=2, ensure_ascii=False))
            if report["failures"]:
                print("\n".join(report["failures"]), file=sys.stderr)
            if not args.loop:
                return 1 if report["failures"] else 0
            time.sleep(settings.job_interval_seconds)
    finally:
        workspace.close()
        auth.close()
        http.close()
        memory.close()


if __name__ == "__main__":
    raise SystemExit(main())
