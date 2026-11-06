from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Route = Literal["calendar", "article", "enquiry", "ignore"]
Disposition = Literal["dispatch", "review", "skip"]
Engine = Literal["heuristic", "llm"]

REVIEW_THRESHOLD = 0.75


class InboundEmail(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    message_id: str = Field(alias="messageId", min_length=1, max_length=200)
    from_address: str = Field(default="", alias="from", max_length=320)
    subject: str = Field(default="", max_length=500)
    body: str = Field(default="", max_length=20000)
    attachment_names: list[str] = Field(default_factory=list, alias="attachmentNames")


class TriageOutput(BaseModel):
    """The only result of this build. Specialists are not called."""

    model_config = ConfigDict(populate_by_name=True)

    route: Route
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=400)
    message_id: str = Field(alias="messageId")
    disposition: Disposition
    engine: Engine
    fields: dict[str, Any]


def disposition_for(route: Route, confidence: float) -> Disposition:
    if confidence < REVIEW_THRESHOLD:
        return "review"
    if route == "ignore":
        return "skip"
    return "dispatch"
