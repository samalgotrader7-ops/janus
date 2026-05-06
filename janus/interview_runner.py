"""
interview_runner.py — orchestrate the question-answer loop (v1.19.0 Phase 3).

Gateway-agnostic. The runner walks ``interviews.next_question`` until
no eligible question remains, the user cancels, or it hits a question
budget. Each gateway provides an ``ask_user`` callback that knows how
to render the question and collect an answer (CLI uses ``input()``,
Telegram uses an inline keyboard, etc).

Answers become memory cards through the v1.18 ``memory.apply_cards``
pipeline with ``origin_kind="user_turn"`` and ``confidence=0.9`` (high
because the user explicitly answered). Default scope is ``global`` —
interview answers are intentional facts about the user that should
apply everywhere, unlike organic-conversation extractions which default
to the current origin per the v1.18 privacy invariant.

SKIP / LATER SENTINELS:
The ask_user callback returns one of:
  - a non-empty string → user's answer
  - ``SKIP_TOKEN`` → skip THIS question (7-day cooldown via mark_skipped)
  - ``LATER_TOKEN`` → cancel the WHOLE interview (saved state, resume later)
  - ``""`` / ``None`` → treated as Skip
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import interviews, memory, memory_extract


# Sentinel return values from the ask_user callback.
SKIP_TOKEN = "__interview_skip__"
LATER_TOKEN = "__interview_later__"


# ask_user(question, fqid) -> str | SKIP_TOKEN | LATER_TOKEN
AskUser = Callable[[interviews.Question, str], str]


@dataclass
class InterviewResult:
    answered: int = 0
    skipped: int = 0
    cards_written: list[str] = field(default_factory=list)
    completed: bool = False  # walked to "no more eligible questions"
    cancelled: bool = False  # user pressed Later
    completion_pct: dict[str, float] = field(default_factory=dict)


def run_one_shot(
    state: interviews.InterviewState,
    library: dict[str, interviews.Category],
    ask_user: AskUser,
    *,
    category_filter: Optional[str] = None,
    scope: str = "global",
    conversation_id: str = "",
    turn: int = 0,
    max_questions: int = 50,
) -> InterviewResult:
    """Walk the question library, asking each eligible question.

    Returns when:
      - no more eligible questions (``result.completed=True``)
      - user pressed Later (``result.cancelled=True``)
      - ``max_questions`` reached (a safety budget — default 50)

    Always saves state at the end so re-running picks up where we left
    off (or skips already-answered questions).
    """
    state.mode = "one_shot"
    if not state.started_at:
        state.started_at = interviews._now_iso()

    result = InterviewResult()
    asked = 0

    while asked < max_questions:
        nxt = interviews.next_question(
            state, library, category_filter=category_filter,
        )
        if nxt is None:
            result.completed = True
            break

        cat, q = nxt
        fqid = q.fqid(cat.name)
        state.current_category = cat.name
        state.current_question_id = fqid

        try:
            answer = ask_user(q, fqid)
        except Exception:
            # Callback failure → treat as Later so we don't lose state.
            result.cancelled = True
            break

        asked += 1

        if answer is None or answer == LATER_TOKEN:
            result.cancelled = True
            break
        if answer == SKIP_TOKEN or not str(answer).strip():
            interviews.mark_skipped(state, fqid)
            result.skipped += 1
            continue

        # Answer string → CardProposal
        proposal = memory_extract.CardProposal(
            type=cat.name,
            subject=q.id,
            content=str(answer).strip(),
            confidence=0.9,
            importance=q.importance,
            durability=q.durability,
            scope=scope,
            origin_kind="user_turn",
        )
        try:
            written = memory.apply_cards(
                [proposal],
                conversation_id=conversation_id,
                turn=turn,
                gateway=state.gateway,
            )
            result.cards_written.extend(written)
            interviews.mark_answered(
                state, fqid,
                card_id=written[0] if written else "",
            )
            result.answered += 1
        except Exception:
            # apply_cards failed → record skip so loop progresses.
            interviews.mark_skipped(state, fqid)
            result.skipped += 1

    state.completion_pct = interviews.compute_completion(state, library)
    state.mode = "idle"
    state.current_category = ""
    state.current_question_id = ""
    interviews.save_state(state)
    result.completion_pct = state.completion_pct
    return result


def render_completion_meter(
    pcts: dict[str, float],
    *,
    bar_width: int = 10,
) -> list[str]:
    """Plain-text profile completion meter (one line per category)."""
    lines: list[str] = []
    for cat in interviews.SUPPORTED_CATEGORIES:
        pct = pcts.get(cat, 0.0)
        filled = int(round(pct * bar_width))
        bar = "█" * filled + "░" * (bar_width - filled)
        lines.append(f"  {cat:14} {bar} {int(pct * 100):>3}%")
    return lines
