from app.heuristic import triage_heuristic
from app.llm import triage_llm
from app.models import InboundEmail, TriageOutput
from app.settings import Settings


def run_triage(email: InboundEmail, settings: Settings) -> TriageOutput:
    if settings.engine() == "llm":
        return triage_llm(email, settings)
    return triage_heuristic(email)
