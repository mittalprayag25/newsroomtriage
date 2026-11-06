import os
from typing import Literal

from pydantic import BaseModel


Mode = Literal["auto", "heuristic", "llm"]


class Settings(BaseModel):
    mode: Mode = "auto"
    base_url: str = ""
    api_key: str = ""
    model: str = "aisingapore/Qwen-SEA-LION-v4.5-27B-IT"
    timeout_seconds: float = 90

    @classmethod
    def from_env(cls) -> "Settings":
        mode = os.environ.get("TRIAGE_MODE", "auto").strip().lower() or "auto"
        if mode not in ("auto", "heuristic", "llm"):
            mode = "auto"
        raw_timeout = os.environ.get("TRIAGE_TIMEOUT_SECONDS", "90").strip()
        try:
            timeout = float(raw_timeout)
        except ValueError:
            timeout = 90
        if timeout <= 0:
            timeout = 90
        return cls(
            mode=mode,
            base_url=os.environ.get("TRIAGE_BASE_URL", "").strip().rstrip("/"),
            api_key=os.environ.get("TRIAGE_API_KEY", "").strip(),
            model=os.environ.get("TRIAGE_MODEL", "aisingapore/Qwen-SEA-LION-v4.5-27B-IT").strip()
            or "aisingapore/Qwen-SEA-LION-v4.5-27B-IT",
            timeout_seconds=timeout,
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
