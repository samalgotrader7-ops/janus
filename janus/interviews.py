"""
interviews.py — question library + state machine for memory cold-start
(v1.19.0 Phases 1-2).

Phase 1: bundled markdown files of targeted questions, parsed into
Question / Category dataclasses. The library is user-editable in
``~/.janus/interviews/``; defaults ship in ``janus/interviews_bundled/``.

Phase 2: per-(gateway, chat_id) interview state — what the user has
answered or skipped, where they are in a one-shot flow, drip-mode
quota. Smart-skip checks the v1.18 cards layer too: if extraction
already wrote a card for a (category, subject), the interview won't
re-ask.

P5 (plain-text canonical): question files AND state files are
human-readable. ``cat ~/.janus/interviews/_state/cli.json`` shows
exactly what the runner thinks.

CATEGORIES MUST MATCH ``memory_cards.TYPES``: the 8 types from v1.18.
A question's category drives the type of the resulting card.
"""

from __future__ import annotations
import datetime as _dt
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import config, memory_cards, skills as _skills


# v1.19.0 Phase 2 tunables.
SKIP_COOLDOWN_DAYS = int(os.getenv("JANUS_INTERVIEW_SKIP_COOLDOWN_DAYS", "7"))
DRIP_DEFAULT_PER_DAY = int(os.getenv("JANUS_INTERVIEW_DRIP_PER_DAY", "2"))
DRIP_AUTO_PAUSE_PCT = float(os.getenv("JANUS_INTERVIEW_DRIP_PAUSE_PCT", "0.9"))
INTERVIEW_MODES = ("idle", "one_shot", "drip")

# v1.41.7 — global kill switch. Default OFF so Janus doesn't tack a
# "Quick question:" onto every assistant reply in normal chats. Users
# who actually want the onboarding-drip experience set this to 1
# explicitly (and then `/interview` to enable on a given chat). Sam
# 2026-05-13: the post-turn drip questions in Telegram conversations
# were treated as a bug — the previous opt-OUT (`/interview pause`)
# wasn't discoverable enough.
DRIP_ENABLED = os.getenv(
    "JANUS_INTERVIEW_DRIP_ENABLED", "0",
).lower() in ("1", "true", "yes", "on")


# Categories must EXACTLY match the v1.18 type set so answers map
# correctly at apply time. Hardcoded (not ``tuple(memory_cards.TYPES)``)
# to avoid a circular at module load: memory_cards → skills → tools/__init__
# → tools/interview_ask → interviews, where the last hop tries to read
# memory_cards.TYPES while memory_cards is still mid-init. Pinned by
# tests/test_interviews_categories_match_memory_cards_TYPES.
SUPPORTED_CATEGORIES: tuple[str, ...] = (
    "identity",
    "preference",
    "goal",
    "project",
    "habit",
    "decision",
    "constraint",
    "relationship",
)
QUESTION_MODES = ("text", "choices")


# ---------- Data classes ----------


@dataclass
class Question:
    """One question in the library. Independent of category — the
    enclosing Category dataclass holds that.
    """
    id: str
    question: str
    mode: str = "text"                  # one of QUESTION_MODES
    choices: list[str] = field(default_factory=list)
    importance: float = 0.5
    durability: float = 0.5
    recheck_days: Optional[int] = None  # None = never re-ask
    placeholder: str = ""

    def fqid(self, category: str) -> str:
        """Fully-qualified id used by state files: ``category.id``."""
        return f"{category}.{self.id}"


@dataclass
class Category:
    """A loaded category file — frontmatter metadata + question list."""
    name: str
    description: str
    version: int
    questions: list[Question]
    body: str = ""  # markdown body after the frontmatter (free-form notes)

    def find(self, question_id: str) -> Optional[Question]:
        for q in self.questions:
            if q.id == question_id:
                return q
        return None


class InterviewLoadError(ValueError):
    """Raised when a question file is malformed."""


# ---------- Path helpers ----------


def interviews_dir() -> Path:
    """``~/.janus/interviews/`` — user-editable library root."""
    custom = getattr(config, "INTERVIEWS_DIR", None)
    if custom is not None:
        return Path(custom)
    return Path(config.HOME) / "interviews"


def category_path(category: str) -> Path:
    return interviews_dir() / f"{category}.md"


def bundled_dir() -> Path:
    """In-package ``janus/interviews_bundled/`` directory.

    Resolved relative to this module so the package works installed via
    pipx, editable, or zipped.
    """
    return Path(__file__).parent / "interviews_bundled"


def install_marker_path() -> Path:
    return interviews_dir() / "_bundled_installed"


# ---------- Parser ----------


def _parse_question_dict(qid: str, q: dict, source: Path) -> Question:
    """Build a Question from one frontmatter entry.

    File format (dict-of-dicts under ``questions:``):

        questions:
          name:
            question: "What should I call you?"
            mode: text
          role:
            question: "..."

    The dict KEY is the question id; the value dict carries the rest.
    Order is preserved by CPython's insertion-ordered dicts.
    """
    if not isinstance(q, dict):
        raise InterviewLoadError(
            f"question {qid!r} must be a dict in {source}"
        )

    qid = str(qid or "").strip()
    text = str(q.get("question") or "").strip()
    if not qid:
        raise InterviewLoadError(f"question missing id in {source}")
    if not text:
        raise InterviewLoadError(
            f"question {qid!r} missing 'question' text in {source}"
        )

    mode = str(q.get("mode") or "text").strip()
    if mode not in QUESTION_MODES:
        raise InterviewLoadError(
            f"question {qid!r}: mode must be one of {QUESTION_MODES}, "
            f"got {mode!r}"
        )

    raw_choices = q.get("choices") or []
    if not isinstance(raw_choices, list):
        raise InterviewLoadError(
            f"question {qid!r}: choices must be a list"
        )
    choices = [str(c).strip() for c in raw_choices if str(c).strip()]
    if mode == "choices" and not choices:
        raise InterviewLoadError(
            f"question {qid!r}: mode='choices' requires non-empty choices"
        )

    try:
        importance = float(q.get("importance", 0.5))
        durability = float(q.get("durability", 0.5))
    except (TypeError, ValueError):
        raise InterviewLoadError(
            f"question {qid!r}: importance / durability must be floats"
        )
    importance = max(0.0, min(1.0, importance))
    durability = max(0.0, min(1.0, durability))

    recheck = q.get("recheck_days")
    if recheck is not None:
        try:
            recheck_int = int(recheck)
        except (TypeError, ValueError):
            raise InterviewLoadError(
                f"question {qid!r}: recheck_days must be int or null"
            )
        if recheck_int < 0:
            raise InterviewLoadError(
                f"question {qid!r}: recheck_days must be >= 0"
            )
        recheck = recheck_int

    placeholder = str(q.get("placeholder") or "")

    return Question(
        id=qid,
        question=text,
        mode=mode,
        choices=choices,
        importance=importance,
        durability=durability,
        recheck_days=recheck,
        placeholder=placeholder,
    )


def load_category(category: str) -> Category:
    """Parse one category file from ``~/.janus/interviews/<category>.md``.

    Raises ``InterviewLoadError`` on missing file, bad frontmatter, or
    invalid question entries.
    """
    if category not in SUPPORTED_CATEGORIES:
        raise InterviewLoadError(
            f"unsupported category {category!r}; must be one of "
            f"{SUPPORTED_CATEGORIES}"
        )
    path = category_path(category)
    if not path.exists():
        raise InterviewLoadError(f"category file not found: {path}")
    return _parse_path(path, category)


def _parse_path(path: Path, expected_category: str) -> Category:
    text = path.read_text(encoding="utf-8")
    fm, body = _skills.parse_frontmatter(text)
    if not fm:
        raise InterviewLoadError(f"missing frontmatter: {path}")

    cat_name = str(fm.get("category") or "").strip()
    if cat_name != expected_category:
        raise InterviewLoadError(
            f"frontmatter category mismatch in {path}: "
            f"expected {expected_category!r}, got {cat_name!r}"
        )

    raw_qs = fm.get("questions")
    if not isinstance(raw_qs, dict):
        raise InterviewLoadError(
            f"questions must be a dict (id → question-spec) in {path}; "
            f"got {type(raw_qs).__name__}"
        )

    questions: list[Question] = []
    for qid, q in raw_qs.items():
        question = _parse_question_dict(str(qid), q, path)
        questions.append(question)
    # Duplicate ids are impossible at this layer because dict keys are
    # already unique. The frontmatter parser collapses duplicate keys
    # to the LAST value, which is the same yaml-spec behavior.

    try:
        version = int(fm.get("version") or 1)
    except (TypeError, ValueError):
        version = 1

    return Category(
        name=cat_name,
        description=str(fm.get("description") or "").strip(),
        version=version,
        questions=questions,
        body=body,
    )


# ---------- Library-level helpers ----------


def list_categories() -> list[str]:
    """Return SUPPORTED_CATEGORIES that have a present file on disk.

    Files for unsupported names are ignored — the user might add a
    custom .md file but the runner only iterates types from v1.18.
    """
    d = interviews_dir()
    if not d.exists():
        return []
    out: list[str] = []
    for p in sorted(d.glob("*.md")):
        if p.stem in SUPPORTED_CATEGORIES:
            out.append(p.stem)
    return out


def load_all() -> dict[str, Category]:
    """Load every present category. Skips files that fail to parse —
    a single bad file doesn't break the whole interview surface.
    """
    out: dict[str, Category] = {}
    for cat in list_categories():
        try:
            out[cat] = load_category(cat)
        except InterviewLoadError:
            continue
    return out


# ---------- Bundled-library install (Phase 8 hook) ----------


def is_bundled_installed() -> bool:
    return install_marker_path().exists()


def maybe_install_bundled() -> dict:
    """Copy bundled question files to ``~/.janus/interviews/`` once.

    Idempotent via marker file. NEVER overwrites a user's edits — once
    the marker is present the install is "done"; new bundled files in
    later releases land via separate handling (deferred).

    Returns ``{"skipped": bool, "installed": int}``.
    """
    if is_bundled_installed():
        return {"skipped": True, "installed": 0}
    src = bundled_dir()
    dst = interviews_dir()
    dst.mkdir(parents=True, exist_ok=True)

    installed = 0
    if src.is_dir():
        for src_file in sorted(src.glob("*.md")):
            target = dst / src_file.name
            if target.exists():
                continue  # user beat us to it; don't clobber
            target.write_text(
                src_file.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            installed += 1

    install_marker_path().write_text(
        f"bundled interviews installed at "
        f"{_dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
        f"files: {installed}\n",
        encoding="utf-8",
    )
    return {"skipped": False, "installed": installed}


# ---------- Phase 2: state machine + smart-skip ----------


@dataclass
class InterviewState:
    """Per-(gateway, chat_id) interview progress.

    Persisted to ``~/.janus/interviews/_state/<gateway>__<chat_id>.json``.
    P5 plain-text: hand-readable, hand-editable. ``cat`` it any time.
    """
    gateway: str
    chat_id: str
    mode: str = "idle"                   # one of INTERVIEW_MODES
    started_at: str = ""
    current_category: str = ""
    current_question_id: str = ""        # "category.id" form
    answered: dict[str, dict] = field(default_factory=dict)
    skipped: dict[str, dict] = field(default_factory=dict)
    drip_quota_remaining: int = 0
    drip_quota_resets_at: str = ""
    drip_filter_category: str = ""       # "" = no filter, walk all 8 categories
    completion_pct: dict[str, float] = field(default_factory=dict)


def _state_dir() -> Path:
    return interviews_dir() / "_state"


def _safe_chat(chat_id: str) -> str:
    """Sanitize chat_id for filesystem use."""
    s = str(chat_id or "default")
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in s) or "default"


def _cross_gateway_enabled() -> bool:
    """v1.34.8 — Phase 8.2. When JANUS_INTERVIEW_CROSS_GATEWAY=1 is
    set, the interview state file is keyed by chat_id only (no
    gateway prefix), so a session started on web continues on
    telegram. Default off — preserves per-gateway behavior for
    existing deployments."""
    import os
    return os.environ.get(
        "JANUS_INTERVIEW_CROSS_GATEWAY", "0",
    ).lower() in ("1", "true", "yes", "on")


def state_path(gateway: str, chat_id: str) -> Path:
    if _cross_gateway_enabled():
        # Single shared file per chat_id; gateway dropped from name.
        return _state_dir() / f"shared__{_safe_chat(chat_id)}.json"
    return _state_dir() / f"{gateway}__{_safe_chat(chat_id)}.json"


def load_state(gateway: str, chat_id: str) -> InterviewState:
    """Load state for (gateway, chat_id), or return a fresh blank one."""
    path = state_path(gateway, chat_id)
    if not path.exists():
        return InterviewState(gateway=gateway, chat_id=str(chat_id))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return InterviewState(gateway=gateway, chat_id=str(chat_id))
    if not isinstance(data, dict):
        return InterviewState(gateway=gateway, chat_id=str(chat_id))

    mode = str(data.get("mode") or "idle")
    if mode not in INTERVIEW_MODES:
        mode = "idle"
    return InterviewState(
        gateway=str(data.get("gateway") or gateway),
        chat_id=str(data.get("chat_id") or chat_id),
        mode=mode,
        started_at=str(data.get("started_at") or ""),
        current_category=str(data.get("current_category") or ""),
        current_question_id=str(data.get("current_question_id") or ""),
        answered=dict(data.get("answered") or {}),
        skipped=dict(data.get("skipped") or {}),
        drip_quota_remaining=int(data.get("drip_quota_remaining") or 0),
        drip_quota_resets_at=str(data.get("drip_quota_resets_at") or ""),
        drip_filter_category=str(data.get("drip_filter_category") or ""),
        completion_pct=dict(data.get("completion_pct") or {}),
    )


def save_state(state: InterviewState) -> None:
    """Atomic write to ``~/.janus/interviews/_state/<gateway>__<chat>.json``."""
    path = state_path(state.gateway, state.chat_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "gateway": state.gateway,
        "chat_id": state.chat_id,
        "mode": state.mode,
        "started_at": state.started_at,
        "current_category": state.current_category,
        "current_question_id": state.current_question_id,
        "answered": state.answered,
        "skipped": state.skipped,
        "drip_quota_remaining": state.drip_quota_remaining,
        "drip_quota_resets_at": state.drip_quota_resets_at,
        "drip_filter_category": state.drip_filter_category,
        "completion_pct": state.completion_pct,
    }
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _now_iso(when: Optional[_dt.datetime] = None) -> str:
    when = when or _dt.datetime.now(_dt.timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> Optional[_dt.datetime]:
    if not s:
        return None
    try:
        s = s.rstrip("Z")
        return _dt.datetime.fromisoformat(s).replace(tzinfo=_dt.timezone.utc)
    except (ValueError, TypeError):
        return None


def mark_answered(state: InterviewState, fqid: str, card_id: str = "",
                  *, when: Optional[_dt.datetime] = None) -> None:
    """Record that a question was answered. Keep the card_id for traceability."""
    state.answered[fqid] = {
        "answered_at": _now_iso(when),
        "card_id": str(card_id),
    }
    # Clear any prior skip — answering supersedes a skip.
    state.skipped.pop(fqid, None)


def mark_skipped(state: InterviewState, fqid: str,
                 *, when: Optional[_dt.datetime] = None) -> None:
    """Record that a question was skipped. 7-day cooldown before re-asking."""
    state.skipped[fqid] = {
        "skipped_at": _now_iso(when),
    }


def is_eligible(
    state: InterviewState,
    category: str,
    question: Question,
    *,
    now: Optional[_dt.datetime] = None,
    check_cards_layer: bool = True,
) -> bool:
    """Should this question be asked next?

    Skipped if any of:
      1. Already answered AND (no recheck_days OR recheck not yet elapsed)
      2. Skipped within ``SKIP_COOLDOWN_DAYS`` (default 7)
      3. (Smart-skip) An existing card already covers (category, question.id)

    The cards-layer check is the v1.18 belt-and-suspenders: if extraction
    already learned the user's coffee preference from organic
    conversation, don't pop the wizard's "how do you take coffee" prompt.
    Pass ``check_cards_layer=False`` for tests that don't want to hit
    the index.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc)
    fqid = question.fqid(category)

    # 1. Already answered?
    if fqid in state.answered:
        if question.recheck_days is None:
            return False
        last = _parse_iso(state.answered[fqid].get("answered_at"))
        if last is not None:
            elapsed_days = (now - last).total_seconds() / 86400
            if elapsed_days < question.recheck_days:
                return False

    # 2. Skipped recently?
    if fqid in state.skipped:
        last = _parse_iso(state.skipped[fqid].get("skipped_at"))
        if last is not None:
            elapsed_days = (now - last).total_seconds() / 86400
            if elapsed_days < SKIP_COOLDOWN_DAYS:
                return False

    # 3. Card already exists for this (category, subject)?
    if check_cards_layer:
        try:
            from . import memory_index
            rows = memory_index.lookup_by_subject(category, question.id)
            if rows:
                return False
        except Exception:
            pass

    return True


def next_question(
    state: InterviewState,
    library: Optional[dict[str, Category]] = None,
    *,
    category_filter: Optional[str] = None,
    now: Optional[_dt.datetime] = None,
    check_cards_layer: bool = True,
) -> Optional[tuple[Category, Question]]:
    """Find the next eligible question.

    With ``category_filter`` set, restricts to that category. Otherwise
    walks SUPPORTED_CATEGORIES in order. Returns ``None`` when nothing
    is eligible (all answered / cooldown / cards-layer-covered).
    """
    if library is None:
        library = load_all()

    if category_filter:
        cats_to_check: tuple[str, ...] = (category_filter,)
    else:
        cats_to_check = SUPPORTED_CATEGORIES

    for cat_name in cats_to_check:
        cat = library.get(cat_name)
        if cat is None:
            continue
        for q in cat.questions:
            if is_eligible(state, cat_name, q, now=now,
                           check_cards_layer=check_cards_layer):
                return cat, q
    return None


def compute_completion(
    state: InterviewState,
    library: Optional[dict[str, Category]] = None,
    *,
    include_cards_layer: bool = True,
) -> dict[str, float]:
    """Per-category answered_count / total_questions ratio.

    The denominator is the bundled question count for that category;
    user-added questions don't inflate the meter (the meter measures
    DEFAULT coverage). Re-saved into ``state.completion_pct`` by callers
    that want it persisted.

    v1.24.2 — `include_cards_layer` (default True) extends the count
    to include questions whose subject already has a memory card
    (cross-gateway answers). Pre-v1.24.2 the meter counted ONLY
    questions answered through THIS gateway's state file; if you
    answered via Telegram and looked at the web meter, it was
    misleadingly empty. Now both gateways see the same coverage.
    Tests that need pure state-only counting pass include_cards_layer=False.
    """
    if library is None:
        library = load_all()
    out: dict[str, float] = {}
    now = _dt.datetime.now(_dt.timezone.utc)

    # v1.24.2: pre-fetch all cards per category once, not per question.
    cards_by_subject: dict[str, set[str]] = {}
    if include_cards_layer:
        try:
            from . import memory_index
            try:
                memory_index.reconcile()
            except Exception:
                pass
            for r in (memory_index.list_all() or []):
                t = r.get("type", "")
                s = r.get("subject", "")
                if t and s:
                    cards_by_subject.setdefault(t, set()).add(s)
        except Exception:
            cards_by_subject = {}

    for cat_name in SUPPORTED_CATEGORIES:
        cat = library.get(cat_name)
        if cat is None or not cat.questions:
            out[cat_name] = 0.0
            continue
        cat_cards = cards_by_subject.get(cat_name, set())
        # Count "currently answered AND still fresh" against total.
        answered_fresh = 0
        for q in cat.questions:
            fqid = q.fqid(cat_name)
            counted = False
            if fqid in state.answered:
                if q.recheck_days is None:
                    counted = True
                else:
                    last = _parse_iso(
                        state.answered[fqid].get("answered_at"),
                    )
                    if last is None:
                        counted = True
                    else:
                        elapsed_days = (now - last).total_seconds() / 86400
                        if elapsed_days < q.recheck_days:
                            counted = True
            # v1.24.2: cards-layer match counts even if state is empty
            # (cross-gateway). Each question's id maps to a card subject;
            # if a card exists we treat the question as answered.
            if not counted and q.id in cat_cards:
                counted = True
            if counted:
                answered_fresh += 1
        out[cat_name] = answered_fresh / len(cat.questions)
    return out


def overall_completion(
    state: InterviewState,
    library: Optional[dict[str, Category]] = None,
) -> float:
    """Average completion across all 8 categories. Drives drip auto-pause."""
    pcts = compute_completion(state, library)
    if not pcts:
        return 0.0
    return sum(pcts.values()) / len(pcts)


def reset_drip_quota(state: InterviewState, per_day: int = DRIP_DEFAULT_PER_DAY,
                     *, now: Optional[_dt.datetime] = None) -> None:
    """Reset the daily drip quota. Called when the previous quota window expired."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    # Next midnight UTC
    next_day = (now + _dt.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    state.drip_quota_remaining = per_day
    state.drip_quota_resets_at = _now_iso(next_day)


def quota_window_expired(state: InterviewState,
                         *, now: Optional[_dt.datetime] = None) -> bool:
    """Has the drip quota's reset time passed?"""
    if not state.drip_quota_resets_at:
        return True
    now = now or _dt.datetime.now(_dt.timezone.utc)
    resets = _parse_iso(state.drip_quota_resets_at)
    if resets is None:
        return True
    return now >= resets


# ---------- Phase 4: drip-mode helpers ----------


# Sentinel words that intercept drip answer flow without going to chat.
DRIP_SKIP_TOKENS = ("/skip", "skip drip", "skip question")
DRIP_CANCEL_TOKENS = ("/cancel drip", "stop drip", "/interview pause")


def consume_pending_drip_answer(
    gateway: str,
    chat_id: str,
    user_input: str,
    *,
    library: Optional[dict[str, "Category"]] = None,
) -> tuple[bool, str]:
    """If drip is active with a pending question, treat ``user_input`` as
    the answer. Build a high-confidence card and mark answered.

    Returns ``(handled, ack)`` —
      - ``handled=True`` when state had a pending question (caller may
        still pass user_input to the chat loop for normal conversation;
        we don't intercept the chat itself, just record the card)
      - ``ack`` is a short string the gateway can prepend to its reply
        ("got it — saved as identity.role"), or empty when not handled.

    Special tokens (``/skip``, ``stop drip``) bypass card creation:
      - ``/skip`` → mark_skipped + ack the skip
      - ``stop drip`` / ``/interview pause`` → flip mode to idle
    """
    state = load_state(gateway, chat_id)
    if state.mode != "drip":
        return False, ""
    if not state.current_question_id:
        return False, ""

    if library is None:
        library = load_all()
    if not library:
        return False, ""

    fqid = state.current_question_id
    if "." not in fqid:
        state.current_question_id = ""
        save_state(state)
        return False, ""
    cat_name, qid = fqid.split(".", 1)
    cat = library.get(cat_name)
    q = cat.find(qid) if cat else None
    if q is None:
        state.current_question_id = ""
        save_state(state)
        return False, ""

    text = (user_input or "").strip()
    low = text.lower()

    if low in DRIP_SKIP_TOKENS or low == "skip":
        mark_skipped(state, fqid)
        state.current_question_id = ""
        save_state(state)
        return True, f"(skipped {cat_name}/{qid} — I'll ask again later)"

    if low in DRIP_CANCEL_TOKENS or low == "stop":
        state.mode = "idle"
        state.current_question_id = ""
        save_state(state)
        return True, "(drip paused — resume with /interview daily)"

    if not text:
        # Empty / whitespace → treat as skip but quietly.
        mark_skipped(state, fqid)
        state.current_question_id = ""
        save_state(state)
        return True, ""

    # Substantive answer → apply as card.
    answer_text = text
    if q.mode == "choices" and text.isdigit():
        idx = int(text) - 1
        if 0 <= idx < len(q.choices):
            answer_text = q.choices[idx]

    try:
        from . import memory, memory_extract
        proposal = memory_extract.CardProposal(
            type=cat_name,
            subject=q.id,
            content=answer_text,
            confidence=0.9,
            importance=q.importance,
            durability=q.durability,
            scope="global",
            origin_kind="user_turn",
        )
        written = memory.apply_cards([proposal], gateway=gateway)
        card_id = written[0] if written else ""
        mark_answered(state, fqid, card_id=card_id)
        state.current_question_id = ""
        save_state(state)
        return True, f"got it — saved as {cat_name}/{qid}."
    except Exception:
        # apply failed; record as skip so we don't get stuck.
        mark_skipped(state, fqid)
        state.current_question_id = ""
        save_state(state)
        return True, ""


def get_drip_question(
    gateway: str,
    chat_id: str,
    *,
    library: Optional[dict[str, "Category"]] = None,
    per_day_default: int = DRIP_DEFAULT_PER_DAY,
) -> Optional[tuple[str, str]]:
    """Pick the next drip question if drip is active and quota allows.

    Returns ``(question_text, fqid)`` or ``None``. Side effects:
      - resets the quota window at midnight (UTC)
      - auto-pauses drip when overall completion ≥ DRIP_AUTO_PAUSE_PCT
      - decrements ``drip_quota_remaining`` and stamps
        ``current_question_id`` so the next user turn knows what to
        treat as an answer

    Caller is responsible for prepending the returned text to the
    assistant's outgoing reply.
    """
    # v1.41.7 — global kill switch. Default OFF; users opt in via
    # JANUS_INTERVIEW_DRIP_ENABLED=1. See DRIP_ENABLED definition above.
    if not DRIP_ENABLED:
        return None
    state = load_state(gateway, chat_id)
    if state.mode != "drip":
        return None
    if library is None:
        library = load_all()
    if not library:
        return None

    # Auto-pause when mostly complete.
    if overall_completion(state, library) >= DRIP_AUTO_PAUSE_PCT:
        state.mode = "idle"
        state.completion_pct = compute_completion(state, library)
        save_state(state)
        return None

    # Reset quota daily.
    if quota_window_expired(state):
        reset_drip_quota(state, per_day=per_day_default)

    if state.drip_quota_remaining <= 0:
        return None

    # Honor optional category filter (set by /interview <category> on
    # gateways — restricts drip to a single category until drip ends).
    cat_filter = state.drip_filter_category or None
    nxt = next_question(state, library, category_filter=cat_filter)
    if nxt is None:
        # No eligible question (everything covered / cooldown). Auto-pause.
        state.mode = "idle"
        state.drip_filter_category = ""
        state.completion_pct = compute_completion(state, library)
        save_state(state)
        return None

    cat, q = nxt
    fqid = q.fqid(cat.name)
    state.drip_quota_remaining -= 1
    state.current_category = cat.name
    state.current_question_id = fqid
    save_state(state)
    return q.question, fqid


def render_drip_question(question_text: str) -> str:
    """Render a drip question as a one-line append the gateway can put
    at the end of the assistant's reply.

    Includes hint about /skip / stop drip controls.
    """
    return (
        f"\n\n💬 _Quick question:_ {question_text}\n"
        f"_(answer normally, type 'skip' to skip, 'stop drip' to pause)_"
    )
