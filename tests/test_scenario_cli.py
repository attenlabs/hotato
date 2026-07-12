"""``hotato scenario init`` / ``hotato scenario validate``: author + validate
conversation-test files, mirroring ``assert init`` / a validation pass.

Pins: init writes a starter that itself round-trips through the loader (parse +
validate), carries the two SEPARATE assertion lanes and a boolean success (no
overall_score), and includes the commented ``# inconclusive_policy: fail`` line;
validate accepts a good file / directory (exit 0) and rejects a malformed one
(exit 2).
"""

import json

from hotato import cli
from hotato import conversation_test as CT


def _good_doc(name="ok"):
    return {
        "kind": "hotato.conversation-test", "version": 1, "id": name,
        "agent": "a",
        "assertions": {"deterministic": [
            {"id": "refunded", "kind": "tool_call", "name": "issue_refund",
             "dimension": "outcome"},
        ]},
    }


# --- scenario init ----------------------------------------------------------

def test_init_writes_a_starter_that_round_trips(tmp_path):
    out = tmp_path / "conversation-test.yaml"
    code = cli.main(["scenario", "init", "refund-flow", "--agent", "support-v3",
                     "--out", str(out)])
    assert code == 0
    assert out.is_file()
    # the starter loads + validates through the real loader (not just parses)
    doc = CT.load_conversation_test_file(str(out))
    assert doc["id"] == "refund-flow"
    assert doc["agent"] == "support-v3"
    # both SEPARATE lanes present; success is a boolean over named conditions
    assert "deterministic" in doc["assertions"]
    assert "rubric" in doc["assertions"]
    assert "overall_score" not in json.dumps(doc)


def test_init_includes_commented_inconclusive_policy_line(tmp_path):
    out = tmp_path / "s.yaml"
    cli.main(["scenario", "init", "--out", str(out)])
    text = out.read_text(encoding="utf-8")
    assert "# inconclusive_policy: fail" in text
    # deterministic checks are tagged across the report dimensions
    for dim in ("policy", "outcome", "conversation", "speech"):
        assert f"dimension: {dim}" in text


def test_init_refuses_to_overwrite_without_force(tmp_path):
    out = tmp_path / "s.yaml"
    out.write_text("existing", encoding="utf-8")
    code = cli.main(["scenario", "init", "--out", str(out)])
    assert code == 2
    assert out.read_text(encoding="utf-8") == "existing"
    # --force overwrites
    code = cli.main(["scenario", "init", "--out", str(out), "--force"])
    assert code == 0
    assert out.read_text(encoding="utf-8") != "existing"


def test_init_json_format(tmp_path, capsys):
    out = tmp_path / "s.yaml"
    code = cli.main(["scenario", "init", "myflow", "--out", str(out),
                     "--format", "json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "scenario-init"
    assert payload["scenario_id"] == "myflow"
    assert payload["path"] == str(out)


# --- scenario validate ------------------------------------------------------

def test_validate_accepts_a_good_file(tmp_path, capsys):
    p = tmp_path / "ok.json"
    p.write_text(json.dumps(_good_doc()), encoding="utf-8")
    code = cli.main(["scenario", "validate", str(p)])
    assert code == 0
    assert "1/1 valid" in capsys.readouterr().out


def test_validate_rejects_a_malformed_file(tmp_path, capsys):
    p = tmp_path / "bad.json"
    # missing required 'agent'
    p.write_text(json.dumps({"kind": "hotato.conversation-test", "version": 1,
                             "id": "x", "assertions": {"deterministic": []}}),
                 encoding="utf-8")
    code = cli.main(["scenario", "validate", str(p)])
    assert code == 2
    assert "BAD" in capsys.readouterr().out


def test_validate_rejects_overall_score(tmp_path):
    p = tmp_path / "score.json"
    doc = _good_doc()
    doc["overall_score"] = 0.9
    p.write_text(json.dumps(doc), encoding="utf-8")
    code = cli.main(["scenario", "validate", str(p)])
    assert code == 2


def test_validate_directory_all_valid(tmp_path, capsys):
    (tmp_path / "a.json").write_text(json.dumps(_good_doc("a")), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps(_good_doc("b")), encoding="utf-8")
    code = cli.main(["scenario", "validate", str(tmp_path)])
    assert code == 0
    assert "2/2 valid" in capsys.readouterr().out


def test_validate_directory_one_bad_is_exit_2(tmp_path, capsys):
    (tmp_path / "a.json").write_text(json.dumps(_good_doc("a")), encoding="utf-8")
    (tmp_path / "bad.json").write_text(json.dumps({"kind": "nope"}),
                                       encoding="utf-8")
    code = cli.main(["scenario", "validate", str(tmp_path)])
    assert code == 2
    out = capsys.readouterr().out
    assert "1/2 valid" in out


def test_validate_json_format(tmp_path, capsys):
    p = tmp_path / "ok.json"
    p.write_text(json.dumps(_good_doc()), encoding="utf-8")
    code = cli.main(["scenario", "validate", str(p), "--format", "json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["results"][0]["ok"] is True


def test_validate_empty_directory_is_usage_error(tmp_path):
    code = cli.main(["scenario", "validate", str(tmp_path)])
    assert code == 2


def test_init_then_validate_the_starter(tmp_path):
    out = tmp_path / "s.yaml"
    assert cli.main(["scenario", "init", "roundtrip", "--out", str(out)]) == 0
    assert cli.main(["scenario", "validate", str(out)]) == 0
