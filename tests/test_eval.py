import json
from janus import eval as eval_mod, config


def test_label_jaccard_identity():
    assert eval_mod.label_jaccard(["alpha"], ["alpha"]) == 1.0


def test_label_jaccard_disjoint():
    assert eval_mod.label_jaccard(["alpha"], ["beta"]) == 0.0


def test_classify_drift():
    assert eval_mod.classify_interp_drift(1.0) == 0
    assert eval_mod.classify_interp_drift(0.5) == 1
    assert eval_mod.classify_interp_drift(0.05) == 2


def _write_record(rec):
    config.ensure_home()
    with config.LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def test_replay_emits_report(janus_home, fake_llm):
    _write_record({
        "ts": "t1",
        "request": "merge two files",
        "interpretations": [
            {"label": "concat by row", "action": "...", "risk": "-"},
            {"label": "join by key", "action": "...", "risk": "-"},
        ],
        "choice": 1,
        "trace": [],
        "output": "done",
    })
    fake_llm.append({
        "content": (
            '{"interpretations": ['
            '{"label": "concat by row", "action": "x", "risk": "-"},'
            '{"label": "join by key", "action": "y", "risk": "-"}'
            "]}"
        ),
    })
    report = eval_mod.replay(last_n=10, write_report=False)
    assert report.n_records == 1
    assert report.by_record[0].new_labels == ["concat by row", "join by key"]
    assert report.by_record[0].interp_drift == 0
    assert report.by_record[0].interp_overlap == 1.0
