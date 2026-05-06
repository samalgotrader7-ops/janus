"""
tools/interview_ask.py — model-callable contextual interview (v1.19.0 Phase 5).

Lets the model ask the user one targeted question from the bundled
question library when it notices a memory gap mid-conversation. Example:
the user says "I'm working on a new project called X", the project
category has no cards, the model calls
``interview_ask(category="project")`` and the user gets the next
eligible project question via the existing clarify infrastructure.

WHY a separate tool from ``clarify``:
``clarify`` is for ad-hoc disambiguation — "which file?", "which chat?".
``interview_ask`` is for STRUCTURED memory population — it pulls from
the bundled question library, applies smart-skip (won't re-ask covered
ground), and writes a high-confidence card with proper provenance.

WIRES INTO THE EXISTING CLARIFY CALLBACK:
The tool reuses each gateway's clarify infrastructure (Telegram inline
keyboard, CLI input(), web prompt, etc.) — same pattern, different
question source. Constructed with a callback the gateway / runner
injects; outside any gateway it returns "[interview_ask unavailable]".
"""

from __future__ import annotations
from typing import Callable, Optional

from .. import interviews, memory, memory_extract
from .base import Tool


# Sentinel returned when no gateway callback is wired (CLI / sub-agent
# contexts where there's no UI to ask the user).
UNAVAILABLE = (
    "[interview_ask unavailable in this context — proceed without asking]"
)


# Callback signature mirrors clarify: (question, choices) -> answer string.
ClarifyCallback = Callable[[str, Optional[list[str]]], Optional[str]]


class InterviewAsk(Tool):
    """Ask the user one bundled-library question for a given category."""

    name = "interview_ask"
    description = (
        "Ask the user ONE bundled interview question to fill a memory "
        "gap. Use when the user mentions something in a category and "
        "there are no cards covering it yet (e.g., they reference a "
        "project but you have no project cards). Pulls the next "
        "eligible question from the library, asks via the gateway's "
        "clarify UI, writes a high-confidence card with the answer. "
        "Smart-skip prevents re-asking already-covered ground. Don't "
        "use when memory_search shows the topic is already covered."
    )
    parameters = {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "description": (
                    "Memory category to ask about. One of: identity, "
                    "preference, goal, project, habit, decision, "
                    "constraint, relationship."
                ),
            },
            "question_id": {
                "type": "string",
                "description": (
                    "Optional specific question id within the category "
                    "(e.g., 'current_active' for project). Omit to ask "
                    "the next eligible question per smart-skip."
                ),
            },
        },
        "required": ["category"],
    }
    risk = "read"
    dangerous = False

    def __init__(
        self,
        clarify_callback: ClarifyCallback | None = None,
        gateway: str = "cli",
        chat_id: str = "default",
    ):
        self._clarify = clarify_callback
        self._gateway = gateway
        self._chat_id = chat_id

    def run(self, args: dict, approver) -> str:
        category = str(args.get("category") or "").strip()
        if category not in interviews.SUPPORTED_CATEGORIES:
            return (
                f"error: invalid category {category!r}; valid: "
                f"{', '.join(interviews.SUPPORTED_CATEGORIES)}"
            )

        library = interviews.load_all()
        if not library or category not in library:
            return f"(no question library for category {category!r})"

        # Pick which question to ask.
        question_id = str(args.get("question_id") or "").strip()
        cat = library[category]
        state = interviews.load_state(self._gateway, self._chat_id)

        if question_id:
            question = cat.find(question_id)
            if question is None:
                return (
                    f"error: question {question_id!r} not in category "
                    f"{category!r}"
                )
            # Honor smart-skip even when explicit id given — if it's
            # already known, tell the model so it doesn't re-ask.
            if not interviews.is_eligible(state, category, question):
                return (
                    f"(skipped: {category}.{question_id} is already "
                    f"covered or in cooldown)"
                )
        else:
            nxt = interviews.next_question(
                state, library, category_filter=category,
            )
            if nxt is None:
                return (
                    f"(no eligible question in {category!r} — already "
                    f"covered or in cooldown)"
                )
            _cat, question = nxt

        fqid = question.fqid(category)

        if self._clarify is None:
            return UNAVAILABLE

        try:
            choices = question.choices if question.mode == "choices" else None
            answer = self._clarify(question.question, choices)
        except Exception as e:
            return (
                f"error: interview_ask callback failed: "
                f"{type(e).__name__}: {e}"
            )

        if answer is None or not str(answer).strip():
            interviews.mark_skipped(state, fqid)
            interviews.save_state(state)
            return f"(user declined to answer {category}.{question.id})"

        text = str(answer).strip()
        # Map numeric-choice answer back to choice text if applicable.
        if question.mode == "choices" and text.isdigit():
            idx = int(text) - 1
            if 0 <= idx < len(question.choices):
                text = question.choices[idx]

        try:
            proposal = memory_extract.CardProposal(
                type=category,
                subject=question.id,
                content=text,
                confidence=0.9,
                importance=question.importance,
                durability=question.durability,
                scope="global",
                origin_kind="user_turn",
            )
            written = memory.apply_cards(
                [proposal], gateway=self._gateway,
            )
            interviews.mark_answered(
                state, fqid,
                card_id=written[0] if written else "",
            )
            interviews.save_state(state)
        except Exception as e:
            return f"error: failed to apply card: {type(e).__name__}: {e}"

        return (
            f"asked {category}.{question.id}; answer recorded as a "
            f"high-confidence {category} card."
        )
