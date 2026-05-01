import time
from janus.triggers import base as trg_base
from janus.triggers import runtime as trg_runtime


def test_cron_field_match_star():
    assert trg_base._cron_field_match("*", 5, 0, 59)


def test_cron_field_match_value():
    assert trg_base._cron_field_match("5", 5, 0, 59)
    assert not trg_base._cron_field_match("5", 6, 0, 59)


def test_cron_field_match_step():
    # */15 from 0 → matches 0, 15, 30, 45
    assert trg_base._cron_field_match("*/15", 0, 0, 59)
    assert trg_base._cron_field_match("*/15", 30, 0, 59)
    assert not trg_base._cron_field_match("*/15", 7, 0, 59)


def test_cron_field_match_csv():
    assert trg_base._cron_field_match("1,3,5", 3, 0, 59)
    assert not trg_base._cron_field_match("1,3,5", 4, 0, 59)


def test_cron_due_full_expression():
    # "0 7 * * *" matches minute=0, hour=7, any day
    assert trg_base.cron_due("0 7 * * *", (0, 7, 15, 6, 3))
    assert not trg_base.cron_due("0 7 * * *", (1, 7, 15, 6, 3))


def test_log_pattern_match():
    lines = ["error: x", "ok: y", "ERROR: z"]
    out = trg_base.log_pattern_match(r"(?i)error", lines)
    assert len(out) == 2


def test_interval_due():
    assert trg_base.interval_due("60", None, "2026-04-30T12:00:00+00:00")
    assert trg_base.interval_due("60", "2026-04-30T11:00:00+00:00",
                                 "2026-04-30T12:00:00+00:00")
    assert not trg_base.interval_due("60", "2026-04-30T11:59:30+00:00",
                                     "2026-04-30T12:00:00+00:00")


def test_load_triggers_yaml(janus_home):
    yaml = """name: morning
kind: cron
when: 0 7 * * *
skill: brief
request: "send my brief"
dedupe_seconds: 3600
enabled: true
"""
    p = janus_home / "triggers" / "morning.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml, encoding="utf-8")
    triggers = trg_base.list_triggers()
    assert len(triggers) == 1
    assert triggers[0].name == "morning"
    assert triggers[0].kind == "cron"
    assert triggers[0].dedupe_seconds == 3600


def test_state_roundtrip(janus_home):
    trg_base.write_state({"morning": "2026-04-30T07:00:00+00:00"})
    s = trg_base.read_state()
    assert s["morning"].startswith("2026-04-30")


def test_check_trigger_dedupe(janus_home):
    yaml = """name: x
kind: interval
when: "10"
skill: s
request: r
dedupe_seconds: 60
enabled: true
"""
    p = janus_home / "triggers" / "x.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml, encoding="utf-8")
    t = trg_base.list_triggers()[0]

    # First check with no last fire — should fire.
    state = {}
    fires, _ = trg_runtime._check_trigger(t, state, [])
    assert fires

    # Second check immediately after — dedupe blocks it.
    state = {"x": trg_runtime._now_iso()}
    fires, detail = trg_runtime._check_trigger(t, state, [])
    assert not fires
    assert detail.get("reason") == "dedupe"
