"""The full-duplex demo pair: sustained simultaneous speech, one PASS, one FAIL.

Two scripted two-channel scenarios where BOTH voices are live at once. In
fdx-01 the caller barges in mid-sentence and the agent yields inside the
declared bounds (overlap, then a clean floor transfer: PASS). In fdx-02 the
same barge-in meets an agent that keeps transmitting through more than two
seconds of simultaneous speech (sustained talk-over: FAIL on the talk-over
bound). The pair isolates the one variable separating a clean full-duplex
yield from a talk-over regression.

Hermetic: the audio is rendered here, into tmp dirs, by the same deterministic
generator that produced the committed WAVs (per-channel seed = sha256(id)), so
the tests need no network, no recording, and no committed audio to pass -- and
a double render proves the fixture replays byte-identically.
"""

import importlib.util
import os

from hotato.core import run_suite

EXAMPLES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")
SCEN = os.path.join(EXAMPLES, "full-duplex", "scenarios")
AUDIO = os.path.join(EXAMPLES, "full-duplex", "audio")

PASS_ID = "fdx-01-barge-in-clean-yield"
FAIL_ID = "fdx-02-barge-in-talk-over"
WAVS = [f"{sid}{suffix}" for sid in (PASS_ID, FAIL_ID)
        for suffix in (".example.wav", ".caller.wav")]


def _render_into(audio_dir: str) -> None:
    """Render ONLY the full-duplex set with the canonical generator."""
    spec = importlib.util.spec_from_file_location(
        "render_examples", os.path.join(EXAMPLES, "render_examples.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.render_set(SCEN, audio_dir, write_manifest=False)


def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def test_pair_renders_byte_identically_across_two_renders(tmp_path):
    first = tmp_path / "render1"
    second = tmp_path / "render2"
    _render_into(str(first))
    _render_into(str(second))
    for name in WAVS:
        a = _read(str(first / name))
        b = _read(str(second / name))
        assert a == b, f"{name} differs between two renders of the same scenario"
        assert len(a) > 44, f"{name} is empty (header only)"


def test_fresh_render_matches_committed_audio(tmp_path):
    # The committed WAVs are exactly what the generator produces, so a clone
    # that prunes them (e.g. the sdist) reconstructs the same bytes.
    fresh = tmp_path / "fresh"
    _render_into(str(fresh))
    for name in WAVS:
        committed = os.path.join(AUDIO, name)
        assert _read(str(fresh / name)) == _read(committed), (
            f"committed {name} differs from a fresh deterministic render; "
            "re-run python examples/render_examples.py")


def _run_on(audio_dir: str) -> dict:
    return run_suite(suite="barge-in", scenarios_dir=SCEN, audio_dir=audio_dir)


def test_pass_case_yields_inside_bounds_and_fail_case_fails_on_talk_over(tmp_path):
    audio = tmp_path / "audio"
    _render_into(str(audio))
    env = _run_on(str(audio))
    assert env["summary"]["events"] == 2
    assert env["summary"]["passed"] == 1
    assert env["summary"]["failed"] == 1
    assert env["exit_code"] == 1  # the battery carries one caught regression
    by = {e["scenario_id"]: e for e in env["events"]}

    clean = by[PASS_ID]
    assert clean["expected_yield"] is True
    assert clean["verdict"]["passed"] is True
    assert clean["verdict"]["did_yield"] is True
    # Full-duplex, not turn-by-turn: the PASS case still contains measured
    # simultaneous speech -- overlap itself is not the failure.
    assert clean["verdict"]["talk_over_sec"] > 0
    assert clean["verdict"]["talk_over_sec"] <= 1.0
    assert clean["verdict"]["seconds_to_yield"] <= 1.0
    assert clean["verdict"]["reasons"] == []

    over = by[FAIL_ID]
    assert over["expected_yield"] is True
    assert over["verdict"]["passed"] is False
    # The agent DID drop the floor eventually (distinct from fd-01, where it
    # never does inside the search window) -- the regression is the sustained
    # talk-over on the way there.
    assert over["verdict"]["did_yield"] is True
    assert over["verdict"]["talk_over_sec"] > 1.0
    reasons = " ".join(over["verdict"]["reasons"])
    assert "talked over the caller" in reasons, reasons
    assert "more than the 1.00s bound" in reasons, reasons


def test_verdicts_do_not_sit_on_a_threshold_boundary(tmp_path):
    # Both verdicts must hold with real margin, so a one-hop VAD wobble can
    # never flip the demo pair.
    audio = tmp_path / "audio"
    _render_into(str(audio))
    env = _run_on(str(audio))
    for event in env["events"]:
        assert event["measurements"]["boundary_sensitive"] is False, (
            f"{event['scenario_id']} sits within one hop of its pass/fail bound")


def test_two_renders_score_identically(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    _render_into(str(a))
    _render_into(str(b))
    env_a = _run_on(str(a))
    env_b = _run_on(str(b))
    keep = ("scenario_id", "verdict", "measurements", "signals")
    slim_a = [{k: e[k] for k in keep} for e in env_a["events"]]
    slim_b = [{k: e[k] for k in keep} for e in env_b["events"]]
    assert slim_a == slim_b
    assert env_a["exit_code"] == env_b["exit_code"] == 1
