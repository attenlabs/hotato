"""``hotato conversation verify``: digest-verify a conversation artifact.

Pins the evidence-kernel posture on the CLI: an intact artifact verifies (exit
0); a tampered child (digest mismatch) or a missing child is REFUSED (exit 2) --
never silently accepted.
"""

import json
from importlib import resources

from hotato import cli


def _bundled_wav() -> str:
    return str(resources.files("hotato").joinpath(
        "data", "audio", "01-hard-interruption.example.wav"))


def _data(name: str) -> str:
    import os
    return os.path.join(os.path.dirname(__file__), "data", "conversation", name)


def _build_artifact(tmp_path) -> str:
    """Produce a real conversation artifact via `hotato test run` and return its
    directory."""
    out = tmp_path / "conv-artifact"
    code = cli.main([
        "test", "run", _data("refund.conversation-test.yaml"),
        "--agent", "support-v3", "--audio", _bundled_wav(),
        "--trace", _data("refund.voice_trace.jsonl"),
        "--transcript", _data("refund.transcript.json"),
        "--out", str(out), "--created-at", "2026-07-12T00:00:00Z",
    ])
    assert code == 0
    return str(out)


def test_intact_artifact_verifies(tmp_path, capsys):
    art = _build_artifact(tmp_path)
    capsys.readouterr()  # drop the test-run output
    code = cli.main(["conversation", "verify", art])
    assert code == 0
    assert "VERIFIED" in capsys.readouterr().out


def test_intact_artifact_verifies_json(tmp_path, capsys):
    art = _build_artifact(tmp_path)
    capsys.readouterr()
    code = cli.main(["conversation", "verify", art, "--format", "json"])
    assert code == 0
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["ok"] is True and verdict["refused"] is False
    assert set(verdict["verified"]) == {
        "audio", "trace", "transcript", "timing", "assertions"}


def test_tampered_child_is_refused(tmp_path, capsys):
    art = _build_artifact(tmp_path)
    capsys.readouterr()
    # tamper: append a byte to a bound child so its digest no longer matches
    import os
    with open(os.path.join(art, "transcript.json"), "a", encoding="utf-8") as fh:
        fh.write("tampered")
    code = cli.main(["conversation", "verify", art])
    assert code == 2
    out = capsys.readouterr().out
    assert "REFUSED" in out and "MISMATCH" in out


def test_tampered_child_is_refused_json(tmp_path, capsys):
    art = _build_artifact(tmp_path)
    capsys.readouterr()
    import os
    with open(os.path.join(art, "assertions.json"), "a", encoding="utf-8") as fh:
        fh.write(" ")
    code = cli.main(["conversation", "verify", art, "--format", "json"])
    assert code == 2
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["refused"] is True
    assert verdict["mismatches"]
    assert verdict["mismatches"][0]["artifact"] == "assertions"


def test_missing_child_is_refused(tmp_path, capsys):
    art = _build_artifact(tmp_path)
    capsys.readouterr()
    import os
    os.remove(os.path.join(art, "trace.jsonl"))
    code = cli.main(["conversation", "verify", art])
    assert code == 2
    assert "MISSING" in capsys.readouterr().out


def test_missing_directory_is_exit_2(tmp_path):
    code = cli.main(["conversation", "verify", str(tmp_path / "nope")])
    assert code == 2


def test_explicit_output_works_with_uncreatable_home_and_stays_verifiable(
        tmp_path, monkeypatch, capsys):
    """The default rubric cache is auxiliary state. A read-only container HOME
    must not block an explicitly located conversation artifact, and bypassing
    that cache must not relax digest verification."""
    blocked_home = tmp_path / "home-is-a-file"
    blocked_home.write_text("no directory can be created below this path")
    monkeypatch.setenv("HOME", str(blocked_home))
    monkeypatch.setenv("USERPROFILE", str(blocked_home))

    class _LocalJudge:
        provider = "test-local"
        model = "pinned-test-model"

        def model_digest(self):
            return "sha256:test-model"

        def complete(self, system, user):
            return json.dumps({
                "verdict": "pass",
                "rationale": "the supplied transcript meets the criterion",
                "citations": [],
            })

    monkeypatch.setattr(cli, "_build_judge", lambda args: _LocalJudge())

    art = _build_artifact(tmp_path)
    captured = capsys.readouterr()
    assert "default rubric cache is unavailable" in captured.err

    code = cli.main(["conversation", "verify", art, "--format", "json"])
    assert code == 0
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["ok"] is True and verdict["refused"] is False

    # The fallback changes only optional verdict persistence. The artifact's
    # digest wall remains active under the same uncreatable HOME.
    import os
    with open(os.path.join(art, "transcript.json"), "a", encoding="utf-8") as fh:
        fh.write("tampered")
    code = cli.main(["conversation", "verify", art, "--format", "json"])
    assert code == 2
    refused = json.loads(capsys.readouterr().out)
    assert refused["refused"] is True
    assert refused["mismatches"][0]["artifact"] == "transcript"


def test_explicit_unwritable_cache_request_remains_strict(
        tmp_path, monkeypatch, capsys):
    """Only the implicit default cache may degrade. An explicit cache path is
    a persistence request and must still fail instead of silently discarding
    the replay/drift baseline."""
    monkeypatch.setenv("HOME", str(tmp_path / "otherwise-writable-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "otherwise-writable-home"))
    blocked_cache_parent = tmp_path / "cache-parent-is-a-file"
    blocked_cache_parent.write_text("not a directory")
    out = tmp_path / "conv-artifact"
    code = cli.main([
        "test", "run", _data("refund.conversation-test.yaml"),
        "--agent", "support-v3", "--audio", _bundled_wav(),
        "--trace", _data("refund.voice_trace.jsonl"),
        "--transcript", _data("refund.transcript.json"),
        "--out", str(out),
        "--cache-dir", str(blocked_cache_parent / "cache"),
    ])
    assert code == 2
    assert not out.exists()
    assert "error:" in capsys.readouterr().err
