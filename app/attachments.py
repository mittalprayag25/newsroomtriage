from io import BytesIO

import mammoth
from pypdf import PdfReader

MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024
MAX_EXTRACTED_CHARS = 12_000


class AttachmentError(Exception):
    pass


def extract_text(filename: str, data: bytes) -> str:
    """Turn a PDF or Word file into plain text for the draft agent."""
    if not data:
        raise AttachmentError(f"{filename} is empty.")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise AttachmentError(f"{filename} is larger than 15 MB.")
    lower = filename.lower()
    if lower.endswith(".pdf"):
        return _clip(_pdf_text(filename, data))
    if lower.endswith(".docx"):
        return _clip(_docx_text(filename, data))
    if lower.endswith(".doc"):
        raise AttachmentError(f"{filename} is a legacy Word file. Save it as .docx.")
    raise AttachmentError(f"{filename} is not a PDF or Word file.")


def _pdf_text(filename: str, data: bytes) -> str:
    if not data.startswith(b"%PDF"):
        raise AttachmentError(f"{filename} is not a PDF.")
    try:
        reader = PdfReader(BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise AttachmentError(f"{filename} could not be read.") from exc
    return "\n".join(pages).strip()


def _docx_text(filename: str, data: bytes) -> str:
    try:
        result = mammoth.extract_raw_text(BytesIO(data))
    except Exception as exc:
        raise AttachmentError(f"{filename} could not be read.") from exc
    return (result.value or "").strip()


def _clip(text: str) -> str:
    if len(text) <= MAX_EXTRACTED_CHARS:
        return text
    return text[:MAX_EXTRACTED_CHARS].rstrip() + "\n[truncated]"
