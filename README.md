# Straits Times newsroom agent

Triage reads one email and chooses calendar, article, enquiry, or ignore. The page at http://localhost:8080 stops there.

The 30-second job is the part that acts. It reads Gmail, skips any message id already in SQLite, and runs a specialist when triage says `dispatch`.

- Calendar calls `create_event` on the Google Calendar API and stores the event id.
- Article turns PDF and Word attachments into text, drafts against `house-style.md`, creates a Google Doc, and stores the doc link.
- Enquiry, ignore, and review are recorded so the next pass does not do them again. They do not book or file.

`dispatch` means confidence is at least 0.75. `review` means a person should look. `skip` means ignore it.

## Triage

```bash
docker compose build
docker compose up
```

Open http://localhost:8080, paste an email, and attach a PDF if it has one. The file has to start with a real PDF header and stay under 15 MB. That upload is only for the triage page. The job reads attachments from Gmail.

Triage every fixture:

```bash
docker compose run --rm triage python -m app.cli --all
```

The default image is offline. Decisions come from the rules in `app/heuristic.py`.

Copy `.env.example` to `.env` and set a SEA-LION trial key to use the model. Triage and the draft both call `aisingapore/Qwen-SEA-LION-v4.5-27B-IT` at `https://api.sea-lion.ai/v1`. `TRIAGE_MODE=llm` keeps a failed model call visible. Set `TRIAGE_MODE=heuristic` to use the phrase rules, and a plain sourced draft, instead.

Clock times in the rules engine are read as Asia/Singapore (`+08:00`).

## Google sign-in (personal Gmail or Workspace)

The job uses the standard Gmail, Calendar, and Docs REST APIs. No Google MCP services or Developer Preview enrollment are required.

1. In your Google Cloud project, enable **Gmail API**, **Google Calendar API**, and **Google Docs API**.
2. Configure Google Auth Platform with an External audience for personal Gmail. While in Testing, add your personal Gmail account under Audience > Test users.
3. Create a **Web application** OAuth client for this standard API integration, with authorized redirect URI `http://127.0.0.1:8765/oauth/callback`. The AI-powered agent MCP setting is not required.
4. Put `GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET` in `.env` (do not commit credentials).
5. Activate `.venv` and run `python -m app.auth`. Choose your **personal Gmail account** in the Google sign-in window. The command saves a refresh token in `data/google-token.json`.

The current specialists request `gmail.readonly`, `gmail.send`, `calendar.events`, `documents`, and `drive.file`. Gmail reads new incoming messages and sends article document links back to the sender; calendar and document permissions allow the configured specialists to book and draft. An existing token can be reused if it belongs to the intended account and has these scopes. Reauthorize when changing accounts or OAuth clients. Testing-mode OAuth grants may require periodic sign-in again.

## The job

One pass:

```bash
python -m app.job
```

A pass every 30 seconds, in the same process:

```bash
python -m app.job --loop
```

Or in Docker, after `python -m app.auth` has written `data/google-token.json`:

```bash
docker compose --profile job up
```

On the first successful connection for an account, the worker saves its current Gmail history checkpoint and skips all existing messages. Later passes read only newly added inbox messages, excluding sent mail and drafts. Checkpoints persist in `data/newsroom.sqlite` across restarts. An expired checkpoint holds processing with an error instead of scanning old mail. Run only one worker per database.

The existing `GMAIL_FROM` sender restriction remains (default `markcwm@sph.com.sg`); new arrivals from other senders are recorded as skipped. `GMAIL_QUERY` is a legacy MCP setting and is not used by history-based monitoring. Successful passes advance the checkpoint; failed messages are retried and already-recorded messages are skipped.

Each handled message becomes one row in `data/newsroom.sqlite`: the Gmail message id, the route, and either the calendar event id or the doc link.

Calendar events are created on the signed-in user's primary calendar with the email sender and To/CC recipients as attendees and `sendUpdates=all`, so Google sends guests calendar invitations. Addresses are deduplicated without regard to case; the connected mailbox address is excluded. BCC recipients and addresses mentioned in the body are not added. The sender is also written in the event description.

Article drafts follow `house-style.md`. Edit that file to change the house style. The draft agent reads it on every story. Without a model key, the job still files a short sourced draft and the style file has to be present.

## Email JSON

```json
{
  "messageId": "local-interview",
  "from": "press@mnd.gov.sg",
  "subject": "Interview request",
  "body": "Friday 7 November 2026, 3:00 pm to 3:45 pm, at Parliament House.",
  "attachmentNames": []
}
```

## Article link replies

After an article draft is saved, the job replies to the original sender with its Google Doc link in the original Gmail thread. To/CC recipients are not copied. This does not change the document's sharing permissions.

Existing sign-ins must grant the new `https://www.googleapis.com/auth/gmail.send` scope: add it in Google Auth Platform > Data Access if needed, stop the worker, and run `python -m app.auth` again. Then restart the worker. Replies are queued independently of article creation, so a rejected send retries the saved link without creating another document. Successful sends are recorded and not repeated. If a send times out or the process stops during sending, its state stays `sending`: check Gmail Sent before manually retrying, as delivery may have succeeded.

To reply with a previously recorded document without recreating it:

```bash
python -m app.job --reply-doc GMAIL_MESSAGE_ID
```

Only explicitly queued historical messages are replied to; old article rows are not automatically backfilled. Run just one worker at a time.
