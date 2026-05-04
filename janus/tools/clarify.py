"""
tools/clarify.py — model-callable "ask the user a question" tool (v1.8.0).

WHY THIS EXISTS:
Pre-v1.8 the model had two bad options when it genuinely needed a fact
only the user has:
  (a) GUESS and hope — the v1.7 unattended-preamble path
  (b) Ramble in chat asking for clarification, hoping the user replies
      and the next turn understands the reference

Hermes solved this with `clarify_tool` (multi-choice or free-text
question presented WITHIN a turn — the executor blocks on the user's
answer and resumes mid-loop). v1.8 ports that pattern.

WHEN THE MODEL SHOULD USE IT:
- Real ambiguity that affects what work to do (e.g., "delete which
  branch?", "send to which chat?")
- Choice between mutually-exclusive paths
- A fact the user explicitly didn't supply but you can't proceed
  without (a path, an ID, a credential reference)

WHEN NOT TO USE IT:
- Confirmation. The unattended preamble (and Rule 7 of JANUS_CHAT_SYSTEM)
  says default to ACT. Don't ask "should I proceed?".
- Anything you could plausibly grep / read from disk / look up via
  session_recent.

PLATFORM CALLBACKS:
The actual UI lives in the gateway / CLI layer. This tool owns:
  - the JSON schema the model sees
  - input validation (max 6 choices, max 500-char question)
  - dispatch to a callback the runner injects via Tool constructor

Callback signature: `(question: str, choices: list[str] | None) -> str`.
Returns the user's chosen string. Returning the literal "[clarify
unavailable]" tells the model the gateway can't ask — it should make
a reasonable choice and proceed.
"""

from __future__ import annotations
from typing import Callable

from .base import Tool


# Fewer than Hermes's 4 because we always implicitly add a "[Other]"
# escape hatch in interactive UIs. 6 is enough that the model can offer
# real choice without overwhelming a Telegram keyboard.
MAX_CHOICES = 6
MAX_QUESTION_CHARS = 500


# Sentinel returned by gateways that can't show a UI (headless, etc.).
# The model should treat this as "no answer available, proceed".
UNAVAILABLE = "[clarify unavailable in this context — pick a reasonable default]"


class Clarify(Tool):
    """Ask the user one question; block until they answer."""

    name = "clarify"
    description = (
        "Ask the user ONE question and block until they answer. Use "
        "ONLY for genuine ambiguity that affects what work to do "
        "(which file? which branch? which chat? send-or-save?). Don't "
        "use for confirmation — default to ACT per Rule 7. Don't use "
        "when you could grep / fs_read / session_recent the answer "
        "instead. Returns the user's answer as a string."
    )
    parameters = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": (
                    "One short, specific question (≤500 chars). "
                    "Frame it so a one-line answer is enough."
                ),
            },
            "choices": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional pre-canned choices (up to 6). When "
                    "provided, the gateway renders them as buttons / "
                    "numbered list. The user can still type a free-form "
                    "answer (UI adds an 'Other' option). Omit for "
                    "purely open-ended questions."
                ),
            },
        },
        "required": ["question"],
    }
    risk = "read"  # asking is observation, not mutation

    def __init__(self, callback: Callable[[str, list[str] | None], str] | None = None):
        self._callback = callback

    def run(self, args: dict, approver) -> str:
        question = (args.get("question") or "").strip()
        if not question:
            return "error: question required"
        if len(question) > MAX_QUESTION_CHARS:
            question = question[:MAX_QUESTION_CHARS - 1] + "…"

        choices = args.get("choices")
        if choices is not None:
            if not isinstance(choices, list):
                return "error: choices must be a list of strings"
            choices = [str(c).strip() for c in choices if str(c).strip()]
            if len(choices) > MAX_CHOICES:
                choices = choices[:MAX_CHOICES]
            if not choices:
                choices = None

        # No approver gate — read-class tool; asking the user a question
        # has no side effect.

        if self._callback is None:
            return UNAVAILABLE
        try:
            answer = self._callback(question, choices)
        except Exception as e:
            return f"error: clarify callback failed: {type(e).__name__}: {e}"
        if answer is None:
            return UNAVAILABLE
        return str(answer).strip() or UNAVAILABLE
