# Straits Times newsroom agent

Triage reads one email and chooses calendar, article, enquiry, or ignore. The page at http://localhost:8080 stops there.

The half-hour job is the part that acts. It reads Gmail, skips any message id already in SQLite, and runs a specialist when triage says `dispatch`.

- Calendar calls `create_event` on the Google Calendar remote MCP server and stores the event id.
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

## Google sign-in

The job calls Google's remote MCP servers over Streamable HTTP with OAuth 2.0. The servers are in the Google Workspace Developer Preview, so the Google account has to be accepted into that program.

- Gmail: `https://gmailmcp.googleapis.com/mcp/v1`
- Calendar: `https://calendarmcp.googleapis.com/mcp/v1`
- Docs: `https://docsmcp.googleapis.com/mcp/v1`

Docs can edit a document and cannot create one, so the article specialist creates the empty Google Doc with Drive MCP (`https://drivemcp.googleapis.com/mcp/v1`) and then calls `update_doc`. Gmail's `get_message` tool returns attachment ids, not file bytes. The bytes are downloaded with the same access token, then `pypdf` and `mammoth` turn PDF and `.docx` files into text before the draft runs.

In the Google Cloud project, enable the Gmail, Calendar, Docs, and Drive APIs, and the matching MCP services `gmailmcp.googleapis.com`, `calendarmcp.googleapis.com`, `docsmcp.googleapis.com`, and `drivemcp.googleapis.com`.

Create a Web application OAuth client and select **Use this client for an AI-powered agent**. Add this redirect URI:

```text
http://127.0.0.1:8765/oauth/callback
```

Add these scopes on the consent screen:

- `https://www.googleapis.com/auth/gmail.readonly`
- `https://www.googleapis.com/auth/calendar.events`
- `https://www.googleapis.com/auth/documents`
- `https://www.googleapis.com/auth/drive.file`

`calendar.events` is the write scope. The readonly calendar scopes are not enough to book.

Put the client id and secret in `.env`, then sign in once on the machine that will run the job:

```bash
python -m app.auth
```

The authorization URL sends `access_type=offline` and `prompt=consent`. Google returns a refresh token only when `access_type=offline` is present. The token is written to `data/google-token.json` and is not committed. The half-hour job refreshes the access token from it, so it stays signed in.

## The job

One pass:

```bash
python -m app.job
```

A pass every 30 minutes, in the same process:

```bash
python -m app.job --loop
```

Or in Docker, after `python -m app.auth` has written `data/google-token.json`:

```bash
docker compose --profile job up
```

The job reads Gmail as the signed-in user, but it only searches for mail from `markcwm@sph.com.sg`. The default search is `in:inbox newer_than:2d from:markcwm@sph.com.sg`. `GMAIL_QUERY` can change the mailbox window. It cannot remove the sender. A message from anyone else is stored as skip and is not booked or drafted.

Each handled message becomes one row in `data/newsroom.sqlite`: the Gmail message id, the route, and either the calendar event id or the doc link.

Calendar events are created on the signed-in user's primary calendar with `notificationLevel` set to `NONE`, so the job does not email guests. The sender is written in the event description.

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
