import re
from datetime import datetime

from app.models import Disposition, InboundEmail, TriageOutput, disposition_for

# Newsroom clock. Straits Times desks work in Singapore time, which has no DST.
SINGAPORE_OFFSET = "+08:00"

MONTHS = {
    name: index
    for index, name in enumerate(
        (
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ),
        start=1,
    )
}

PHRASES: dict[str, list[tuple[str, int, str]]] = {
    "calendar": [
        ("interview", 4, "an interview request"),
        ("press conference", 4, "a press conference"),
        ("invitation", 3, "an invitation"),
        ("briefing", 3, "a briefing"),
        ("meeting", 3, "a meeting"),
        ("meet", 3, "a meeting"),
        ("calendar", 2, "a calendar mention"),
        ("schedule", 2, "a scheduling request"),
        ("tomorrow", 2, "a relative day"),
    ],
    "article": [
        ("for immediate release", 5, "an immediate-release line"),
        ("press release", 4, "a press release"),
        ("media release", 4, "a media release"),
        ("embargo", 3, "an embargo"),
        ("official statement", 3, "an official statement"),
        ("headline", 1, "a headline to settle"),
    ],
    "enquiry": [
        ("correction", 4, "a correction"),
        ("is it true", 3, "a truth check"),
        ("clarify", 3, "a request to clarify"),
        ("confirm", 2, "a request to confirm"),
        ("can you", 2, "a direct question"),
        ("could you", 2, "a direct question"),
        ("can we", 2, "a direct question"),
    ],
    "ignore": [
        ("unsubscribe", 4, "an unsubscribe line"),
        ("newsletter", 4, "a newsletter"),
        ("view in browser", 3, "a bulk-mail footer"),
        ("no-reply", 3, "a no-reply sender"),
        ("noreply", 3, "a no-reply sender"),
        ("limited time", 2, "a promotion"),
    ],
}

DESKS: list[tuple[str, tuple[str, ...]]] = [
    ("Sport", ("premier league", "world cup", "athlete")),
    ("Business", ("earnings", "share price", "company results")),
    ("Asia", ("asean", "jakarta", "kuala lumpur", "beijing", "tokyo")),
    ("World", ("white house", "united nations")),
    ("Singapore", ("hdb", "mrt", "parliament", "singapore", "cpf")),
]

DATE_RE = re.compile(
    r"\b(\d{1,2})\s+(January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+(\d{4})\b",
    re.IGNORECASE,
)
TIME_RANGE_RE = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:to|–|-)\s*"
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b",
    re.IGNORECASE,
)
LOCATION_RE = re.compile(
    r"\bat\s+([A-Z][A-Za-z0-9'’/-]*(?:\s+[A-Z][A-Za-z0-9'’/-]*){0,5})"
)
SUBJECT_PREFIX_RE = re.compile(
    r"^(?:(?:re|fw|fwd|for immediate release)\s*:\s*)+",
    re.IGNORECASE,
)


def triage_heuristic(email: InboundEmail) -> TriageOutput:
    text = _blob(email)
    points, notes = _score(text, email)
    ranked = sorted(points.items(), key=lambda item: (-item[1], item[0]))
    route, top = ranked[0]
    second_route, second = ranked[1]

    if top <= 0:
        route = "ignore"
        confidence = 0.60
        reason = "Nothing in the message maps to a desk action."
    elif top == second:
        tied = [name for name, value in ranked if value == top]
        route = tied[0]
        confidence = 0.60
        reason = (
            f"{tied[0].capitalize()} and {tied[1]} scored equally, "
            "so this needs a person."
        )
    else:
        confidence = _confidence(top, second)
        detail = ", ".join(notes[route][:3]) or "the wording"
        if top - second == 1:
            reason = (
                f"Leaning {route} because of {detail}, "
                f"but {second_route} is close enough that a person should check."
            )
        else:
            reason = f"Routed to {route} because of {detail}."

    fields = _fields(route, email, text)
    if route == "calendar" and not fields.get("start"):
        confidence = min(confidence, 0.70)
        reason = f"{reason} No clock time and date were found, so it is held."

    confidence = round(min(0.97, confidence), 2)
    disposition: Disposition = disposition_for(route, confidence)
    return TriageOutput(
        route=route,
        confidence=confidence,
        reason=reason[:400],
        messageId=email.message_id,
        disposition=disposition,
        engine="heuristic",
        fields=fields,
    )


def _blob(email: InboundEmail) -> str:
    attachments = " ".join(email.attachment_names)
    return f"{email.from_address}\n{email.subject}\n{email.body}\n{attachments}".lower()


def _score(text: str, email: InboundEmail) -> tuple[dict[str, int], dict[str, list[str]]]:
    points = {route: 0 for route in PHRASES}
    notes: dict[str, list[str]] = {route: [] for route in PHRASES}
    for route, phrases in PHRASES.items():
        for phrase, weight, label in phrases:
            if _has_phrase(text, phrase) and label not in notes[route]:
                points[route] += weight
                notes[route].append(label)
    if _attachment_is_copy(email):
        points["article"] += 3
        notes["article"].append("an attached document")
    if DATE_RE.search(email.body) or DATE_RE.search(email.subject):
        points["calendar"] += 2
        notes["calendar"].append("a date")
    if TIME_RANGE_RE.search(email.body) or TIME_RANGE_RE.search(email.subject):
        points["calendar"] += 3
        notes["calendar"].append("a time range")
    if "?" in email.body or "?" in email.subject:
        points["enquiry"] += 2
        notes["enquiry"].append("a question mark")
    if "% off" in text or "percent off" in text:
        points["ignore"] += 2
        notes["ignore"].append("a promotion")
    return points, notes


def _has_phrase(text: str, phrase: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None


def _attachment_is_copy(email: InboundEmail) -> bool:
    return any(name.lower().endswith((".pdf", ".doc", ".docx", ".txt", ".rtf")) for name in email.attachment_names)


def _confidence(top: int, second: int) -> float:
    margin = top - second
    if margin <= 1:
        return 0.68
    return 0.74 + min(margin - 1, 5) * 0.05


def _fields(route: str, email: InboundEmail, text: str) -> dict:
    if route == "calendar":
        start, end = _schedule(email)
        location = _location(email.body)
        attendees = [email.from_address] if "@" in email.from_address else []
        fields: dict = {
            "title": _clean_subject(email.subject) or "Newsroom event",
            "start": start,
            "end": end,
            "location": location,
            "attendees": attendees,
        }
        return {key: value for key, value in fields.items() if value not in (None, "")}
    if route == "article":
        fields = {
            "headline": _clean_subject(email.subject) or "Untitled draft",
            "desk": _desk(text),
            "attachmentNames": list(email.attachment_names),
        }
        if fields["desk"] is None:
            del fields["desk"]
        return fields
    if route == "enquiry":
        return {"question": _question(email)}
    return {}


def _clean_subject(subject: str) -> str:
    cleaned = SUBJECT_PREFIX_RE.sub("", subject).strip()
    return re.sub(r"\s+", " ", cleaned)


def _schedule(email: InboundEmail) -> tuple[str | None, str | None]:
    source = f"{email.subject}\n{email.body}"
    date_match = DATE_RE.search(source)
    time_match = TIME_RANGE_RE.search(source)
    if not date_match or not time_match:
        return None, None
    day = int(date_match.group(1))
    month = MONTHS[date_match.group(2).lower()]
    year = int(date_match.group(3))
    start_hour, start_minute = _to_24h(time_match.group(1), time_match.group(2), time_match.group(3) or time_match.group(6))
    end_hour, end_minute = _to_24h(time_match.group(4), time_match.group(5), time_match.group(6))
    try:
        start = datetime(year, month, day, start_hour, start_minute)
        end = datetime(year, month, day, end_hour, end_minute)
    except ValueError:
        return None, None
    return (
        start.strftime(f"%Y-%m-%dT%H:%M:%S{SINGAPORE_OFFSET}"),
        end.strftime(f"%Y-%m-%dT%H:%M:%S{SINGAPORE_OFFSET}"),
    )


def _to_24h(hour_text: str, minute_text: str | None, ampm: str | None) -> tuple[int, int]:
    hour = int(hour_text)
    minute = int(minute_text or "0")
    if ampm:
        marker = ampm.lower()
        if marker == "pm" and hour != 12:
            hour += 12
        if marker == "am" and hour == 12:
            hour = 0
    return hour, minute


def _location(body: str) -> str | None:
    match = LOCATION_RE.search(body)
    if not match:
        return None
    return match.group(1).strip(" .")


def _desk(text: str) -> str | None:
    for desk, words in DESKS:
        if any(word in text for word in words):
            return desk
    return None


def _question(email: InboundEmail) -> str:
    body = email.body.strip()
    if "?" in body:
        return body[: body.find("?") + 1].strip()[:500]
    fallback = body or email.subject
    return fallback[:500] or "No question text was included."
