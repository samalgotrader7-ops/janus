"""Tests for janus.interviews parser + bundled library (v1.19.0 Phase 1).

Covers: frontmatter parsing, validation (missing fields, bad types,
duplicate ids, score clamping), category mismatch detection, bundled
library smoke (every shipped file parses), and bundled-install
idempotency.
"""

from __future__ import annotations
from pathlib import Path

import pytest

from janus import config, interviews


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Redirect interviews_dir to a temp area for each test."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(
        config, "INTERVIEWS_DIR", home / "interviews", raising=False,
    )
    return home


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


# ---------- Bundled library smoke ----------


class TestBundledFiles:
    def test_eight_categories_shipped(self):
        bundled = interviews.bundled_dir()
        files = sorted(p.stem for p in bundled.glob("*.md"))
        # All 8 v1.18 types must have a question file.
        assert files == sorted(interviews.SUPPORTED_CATEGORIES)

    @pytest.mark.parametrize("cat", list(interviews.SUPPORTED_CATEGORIES))
    def test_each_bundled_file_parses(self, cat, isolated_home):
        """Smoke — every shipped category file must parse cleanly."""
        bundled = interviews.bundled_dir() / f"{cat}.md"
        target = interviews.interviews_dir() / f"{cat}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(bundled.read_text(encoding="utf-8"), encoding="utf-8")

        loaded = interviews.load_category(cat)
        assert loaded.name == cat
        assert loaded.description  # description non-empty
        assert len(loaded.questions) >= 1

    @pytest.mark.parametrize("cat", list(interviews.SUPPORTED_CATEGORIES))
    def test_each_bundled_file_has_at_least_3_questions(self, cat, isolated_home):
        bundled = interviews.bundled_dir() / f"{cat}.md"
        target = interviews.interviews_dir() / f"{cat}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(bundled.read_text(encoding="utf-8"), encoding="utf-8")
        loaded = interviews.load_category(cat)
        # Cold-start needs enough surface area to be useful — 3+ Qs minimum.
        assert len(loaded.questions) >= 3

    @pytest.mark.parametrize("cat", list(interviews.SUPPORTED_CATEGORIES))
    def test_each_question_has_score_fields(self, cat, isolated_home):
        bundled = interviews.bundled_dir() / f"{cat}.md"
        target = interviews.interviews_dir() / f"{cat}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(bundled.read_text(encoding="utf-8"), encoding="utf-8")
        loaded = interviews.load_category(cat)
        for q in loaded.questions:
            assert 0.0 <= q.importance <= 1.0
            assert 0.0 <= q.durability <= 1.0


# ---------- Parser unit tests ----------


class TestParser:
    def test_basic_load(self, isolated_home):
        path = interviews.interviews_dir() / "identity.md"
        _write(path, """---
category: identity
description: who you are
version: 1
questions:
  name:
    question: "What should I call you?"
    mode: text
    importance: 0.9
    durability: 0.95
    recheck_days: null
---

# identity
""")
        cat = interviews.load_category("identity")
        assert cat.name == "identity"
        assert cat.description == "who you are"
        assert len(cat.questions) == 1
        q = cat.questions[0]
        assert q.id == "name"
        assert q.question == "What should I call you?"
        assert q.mode == "text"
        assert q.importance == 0.9
        assert q.durability == 0.95
        assert q.recheck_days is None

    def test_choices_mode(self, isolated_home):
        path = interviews.interviews_dir() / "preference.md"
        _write(path, """---
category: preference
description: ...
version: 1
questions:
  tone:
    question: "How should I sound?"
    mode: choices
    choices:
      - terse
      - friendly
      - formal
    importance: 0.7
    durability: 0.7
---

# preference
""")
        cat = interviews.load_category("preference")
        q = cat.questions[0]
        assert q.mode == "choices"
        assert q.choices == ["terse", "friendly", "formal"]

    def test_recheck_days_int(self, isolated_home):
        path = interviews.interviews_dir() / "goal.md"
        _write(path, """---
category: goal
description: x
version: 1
questions:
  q:
    question: "What goal?"
    mode: text
    recheck_days: 90
---
""")
        cat = interviews.load_category("goal")
        assert cat.questions[0].recheck_days == 90

    def test_score_clamping(self, isolated_home):
        path = interviews.interviews_dir() / "preference.md"
        _write(path, """---
category: preference
description: x
version: 1
questions:
  q:
    question: "?"
    mode: text
    importance: 1.5
    durability: -0.3
---
""")
        cat = interviews.load_category("preference")
        q = cat.questions[0]
        assert q.importance == 1.0
        assert q.durability == 0.0

    def test_fqid(self, isolated_home):
        path = interviews.interviews_dir() / "identity.md"
        _write(path, """---
category: identity
description: x
version: 1
questions:
  name:
    question: "?"
    mode: text
---
""")
        cat = interviews.load_category("identity")
        q = cat.questions[0]
        assert q.fqid("identity") == "identity.name"

    def test_find_by_id(self, isolated_home):
        path = interviews.interviews_dir() / "identity.md"
        _write(path, """---
category: identity
description: x
version: 1
questions:
  name:
    question: "What name?"
    mode: text
  role:
    question: "What role?"
    mode: text
---
""")
        cat = interviews.load_category("identity")
        assert cat.find("name").question == "What name?"
        assert cat.find("role").question == "What role?"
        assert cat.find("nope") is None

    def test_question_order_preserved(self, isolated_home):
        """Insertion-order matters — the runner walks them in declaration
        order. CPython 3.7+ dicts preserve insertion order."""
        path = interviews.interviews_dir() / "identity.md"
        _write(path, """---
category: identity
description: x
version: 1
questions:
  third:
    question: "Third?"
    mode: text
  first:
    question: "First?"
    mode: text
  second:
    question: "Second?"
    mode: text
---
""")
        cat = interviews.load_category("identity")
        ids_in_order = [q.id for q in cat.questions]
        assert ids_in_order == ["third", "first", "second"]


# ---------- Validation errors ----------


class TestValidationErrors:
    def test_missing_file_raises(self, isolated_home):
        with pytest.raises(interviews.InterviewLoadError, match="not found"):
            interviews.load_category("identity")

    def test_unsupported_category_raises(self, isolated_home):
        with pytest.raises(interviews.InterviewLoadError, match="unsupported"):
            interviews.load_category("not_a_real_category")

    def test_missing_frontmatter_raises(self, isolated_home):
        path = interviews.interviews_dir() / "identity.md"
        _write(path, "no frontmatter here, just text")
        with pytest.raises(interviews.InterviewLoadError, match="frontmatter"):
            interviews.load_category("identity")

    def test_category_mismatch_raises(self, isolated_home):
        path = interviews.interviews_dir() / "identity.md"
        _write(path, """---
category: preference
description: wrong category
version: 1
questions:
  q:
    question: "?"
    mode: text
---
""")
        with pytest.raises(interviews.InterviewLoadError, match="mismatch"):
            interviews.load_category("identity")

    def test_questions_must_be_dict(self, isolated_home):
        path = interviews.interviews_dir() / "identity.md"
        _write(path, """---
category: identity
description: x
version: 1
questions: not_a_dict
---
""")
        with pytest.raises(interviews.InterviewLoadError, match="dict"):
            interviews.load_category("identity")

    def test_question_missing_text_raises(self, isolated_home):
        path = interviews.interviews_dir() / "identity.md"
        _write(path, """---
category: identity
description: x
version: 1
questions:
  name:
    mode: text
---
""")
        with pytest.raises(interviews.InterviewLoadError, match="question"):
            interviews.load_category("identity")

    def test_invalid_mode_raises(self, isolated_home):
        path = interviews.interviews_dir() / "identity.md"
        _write(path, """---
category: identity
description: x
version: 1
questions:
  q:
    question: "?"
    mode: weirdmode
---
""")
        with pytest.raises(interviews.InterviewLoadError, match="mode"):
            interviews.load_category("identity")

    def test_choices_mode_requires_choices(self, isolated_home):
        path = interviews.interviews_dir() / "identity.md"
        _write(path, """---
category: identity
description: x
version: 1
questions:
  q:
    question: "?"
    mode: choices
---
""")
        with pytest.raises(interviews.InterviewLoadError, match="choices"):
            interviews.load_category("identity")

    def test_negative_recheck_days_raises(self, isolated_home):
        path = interviews.interviews_dir() / "identity.md"
        _write(path, """---
category: identity
description: x
version: 1
questions:
  q:
    question: "?"
    mode: text
    recheck_days: -5
---
""")
        with pytest.raises(interviews.InterviewLoadError, match="recheck_days"):
            interviews.load_category("identity")


# ---------- list_categories / load_all ----------


class TestLibraryHelpers:
    def test_list_categories_empty(self, isolated_home):
        assert interviews.list_categories() == []

    def test_list_categories_filters_to_supported(self, isolated_home):
        # An "unsupported" file should be ignored.
        _write(interviews.interviews_dir() / "identity.md", """---
category: identity
description: x
version: 1
questions:
  q:
    question: "?"
    mode: text
---
""")
        _write(interviews.interviews_dir() / "weird.md", "junk")
        cats = interviews.list_categories()
        assert "identity" in cats
        assert "weird" not in cats

    def test_load_all_skips_malformed(self, isolated_home):
        # One good, one bad file. load_all should skip the bad and
        # return the good one.
        _write(interviews.interviews_dir() / "identity.md", """---
category: identity
description: x
version: 1
questions:
  q:
    question: "?"
    mode: text
---
""")
        _write(interviews.interviews_dir() / "preference.md", "totally bogus")
        all_cats = interviews.load_all()
        assert "identity" in all_cats
        assert "preference" not in all_cats


# ---------- Bundled-install idempotency ----------


class TestBundledInstall:
    def test_initial_install_copies_eight_files(self, isolated_home):
        result = interviews.maybe_install_bundled()
        assert result["skipped"] is False
        assert result["installed"] == 8
        assert interviews.is_bundled_installed()

    def test_repeat_install_is_noop(self, isolated_home):
        interviews.maybe_install_bundled()
        result = interviews.maybe_install_bundled()
        assert result["skipped"] is True
        assert result["installed"] == 0

    def test_install_does_not_clobber_user_edits(self, isolated_home):
        # User has a hand-edited identity.md before install runs.
        custom = """---
category: identity
description: my edits
version: 1
questions:
  name:
    question: "Custom Q?"
    mode: text
---
"""
        _write(interviews.interviews_dir() / "identity.md", custom)
        interviews.maybe_install_bundled()
        # File preserved as-is.
        loaded = interviews.load_category("identity")
        assert loaded.description == "my edits"
        assert loaded.questions[0].question == "Custom Q?"

    def test_marker_written_after_install(self, isolated_home):
        marker = interviews.install_marker_path()
        assert not marker.exists()
        interviews.maybe_install_bundled()
        assert marker.exists()
        text = marker.read_text(encoding="utf-8")
        assert "files:" in text
