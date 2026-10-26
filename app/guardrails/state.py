"""Process guard: approval state machines. Illegal moves are refused in code, whatever a prompt says."""
from __future__ import annotations

POST_TRANSITIONS: dict[str, set[str]] = {
    "drafting": {"pending_approval", "blocked"},
    "blocked": {"drafting", "rejected"},                  # ops may regenerate or drop it
    "pending_approval": {"approved", "changes_requested", "rejected"},
    "changes_requested": {"drafting", "pending_approval"},  # regenerate with the note, or edited by a human
    "approved": {"scheduled"},
    "scheduled": {"published"},        # "published" = the founder confirmed they posted it; nothing posts automatically
    "published": set(),
    "rejected": set(),
}

OUTREACH_TRANSITIONS: dict[str, set[str]] = {
    "blocked": {"pending_approval", "rejected"},
    "pending_approval": {"approved", "changes_requested", "rejected"},
    "changes_requested": {"pending_approval"},
    "approved": {"sending"},
    "sending": {"sent", "approved"},   # 'sending' is a lock: a double click or a crash can never send twice
    "sent": {"replied"},
    "replied": set(),
    "rejected": set(),
}


class IllegalTransition(ValueError):
    pass


def transition(obj, new_status: str, table: dict[str, set[str]]) -> None:
    current = obj.status
    if new_status not in table.get(current, set()):
        raise IllegalTransition(f"{type(obj).__name__} cannot move from '{current}' to '{new_status}'")
    obj.status = new_status
