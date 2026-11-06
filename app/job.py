import argparse
import json
import sys
import time
from pathlib import Path

import httpx

from app.auth import GoogleAuth
from app.google_rest import GoogleRestWorkspace
from app.memory import Memory
from app.paths import repo_path
from app.settings import Settings
from app.specialists import handle_message
from app.triage import run_triage
from app.workspace import GoogleWorkspace, email_from_gmail, is_from_address


def send_pending_replies(settings, workspace, memory, report, only_message_id=None):
    if not hasattr(workspace, "send_doc_reply"):
        return
    for reply in memory.pending_replies():
        message_id = reply["message_id"]
        if only_message_id and message_id != only_message_id:
            continue
        sending = False
        try:
            message = workspace.get_message(message_id)
            if settings.gmail_from and not is_from_address(message.get("sender", ""), settings.gmail_from):
                raise ValueError("Reply sender does not match GMAIL_FROM.")
            memory.reply_state(message_id, "sending")
            sending = True
            sent = workspace.send_doc_reply(message, reply["doc_link"])
            memory.reply_state(message_id, "sent", sent["id"])
            report.setdefault("replies", []).append({"messageId": message_id, "sentMessageId": sent["id"]})
        except Exception as exc:
            # A definitive HTTP rejection can be retried. Timeouts or a crash
            # after submitting a send require checking Sent before retrying.
            cause = exc.__cause__
            if not sending or (isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code < 500):
                memory.reply_state(message_id, "pending")
            report["failures"].append(f"{message_id}: document reply failed: {exc}")


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
    send_pending_replies(settings, workspace, memory, report)
    if not report["failures"] and hasattr(workspace, "commit_poll"):
        workspace.commit_poll()
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

    memory = Memory(repo_path(settings.memory_path))
    workspace = GoogleRestWorkspace(token, http, memory, sender=settings.gmail_from)
    return workspace, auth, http


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read newsroom Gmail and run the calendar and article specialists."
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Wait JOB_INTERVAL_SECONDS after each pass. The default wait is 30 seconds.",
    )
    parser.add_argument("--reply-doc", metavar="MESSAGE_ID", help="Reply with a previously saved article document; does not scan the inbox")
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
        if args.reply_doc:
            memory.queue_doc_reply(args.reply_doc)
            report = {"failures": []}
            send_pending_replies(settings, workspace, memory, report, only_message_id=args.reply_doc)
            print(json.dumps(report, indent=2))
            return 1 if report["failures"] else 0
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
        workspace.memory.close()
        auth.close()
        http.close()
        memory.close()


if __name__ == "__main__":
    raise SystemExit(main())
