import json
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from app.llm import LlmError
from app.models import REVIEW_THRESHOLD, InboundEmail, TriageOutput
from app.paths import project_dir
from app.settings import Settings
from app.triage import run_triage

MAX_PDF_BYTES = 15 * 1024 * 1024
PDF_MAGIC = b"%PDF"


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(
        title="Straits Times triage",
        version="0.2.0",
        summary="Classifies one email into calendar, article, enquiry, or ignore.",
    )
    app.state.settings = settings or Settings.from_env()

    @app.get("/health")
    def health() -> dict:
        current: Settings = app.state.settings
        payload = {
            "status": "ok",
            "engine": current.engine(),
            "mode": current.mode,
            "threshold": REVIEW_THRESHOLD,
        }
        if current.engine() == "llm":
            payload["model"] = current.model
        return payload

    @app.get("/fixtures")
    def fixtures() -> dict:
        names = sorted(path.stem for path in _fixture_files())
        return {"fixtures": names}

    @app.get("/fixtures/{name}")
    def fixture(name: str) -> dict:
        path = _fixture_path(name)
        if path is None:
            raise HTTPException(status_code=404, detail="Unknown fixture.")
        return json.loads(path.read_text(encoding="utf-8"))

    @app.post("/triage", response_model=TriageOutput)
    async def triage(request: Request) -> TriageOutput:
        email = await _email_from_request(request)
        try:
            result = run_triage(email, app.state.settings)
        except LlmError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if email.attachment_names:
            fields = dict(result.fields)
            fields["attachmentNames"] = list(email.attachment_names)
            result = result.model_copy(update={"fields": fields})
        return result

    @app.get("/")
    def index() -> FileResponse:
        page = project_dir("static") / "index.html"
        if not page.is_file():
            raise HTTPException(status_code=404, detail="Triage page is not in the image.")
        return FileResponse(page, headers={"Cache-Control": "no-store"})

    return app


async def _email_from_request(request: Request) -> InboundEmail:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            return InboundEmail.model_validate(await request.json())
        except Exception as exc:
            raise HTTPException(status_code=400, detail="The email could not be read.") from exc
    if "multipart/form-data" not in content_type:
        raise HTTPException(status_code=415, detail="Send the email as JSON, or attach the PDF with the form.")

    form = await request.form()
    raw = form.get("email")
    if not isinstance(raw, str) or not raw.strip():
        raise HTTPException(status_code=400, detail="The form is missing the email.")
    try:
        email = InboundEmail.model_validate_json(raw)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="The email fields could not be read.") from exc

    names = list(email.attachment_names)
    stored: list[tuple[str, bytes]] = []
    for item in form.getlist("files"):
        filename = getattr(item, "filename", None)
        if not filename:
            continue
        data = await item.read()
        safe_name = _validated_pdf(filename, data)
        if safe_name not in names:
            names.append(safe_name)
        stored.append((safe_name, data))
    if stored:
        _store_pdfs(email.message_id, stored)
    return email.model_copy(update={"attachment_names": names})


def _validated_pdf(filename: str, data: bytes) -> str:
    base = Path(filename).name
    if not base.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only a PDF can be attached.")
    if not data:
        raise HTTPException(status_code=400, detail="The PDF is empty.")
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(status_code=400, detail="The PDF is larger than 15 MB.")
    if not data.startswith(PDF_MAGIC):
        raise HTTPException(status_code=400, detail="That file is not a PDF.")
    cleaned = re.sub(r"[^A-Za-z0-9._ -]", "_", base).strip("._")
    if not cleaned.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="The PDF needs a file name.")
    return cleaned


def _store_pdfs(message_id: str, files: list[tuple[str, bytes]]) -> None:
    folder_name = re.sub(r"[^A-Za-z0-9._-]", "_", message_id)[:80] or "message"
    folder = Path("/tmp/newsroom-uploads") / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in files:
        (folder / name).write_bytes(data)


def _fixture_files() -> list[Path]:
    directory = project_dir("fixtures")
    if not directory.is_dir():
        return []
    return [path for path in directory.glob("*.json") if path.is_file()]


def _fixture_path(name: str) -> Path | None:
    if "/" in name or "\\" in name or name.startswith("."):
        return None
    path = project_dir("fixtures") / f"{name}.json"
    if path.is_file():
        return path
    return None


app = create_app()
