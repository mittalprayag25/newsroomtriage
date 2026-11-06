# Straits Times triage

Local service that reads one email and returns a triage decision. It does not create calendar events or Google Docs.

`dispatch` means confidence is at least 0.75 and a later specialist would run. `review` means a person should look. `skip` means ignore it.

## Run

```bash
docker compose build
docker compose up
```

Open http://localhost:8080, paste an email, and attach a PDF if it has one. The file has to start with a real PDF header and stay under 15 MB.

Triage every fixture from the image:

```bash
docker compose run --rm triage python -m app.cli --all
```

The default image is offline. Decisions come from the rules in `app/heuristic.py`.

## Optional model

Copy `.env.example` to `.env` and set an OpenAI-compatible endpoint. `TRIAGE_MODE=auto` uses the model only when `TRIAGE_BASE_URL` or `TRIAGE_API_KEY` is set.

Ollama on the host:

```bash
TRIAGE_BASE_URL=http://host.docker.internal:11434/v1
TRIAGE_MODEL=llama3.2
TRIAGE_API_KEY=ollama
```

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

Clock times in the rules engine are read as Asia/Singapore (`+08:00`).
