"""Guardrails that live in code, not in the prompt.

Prompts can be talked around; these checks cannot. They run before the model sees the
message (input guard, escalation rules), around every tool call (arg allow-listing,
human confirmation) and on everything written to logs (PII redaction).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import MAX_INPUT_CHARS

# --------------------------------------------------------------------------- input guard
_INJECTION = [
    r"ignore (all |any |the |your )?(previous|prior|above|earlier) (instructions|rules|prompts?|messages)",
    r"disregard .{0,40}(instructions|rules|polic(y|ies)|guidelines)",
    r"(reveal|show|print|repeat|output) .{0,30}(system prompt|hidden prompt|your instructions)",
    r"\byou are now\b",
    r"\bact as (an? )?(admin|administrator|developer|root|unrestricted|different)",
    r"developer mode|jailbreak|\bDAN\b",
    r"override .{0,30}(polic|safety|rules|confirmation|verification)",
    r"(refund|cancel).{0,40}without (any )?(confirm|verif|checking|asking)",
    r"(as|i am|i'm) (the |an? )?(admin|administrator|manager|employee|developer) of",
]
_INJECTION_RE = re.compile("|".join(_INJECTION), re.I)


@dataclass
class InputVerdict:
    ok: bool
    reason: str = ""


def check_input(text: str) -> InputVerdict:
    if not text or not text.strip():
        return InputVerdict(False, "empty")
    if len(text) > MAX_INPUT_CHARS:
        return InputVerdict(False, "too_long")
    if _INJECTION_RE.search(text):
        return InputVerdict(False, "prompt_injection")
    return InputVerdict(True)


# --------------------------------------------------------------------------- escalation
@dataclass
class Escalation:
    reason: str
    priority: str
    customer_message: str


_ESCALATIONS = [
    (
        "safety",
        "urgent",
        r"caught fire|on fire|\bsmok(e|ing)\b|\bburn(ed|t|ing)\b|overheat|explod|electric shock|\binjur",
        "That sounds like a safety issue. Please stop using the product right away and keep it "
        "away from flammable items. I've escalated this as urgent and a specialist will contact you.",
    ),
    (
        "legal",
        "high",
        r"\blawyer|attorney|\bsue\b|suing|legal action|consumer court|small claims",
        "I understand this is serious. I've passed your case to our senior support team, who "
        "handle formal complaints, and they will contact you.",
    ),
    (
        "fraud",
        "high",
        r"fraud|unauthori[sz]ed (charge|transaction|payment)|account (was )?hacked|stolen card|scam",
        "I'm sorry, that's worrying. Account and payment security issues are handled by our "
        "specialist team, so I've escalated this as high priority and they will contact you.",
    ),
    (
        "human_requested",
        "normal",
        r"(talk|speak|chat|connect|transfer)\b.{0,20}\b(human|person|agent|manager|representative|someone)|real person|human agent",
        "Of course. I've created a ticket so a human agent can pick this up with the full context.",
    ),
]


def check_escalation(text: str) -> Escalation | None:
    for reason, priority, pattern, reply in _ESCALATIONS:
        if re.search(pattern, text, re.I):
            return Escalation(reason, priority, reply)
    return None


# --------------------------------------------------------------------------- confirmation
_YES = {"yes", "y", "yeah", "yep", "yup", "confirm", "confirmed", "sure", "ok", "okay",
        "go ahead", "proceed", "do it", "please do", "correct", "that's right"}
_NO = {"no", "n", "nope", "nah", "stop", "abort", "never mind", "nevermind", "keep it",
       "don't", "do not", "dont", "cancel that", "no thanks", "no thank you"}


def parse_confirmation(text: str) -> bool | None:
    """True = yes, False = no, None = not a clear answer (so the pending action is dropped)."""
    t = re.sub(r"[^\w' ]", " ", text.lower()).strip()
    t = re.sub(r"\s+", " ", t)
    if not t or len(t.split()) > 8:
        return None
    first = t.split()[0]
    if t in _NO or first in {"no", "nope", "nah", "stop", "don't", "dont"}:
        return False
    if t in _YES or first in {"yes", "yeah", "yep", "yup", "confirm", "sure", "ok", "okay"}:
        return True
    return None


# --------------------------------------------------------------------------- PII redaction
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_PHONE = re.compile(r"(?<!\w)\+?\d[\d ()-]{8,}\d(?!\w)")


def redact(text: str) -> str:
    text = _EMAIL.sub("[email]", text)
    text = _CARD.sub("[card]", text)
    return _PHONE.sub("[phone]", text)


# --------------------------------------------------------------------------- pending action
@dataclass
class PendingAction:
    """A state-changing tool call the customer has been asked to confirm."""

    tool: str
    args: dict
    summary: str
    extra: dict = field(default_factory=dict)
