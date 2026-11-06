import os
from typing import Literal

from pydantic import BaseModel


Mode = Literal["auto", "heuristic", "llm"]

DEFAULT_MODEL = "aisingapore/Qwen-SEA-LION-v4.5-27B-IT"


class Settings(BaseModel):
    mode: Mode = "auto"
    base_url: str = ""
    api_key: str = ""
    model: str = DEFAULT_MODEL
    timeout_seconds: float = 90
    google_client_id: str = ""
    google_client_secret: str = ""
    google_token_path: str = "data/google-token.json"
    memory_path: str = "data/newsroom.sqlite"
    house_style_path: str = "house-style.md"
    job_interval_seconds: int = 1800
    gmail_query: str = "in:inbox newer_than:2d"
    oauth_redirect_uri: str = "http://127.0.0.1:8765/oauth/callback"

    @classmethod
    def from_env(cls) -> "Settings":
        mode = os.environ.get("TRIAGE_MODE", "auto").strip().lower() or "auto"
        if mode not in ("auto", "heuristic", "llm"):
            mode = "auto"
        return cls(
            mode=mode,
            base_url=os.environ.get("TRIAGE_BASE_URL", "").strip().rstrip("/"),
            api_key=os.environ.get("TRIAGE_API_KEY", "").strip(),
            model=os.environ.get("TRIAGE_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
            timeout_seconds=_positive_float(os.environ.get("TRIAGE_TIMEOUT_SECONDS", "90"), 90),
            google_client_id=os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip(),
            google_client_secret=os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip(),
            google_token_path=os.environ.get("GOOGLE_TOKEN_PATH", "data/google-token.json").strip()
            or "data/google-token.json",
            memory_path=os.environ.get("NEWSROOM_DB", "data/newsroom.sqlite").strip()
            or "data/newsroom.sqlite",
            house_style_path=os.environ.get("HOUSE_STYLE_PATH", "house-style.md").strip()
            or "house-style.md",
            job_interval_seconds=_positive_int(os.environ.get("JOB_INTERVAL_SECONDS", "1800"), 1800),
            gmail_query=os.environ.get("GMAIL_QUERY", "in:inbox newer_than:2d").strip()
            or "in:inbox newer_than:2d",
            oauth_redirect_uri=os.environ.get(
                "GOOGLE_OAUTH_REDIRECT_URI", "http://127.0.0.1:8765/oauth/callback"
            ).strip()
            or "http://127.0.0.1:8765/oauth/callback",
        )

    def engine(self) -> Literal["heuristic", "llm"]:
        if self.mode == "heuristic":
            return "heuristic"
        if self.mode == "llm":
            return "llm"
        if self.base_url or self.api_key:
            return "llm"
        return "heuristic"

    def chat_url(self) -> str:
        base = self.base_url or "https://api.sea-lion.ai/v1"
        return f"{base.rstrip('/')}/chat/completions"


def _positive_float(raw: str | None, default: float) -> float:
    try:
        value = float((raw or "").strip())
    except ValueError:
        return default
    if value <= 0:
        return default
    return value


def _positive_int(raw: str | None, default: int) -> int:
    try:
        value = int((raw or "").strip())
    except ValueError:
        return default
    if value <= 0:
        return default
    return value
