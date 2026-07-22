"""``hotato investigate`` / ``hotato investigate label``: one recording ->
ranked candidates with an honest capture origin and the K6 verdict gate, and
the human's label -> a real signed, CI-ready contract.

Covers the load-bearing behaviours from the spec:

* discovery persists state (.hotato/investigate-state.json, loop.py's
  precedent) and prints the EXACT next command for each candidate;
* the capture origin is authenticated, not asserted: a local file is
  ``operator_asserted_local``, a fixture clip already on disk is
  ``frozen_regression``, and a live stack pull is ``provider_pulled`` --
  never conflated with a signed, machine-verified fresh-recapture claim;
* K6: a suspected channel swap/crosstalk REFUSES the verdict path while
  still surfacing the advisory candidates (never a fabricated verdict);
* a not-scorable input is a clean exit-2 report, not a crash, and scan never
  runs on it;
* ``investigate label`` hands the human's --expect straight to
  ``contract.create_contract`` -- a real signed label-record, never
  fabricated -- and the produced bundle passes ``contract verify``;
* the persisted state file is ITSELF a valid FILE#N candidate ref that
  ``fixture.promote_candidate`` / ``contract.create_contract
  (from_candidate=...)`` can read directly, with no second ref resolver;
* the CLI wiring: ``hotato investigate SOURCE`` and ``hotato investigate
  label REF --expect ...`` both route correctly through one parser.

Discovery uses the bundled packaged fixture (present in every wheel/sdist),
so this test never depends on the heavy repo corpus.
"""

from __future__ import annotations

import json
import math
import os
import shlex
import shutil
import struct
import wave
from importlib import resources

import pytest

from hotato import cli
from hotato import contract as _contract
from hotato import fixture as _fixture
from hotato import investigate as _investigate

_HARD_INTERRUPTION = "01-hard-interruption.example.wav"


def _bundled_wav(dst_path: str, name: str = _HARD_INTERRUPTION) -> str:
    src = resources.files("hotato").joinpath("data", "audio", name)
    with resources.as_file(src) as p:
        shutil.copyfile(str(p), dst_path)
    return dst_path


def _tone(sr: int, dur: float, freq: float, amp: float = 0.3):
    n = int(sr * dur)
    return [amp * math.sin(2 * math.pi * freq * i / sr) for i in range(n)]


def _silence(sr: int, dur: float):
    return [0.0] * int(sr * dur)


def _write_stereo(path: str, sr: int, left, right) -> None:
    n = min(len(left), len(right))
    with wave.open(path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        frames = bytearray()
        for c, a in zip(left[:n], right[:n]):
            frames += struct.pack(
                "<hh", int(max(-1.0, min(1.0, c)) * 32767),
                int(max(-1.0, min(1.0, a)) * 32767),
            )
        wf.writeframes(bytes(frames))


def _swapped_channel_wav(path: str) -> str:
    """Caller-dominant / agent-brief-then-silent: trips the possible-swap
    heuristic (trust.py) and, at the stricter contract-mode bar, refuses the
    K6 verdict."""
    sr = 16000
    caller = _tone(sr, 8.0, 220.0)
    agent = _tone(sr, 0.5, 440.0) + _silence(sr, 7.5)
    _write_stereo(path, sr, caller, agent)
    return path


def _mono_wav(path: str) -> str:
    sr = 16000
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(struct.pack("<h", 1000) * sr)
    return path


@pytest.fixture()
def call_wav(tmp_path):
    return _bundled_wav(str(tmp_path / "call.wav"))


# --- discovery: candidates, capture origin, K6, persisted state ------------

def test_investigate_finds_candidates_and_persists_state(call_wav, tmp_path):
    state = str(tmp_path / ".hotato" / "investigate-state.json")
    result, code = _investigate.run_investigate(call_wav, state_path=state)

    assert code == 0
    assert result["run"] == 1
    assert result["total_candidates"] >= 1
    assert result["trust"]["scorable"] is True
    assert result["verdict_status"]["eligible"] is True
    assert result["capture_origin"]["kind"] == "operator_asserted_local"
    assert os.path.exists(state)

    # every shown candidate gets the EXACT next command
    assert len(result["next"]) == result["shown"]
    for i, n in enumerate(result["next"], 1):
        assert n["rank"] == i
        assert n["ref"] == f"{state}#{i}"
        # The ref (PATH#N) is shell-quoted so a state path with a space --
        # and the '#' itself -- survives copy-paste as one argument.
        assert n["command"] == (
            f"hotato investigate label {shlex.quote(f'{state}#{i}')} "
            "--expect yield"
        )

    st = json.loads(open(state, encoding="utf-8").read())
    assert st["schema"] == "hotato.investigate-state.v1"
    assert st["run"] == 1
    assert len(st["history"]) == 1


def test_investigate_persists_run_history_across_two_runs(call_wav, tmp_path):
    state = str(tmp_path / "state.json")
    _investigate.run_investigate(call_wav, state_path=state)
    r2, code2 = _investigate.run_investigate(call_wav, state_path=state)
    assert code2 == 0
    assert r2["run"] == 2
    st = json.loads(open(state, encoding="utf-8").read())
    assert [h["run"] for h in st["history"]] == [1, 2]
    assert st["created_at"] == st["history"][0]["at"]


def test_investigate_render_text_names_the_label_command(call_wav, tmp_path):
    state = str(tmp_path / "state.json")
    result, _ = _investigate.run_investigate(call_wav, state_path=state)
    text = _investigate.render_text(result)
    assert "capture origin: operator-asserted local file" in text
    assert "verdict path: eligible" in text
    assert (f"hotato investigate label {shlex.quote(f'{state}#1')} "
            "--expect yield") in text


# --- capture origin: frozen regression -------------------------------------

def test_investigate_recognizes_a_frozen_regression_clip(call_wav, tmp_path):
    out = tmp_path / "tests" / "hotato"
    audio_dir = out / "audio"
    scen_dir = out / "scenarios"
    audio_dir.mkdir(parents=True)
    scen_dir.mkdir(parents=True)
    clip = audio_dir / "refund-001.example.wav"
    shutil.copyfile(call_wav, str(clip))
    (scen_dir / "refund-001.json").write_text("{}", encoding="utf-8")

    state = str(tmp_path / "state.json")
    result, code = _investigate.run_investigate(str(clip), state_path=state)
    assert code == 0
    origin = result["capture_origin"]
    assert origin["kind"] == "frozen_regression"
    assert origin["scenario_path"].endswith("refund-001.json")


# --- K6: a suspected swap refuses the verdict, not the candidates ----------

def test_investigate_k6_refuses_verdict_but_keeps_candidates(tmp_path):
    wav = _swapped_channel_wav(str(tmp_path / "swap.wav"))
    state = str(tmp_path / "state.json")
    result, code = _investigate.run_investigate(wav, state_path=state,
                                                 min_gap=1.0)
    assert code == 0  # candidate-eligible: scan still ran
    assert result["trust"]["scorable"] is True
    assert result["verdict_status"]["eligible"] is False
    assert result["verdict_status"]["reason"]
    assert result["verdict_status"]["mode"] == "contract"
    assert result["total_candidates"] >= 1  # advisory candidates, not nulled

    text = _investigate.render_text(result)
    assert "verdict path: REFUSED" in text


def test_investigate_confirm_channels_restores_verdict_eligibility(tmp_path):
    wav = _swapped_channel_wav(str(tmp_path / "swap.wav"))
    state = str(tmp_path / "state.json")
    result, code = _investigate.run_investigate(
        wav, state_path=state, min_gap=1.0, channel_map_confirmed=True,
    )
    assert code == 0
    assert result["verdict_status"]["eligible"] is True


# --- not scorable: clean exit 2, scan never runs ---------------------------

def test_investigate_not_scorable_input_is_a_clean_exit_2(tmp_path):
    wav = _mono_wav(str(tmp_path / "mono.wav"))
    state = str(tmp_path / "state.json")
    result, code = _investigate.run_investigate(wav, state_path=state)
    assert code == 2
    assert result["trust"]["scorable"] is False
    assert result["total_candidates"] == 0
    assert result["candidates"] == []
    assert "single channel" in result["trust"]["not_scorable_reason"]


# --- usage errors: exactly one input mode ----------------------------------

def test_investigate_rejects_both_source_and_stack(call_wav):
    with pytest.raises(ValueError, match="not both"):
        _investigate.run_investigate(call_wav, stack="vapi", call_id="c1")


def test_investigate_rejects_neither_source_nor_stack():
    with pytest.raises(ValueError, match="SOURCE"):
        _investigate.run_investigate(None)


def test_investigate_rejects_bad_min_gap(call_wav):
    with pytest.raises(ValueError, match="--min-gap"):
        _investigate.run_investigate(call_wav, min_gap=0)


def test_investigate_rejects_missing_local_file(tmp_path):
    with pytest.raises(ValueError, match="no such file"):
        _investigate.run_investigate(str(tmp_path / "nope.wav"))


def test_investigate_rejects_unpullable_stack():
    with pytest.raises(ValueError, match="no direct fetch"):
        _investigate.run_investigate(None, stack="livekit", call_id="c1")


# --- --demo: the no-recording on-ramp (packaged sample, any cwd) -----------

def test_demo_recording_path_is_the_packaged_resource():
    # The demo sample resolves through importlib.resources to the WAV bundled
    # in the package (not a repo-relative path), so it is found from any cwd
    # after a bare install -- the same posture simulate --example takes.
    src = resources.files("hotato").joinpath(
        "data", "demo", "failing", "audio",
        "fd-01-missed-interruption.example.wav")
    with resources.as_file(src) as p:
        assert os.path.isfile(str(p))
    assert _investigate.demo_recording_path() == str(src)


def test_investigate_demo_scores_the_bundled_sample_from_a_bare_cwd(
    tmp_path, monkeypatch,
):
    # A cwd with NO repo files: --demo still resolves the packaged sample and
    # runs the identical investigate path a real recording does.
    monkeypatch.chdir(tmp_path)
    assert not os.path.exists(".hotato")
    state = str(tmp_path / ".hotato" / "investigate-state.json")
    result, code = _investigate.run_investigate(demo=True, state_path=state)

    # investigate's scorable exit code + a real caught moment
    assert code == 0
    assert result["run"] == 1
    assert result["trust"]["scorable"] is True
    assert result["total_candidates"] >= 1
    # the packaged sample is a previously-frozen fixture clip (a sibling
    # scenario names it): the honest origin, never a fabricated provider pull
    assert result["capture_origin"]["kind"] == "frozen_regression"

    # step two is reachable, and the headline leads with the STORY-WORTHY catch:
    # the real missed interruption (a barge-in the agent talked through), not a
    # passive end-of-call trailing silence. next[] keeps its salience order; only
    # the printed headline prefers the higher-severity kind.
    assert len(result["next"]) == result["shown"]
    text = _investigate.render_text(result)
    lead = result["next"][
        _investigate._story_lead_index(result["next"], result["candidates"])
    ]
    assert (result["candidates"][lead["rank"] - 1]["kind"]
            == "overlap_while_agent_talking")
    # the headlined next-step is a reachable label command for that catch
    assert lead["command"] in text
    assert "--expect yield" in text

    # state persisted under the bare cwd
    assert os.path.exists(state)
    st = json.loads(open(state, encoding="utf-8").read())
    assert st["schema"] == "hotato.investigate-state.v1"


def test_investigate_demo_rejects_a_positional_source(call_wav):
    with pytest.raises(ValueError, match="--demo"):
        _investigate.run_investigate(call_wav, demo=True)


def test_investigate_demo_rejects_stack(tmp_path):
    with pytest.raises(ValueError, match="--demo"):
        _investigate.run_investigate(demo=True, stack="vapi", call_id="c1")
    with pytest.raises(ValueError, match="--demo"):
        _investigate.run_investigate(demo=True, stack="vapi")


def test_cli_investigate_demo_mode(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code = cli.main(["investigate", "--demo", "--format", "json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["kind"] == "investigate"
    assert out["total_candidates"] >= 1
    assert out["next"][0]["command"].startswith("hotato investigate label ")
    assert os.path.exists(tmp_path / ".hotato" / "investigate-state.json")


def test_cli_investigate_demo_help_lists_the_example():
    parser = cli.build_parser()
    for action in parser._actions:
        if isinstance(action, __import__("argparse")._SubParsersAction):
            help_text = action.choices["investigate"].format_help()
            assert "hotato investigate --demo" in help_text


# --- the label step: a real signed, CI-ready contract ----------------------

def test_investigate_label_creates_a_scorable_contract(call_wav, tmp_path):
    state = str(tmp_path / "state.json")
    result, _ = _investigate.run_investigate(call_wav, state_path=state)
    ref = result["next"][0]["ref"]

    out_dir = str(tmp_path / "contracts")
    label_result = _investigate.run_investigate_label(
        ref, expect="yield", out_dir=out_dir,
    )
    assert label_result["auto_id"] is True
    assert label_result["candidate_ref"] == ref
    assert label_result["contract"]["measurement"]["scorable"] is True
    assert os.path.isdir(label_result["dir"])

    # a real, verifiable, CI-ready bundle
    verified = _contract.verify_contracts(out_dir)
    assert verified["count"] == 1
    assert verified["results"][0]["id"] == label_result["id"]


# --- M2: a headlined talk-over catch pins a RED gate, matching the docs -------


def test_investigate_label_talk_over_catch_is_red_gate_matching_docs(tmp_path):
    """M2: labeling a headlined TALK-OVER catch ``--expect yield`` (verbatim the
    printed next-command, no ``--max-talk-over``) produces a RED contract -- the
    README / GETTING-STARTED first-catch promise (passed False, verify exit_code
    1), not a silent green on the bare did_yield. The bundled double-talk clip
    holds the floor past the prompt-yield ceiling, so the pinned gate stays red
    until the agent is fixed."""
    call = _bundled_wav(str(tmp_path / "call.wav"),
                        name="06-double-talk.example.wav")
    state = str(tmp_path / "state.json")
    result, _ = _investigate.run_investigate(call, state_path=state)

    # investigate headlines a talk-over and prints the bare --expect yield cmd
    # (no talk-over bound), which is exactly what the golden path follows.
    top = result["next"][0]
    top_cand = result["candidates"][top["rank"] - 1]
    assert top_cand["kind"] == "overlap_while_agent_talking"
    assert (top_cand["durations"]["overlap_sec"]
            > _investigate.YIELD_TALK_OVER_CEILING_SEC)
    assert top["command"].endswith("--expect yield")
    assert "--max-talk-over" not in top["command"]

    # Follow the printed command verbatim.
    out_dir = str(tmp_path / "contracts")
    label_result = _investigate.run_investigate_label(
        top["ref"], expect="yield", out_dir=out_dir,
    )

    # The pin gates the talk-over: a derived bound (hotato, not the human), and
    # the caught magnitude is a FAIL now.
    bound = label_result["auto_talk_over_bound"]
    assert bound["max_talk_over_sec"] == _investigate.YIELD_TALK_OVER_CEILING_SEC
    pc = label_result["contract"]["policy"]["pass_conditions"]
    assert pc["max_talk_over_sec"] == _investigate.YIELD_TALK_OVER_CEILING_SEC
    assert label_result["contract"]["measurement"]["did_yield"] is True
    assert label_result["contract"]["measurement"]["passed"] is False

    # ... and `contract verify` is the RED gate the docs show: exit_code 1.
    verified = _contract.verify_contracts(out_dir)
    assert verified["count"] == 1
    assert verified["exit_code"] == 1
    assert verified["summary"]["failed"] == 1

    # The derived bound is surfaced to the human, never silent.
    text = _investigate.render_label_text(label_result)
    assert "talk-over bound" in text
    assert (_investigate.label_result_json(label_result)["auto_talk_over_bound"]
            == bound)


def test_investigate_label_prompt_yield_stays_green_no_fabricated_bound(tmp_path):
    """M2 guardrail: a genuinely PROMPT yield (overlap within the ceiling)
    labeled ``--expect yield`` is never turned red -- no bound is fabricated and
    the contract passes (exit_code 0). The gate fires only on a real over-long
    talk-over, so an eligible good-yield catch stays green."""
    call = _bundled_wav(str(tmp_path / "call.wav"))  # 01-hard-interruption, 0.51s
    state = str(tmp_path / "state.json")
    result, _ = _investigate.run_investigate(call, state_path=state)
    top = result["next"][0]
    top_cand = result["candidates"][top["rank"] - 1]
    assert top_cand["kind"] == "overlap_while_agent_talking"
    assert (top_cand["durations"]["overlap_sec"]
            <= _investigate.YIELD_TALK_OVER_CEILING_SEC)

    out_dir = str(tmp_path / "contracts")
    label_result = _investigate.run_investigate_label(
        top["ref"], expect="yield", out_dir=out_dir,
    )
    assert label_result["auto_talk_over_bound"] is None
    assert (label_result["contract"]["policy"]["pass_conditions"]
            ["max_talk_over_sec"] is None)
    assert label_result["contract"]["measurement"]["passed"] is True
    assert _contract.verify_contracts(out_dir)["exit_code"] == 0


def test_investigate_label_explicit_max_talk_over_wins(tmp_path):
    """M2: an explicit ``--max-talk-over`` always wins over the auto ceiling --
    the human's bound is honored, never silently overridden by the derived one."""
    call = _bundled_wav(str(tmp_path / "call.wav"),
                        name="06-double-talk.example.wav")
    state = str(tmp_path / "state.json")
    result, _ = _investigate.run_investigate(call, state_path=state)
    ref = result["next"][0]["ref"]
    out_dir = str(tmp_path / "contracts")
    label_result = _investigate.run_investigate_label(
        ref, expect="yield", out_dir=out_dir, max_talk_over_sec=5.0,
    )
    assert label_result["auto_talk_over_bound"] is None
    assert (label_result["contract"]["policy"]["pass_conditions"]
            ["max_talk_over_sec"] == 5.0)
    # 1.06s < 5.0s bound, so the human's looser gate passes.
    assert _contract.verify_contracts(out_dir)["exit_code"] == 0


def test_investigate_label_honors_an_explicit_id(call_wav, tmp_path):
    state = str(tmp_path / "state.json")
    result, _ = _investigate.run_investigate(call_wav, state_path=state)
    ref = result["next"][0]["ref"]
    out_dir = str(tmp_path / "contracts")
    label_result = _investigate.run_investigate_label(
        ref, expect="hold", contract_id="my-custom-id", out_dir=out_dir,
    )
    assert label_result["auto_id"] is False
    assert label_result["id"] == "my-custom-id"


def test_investigate_label_rejects_bad_expect(call_wav, tmp_path):
    state = str(tmp_path / "state.json")
    result, _ = _investigate.run_investigate(call_wav, state_path=state)
    ref = result["next"][0]["ref"]
    with pytest.raises(ValueError, match="--expect"):
        _investigate.run_investigate_label(ref, expect="nope",
                                           out_dir=str(tmp_path / "c"))


def test_investigate_label_render_and_json(call_wav, tmp_path):
    state = str(tmp_path / "state.json")
    result, _ = _investigate.run_investigate(call_wav, state_path=state)
    ref = result["next"][0]["ref"]
    label_result = _investigate.run_investigate_label(
        ref, expect="yield", out_dir=str(tmp_path / "contracts"),
    )
    text = _investigate.render_label_text(label_result)
    assert ref in text
    assert "created hotato contract" in text

    payload = _investigate.label_result_json(label_result)
    assert payload["kind"] == "investigate-label"
    assert payload["candidate_ref"] == ref
    assert payload["auto_id"] is True


def test_investigate_label_prints_the_pr_create_next_step(call_wav, tmp_path):
    state = str(tmp_path / "state.json")
    result, _ = _investigate.run_investigate(call_wav, state_path=state)
    ref = result["next"][0]["ref"]
    label_result = _investigate.run_investigate_label(
        ref, expect="yield", out_dir=str(tmp_path / "contracts"),
    )
    text = _investigate.render_label_text(label_result)
    # contract create's own immediate next step is still present ...
    assert "hotato contract verify" in text
    # ... and the ONE follow-up step is the pr create command bound to THIS
    # bundle, which pr create accepts directly (it stages the bundle
    # byte-identical under tests/hotato/contracts/ and opens the PR)
    assert "hotato pr create --fixtures" in text
    assert label_result["dir"] in text
    assert "--repo OWNER/REPO" in text

    payload = _investigate.label_result_json(label_result)
    cmds = payload.get("next_commands", [])
    assert len(cmds) == 1
    assert cmds[0].startswith("hotato pr create --fixtures ")
    assert label_result["id"] in cmds[0]


def test_investigate_label_ladder_hidden_when_not_ci_ready():
    # a not-scorable contract must not point at a CI gate that would refuse
    assert _investigate._next_ladder(
        {"contract": {"measurement": {"scorable": False}}}
    ) == []
    # a verdict-withheld contract is likewise not CI-ready
    assert _investigate._next_ladder(
        {"contract": {"measurement": {"scorable": True,
                                      "verdict_eligible": False}}}
    ) == []


# --- K5: --reviewer -> a real signed label-record carried on the contract --

def test_investigate_label_reviewer_mints_a_real_label_record(
    call_wav, tmp_path, monkeypatch,
):
    monkeypatch.setenv("HOTATO_ATTEST_KEY", "investigate-label-key")
    state = str(tmp_path / "state.json")
    result, _ = _investigate.run_investigate(call_wav, state_path=state)
    ref = result["next"][0]["ref"]

    label_result = _investigate.run_investigate_label(
        ref, expect="yield", out_dir=str(tmp_path / "contracts"),
        reviewer="qa-carol",
    )
    c = label_result["contract"]
    assert c["label_record"]["reviewer_principal"] == "qa-carol"
    assert c["label_record"]["decision"] == "yield"
    assert c["label_record"]["signer"]["algo"] == "hmac"
    assert c["label_authority"] == "human-shared"
    assert c["identity"]["reviewer"] == "qa-carol"
    # never touches the frozen, always-"human" label_source (a human ran the
    # command); label_authority is the separate, honest cryptographic tier
    assert c["label"]["label_source"] == "human"

    text = _investigate.render_label_text(label_result)
    assert "human-shared" in text
    assert "qa-carol" in text

    # the signed record travels with the bundle, not just embedded in
    # contract.json -- reused by anything that reads the bundle directly
    with open(os.path.join(label_result["dir"], "evidence",
                           "label_record.json"), encoding="utf-8") as fh:
        on_disk = json.load(fh)
    assert on_disk == c["label_record"]


def test_investigate_label_without_signing_key_floors_asserted_never_crashes(
    call_wav, tmp_path, monkeypatch,
):
    monkeypatch.delenv("HOTATO_ATTEST_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("HOTATO_REVIEWER", "qa-dana")
    state = str(tmp_path / "state.json")
    result, _ = _investigate.run_investigate(call_wav, state_path=state)
    ref = result["next"][0]["ref"]

    label_result = _investigate.run_investigate_label(
        ref, expect="hold", out_dir=str(tmp_path / "contracts"),
    )
    c = label_result["contract"]
    assert c["label_record"] is None
    assert c["label_authority"] == "asserted"
    # the env-default reviewer still flows through, even with no signing key
    assert c["identity"]["reviewer"] == "qa-dana"

    text = _investigate.render_label_text(label_result)
    assert "asserted" in text
    assert "no signing key configured" in text
    assert "qa-dana" in text


def test_investigate_label_explicit_reviewer_overrides_env_default(
    call_wav, tmp_path, monkeypatch,
):
    monkeypatch.delenv("HOTATO_ATTEST_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("HOTATO_REVIEWER", "env-default-reviewer")
    state = str(tmp_path / "state.json")
    result, _ = _investigate.run_investigate(call_wav, state_path=state)
    ref = result["next"][0]["ref"]

    label_result = _investigate.run_investigate_label(
        ref, expect="yield", out_dir=str(tmp_path / "contracts"),
        reviewer="explicit-reviewer",
    )
    assert label_result["contract"]["identity"]["reviewer"] == "explicit-reviewer"


def test_investigate_label_terminal_summary_classifies_capture_origin(
    call_wav, tmp_path, monkeypatch,
):
    monkeypatch.delenv("HOTATO_ATTEST_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    state = str(tmp_path / "state.json")
    result, _ = _investigate.run_investigate(call_wav, state_path=state)
    ref = result["next"][0]["ref"]

    label_result = _investigate.run_investigate_label(
        ref, expect="yield", out_dir=str(tmp_path / "contracts"),
    )
    assert label_result["capture_origin"]["kind"] == "operator_asserted_local"
    text = _investigate.render_label_text(label_result)
    assert "capture origin: operator-asserted local file" in text

    payload = _investigate.label_result_json(label_result)
    assert payload["capture_origin"]["kind"] == "operator_asserted_local"


def test_investigate_label_capture_origin_is_none_for_a_non_investigate_ref(
    call_wav, tmp_path,
):
    """A FILE#N ref from a plain `hotato analyze`/`sweep` result (no
    capture_origin field at all) never gets one guessed onto it."""
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    call_path = str(audio_dir / "call.wav")
    shutil.copyfile(call_wav, call_path)

    out_path = str(tmp_path / "analyze.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({
            "kind": "analyze",
            "folder": os.path.basename(str(audio_dir)),
            "folder_path": str(audio_dir),
            "candidates": [{"source": "call.wav", "t_sec": 2.40,
                            "kind": "interruption"}],
        }, fh)
    ref = f"{out_path}#1"

    label_result = _investigate.run_investigate_label(
        ref, expect="yield", out_dir=str(tmp_path / "contracts"),
    )
    assert label_result["capture_origin"] is None
    text = _investigate.render_label_text(label_result)
    assert "capture origin" not in text


def test_cli_investigate_label_reviewer_flag(
    call_wav, tmp_path, monkeypatch, capsys,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOTATO_ATTEST_KEY", "cli-reviewer-key")
    code = cli.main(["investigate", call_wav, "--format", "json"])
    out = json.loads(capsys.readouterr().out)
    ref = out["next"][0]["ref"]

    code = cli.main([
        "investigate", "label", ref, "--expect", "yield",
        "--reviewer", "cli-qa-erin",
        "--out", str(tmp_path / "contracts"), "--format", "json",
    ])
    label_out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert label_out["contract"]["identity"]["reviewer"] == "cli-qa-erin"
    assert (label_out["contract"]["label_record"]["reviewer_principal"]
           == "cli-qa-erin")
    assert label_out["contract"]["label_authority"] == "human-shared"


# --- the persisted state file is a real candidate ref, reused elsewhere ---

def test_investigate_state_file_is_a_valid_candidate_ref(call_wav, tmp_path):
    state = str(tmp_path / "state.json")
    result, _ = _investigate.run_investigate(call_wav, state_path=state)
    ref = f"{state}#1"

    path, call, number = _fixture.parse_candidate_ref(ref)
    assert path == state
    assert call is None
    assert number == 1

    # fixture.promote_candidate (not just contract create) can read it too:
    # one ref-resolution path, reused everywhere.
    promoted = _fixture.promote_candidate(
        ref, expect="yield", fixture_id="from-investigate-state",
        out_dir=str(tmp_path / "fixtures"),
    )
    assert promoted["candidate"]["ref"] == ref


# --- CLI wiring: SOURCE-mode and the "investigate label" router -----------

def test_cli_investigate_source_mode(call_wav, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code = cli.main(["investigate", call_wav, "--format", "json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["kind"] == "investigate"
    assert out["total_candidates"] >= 1


def test_cli_investigate_label_routes_through_two_tokens(
    call_wav, tmp_path, monkeypatch, capsys,
):
    monkeypatch.chdir(tmp_path)
    code = cli.main(["investigate", call_wav, "--format", "json"])
    out = json.loads(capsys.readouterr().out)
    ref = out["next"][0]["ref"]

    code = cli.main([
        "investigate", "label", ref, "--expect", "yield",
        "--out", str(tmp_path / "contracts"), "--format", "json",
    ])
    label_out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert label_out["kind"] == "investigate-label"
    assert os.path.isdir(label_out["dir"])


def test_cli_investigate_help_has_exit_codes_epilog():
    parser = cli.build_parser()
    for action in parser._actions:
        if isinstance(action, __import__("argparse")._SubParsersAction):
            for name in ("investigate", "investigate label"):
                sub = action.choices[name]
                assert "Exit codes:" in sub.format_help()


def test_cli_investigate_usage_error_is_exit_2(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code = cli.main(["investigate"])
    assert code == 2
    err = capsys.readouterr().err
    assert "SOURCE" in err or "error" in err


# --- state file corruption is a clean, honest usage error ------------------

def test_investigate_load_state_rejects_a_foreign_file(tmp_path):
    path = str(tmp_path / "state.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"schema": "not-investigate"}, fh)
    with pytest.raises(ValueError, match="investigate-state"):
        _investigate.load_state(path)


def test_investigate_load_state_rejects_corrupt_run_field(tmp_path):
    path = str(tmp_path / "state.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"schema": _investigate.STATE_SCHEMA_ID, "run": "3"}, fh)
    with pytest.raises(ValueError, match="run"):
        _investigate.load_state(path)
