"""``hotato fix trial`` (S4): compose apply's clone-only offline gate + a
manifest-pinned RECOMPUTE of both sides from audio + verify's battery-scale
rollup + the Evidence Kernel tier + contract verify + explain into ONE
before/after proof, fail-closed.

The proof gate no longer TRUSTS the stored ``verdict.passed`` in the before /
after envelopes: it re-derives every verdict from the on-disk audio under one
pinned trial manifest (:mod:`hotato.manifest` / :mod:`hotato.recompute`), so a
hand-edited verdict, an old call re-scored under a looser policy, the same
conversation re-scored, an incomplete fixture set, or unrelated caller audio can
never reach ``improved``. These tests therefore build fixtures from REAL stereo
WAVs (via :mod:`tests._trial_audio`) that GENUINELY score as claimed, run them
through ``core.run_suite`` to build multi-event envelopes with matching ids, and
assert the recompute-from-audio gate.

Covered:

* an improving before/after battery over real audio -> verdict "improved", exit
  0, evidence tier >= PAIRED (after the trust preflight), claim supported;
* an after-side verdict hand-edited to passed over the real FAILING audio ->
  refused (score_mismatch);
* after == before audio -> refused (same_audio);
* after drops a fixture -> refused (incomplete_fixture_set);
* after replays unrelated caller audio -> not improved (stimulus_mismatch);
* a hold/opposite-risk fixture regressing -> "regressed", exit 1;
* a --contracts regression and a --policy violation each independently force a
  fail even when the battery itself improved;
* the both-axes threshold-funnel patch REFUSES before any before/after evidence
  is read (exit 3, the same code hotato apply uses);
* fix trial never creates a clone and never touches the network;
* the apply receipt renders beside the verdict on every surface;
* no positive nested CLAIM survives under a non-improved parent;
* no em or en dashes in any rendered output.
"""

from __future__ import annotations

import json
import os

import pytest

from hotato import apply as _apply
from hotato import cli
from hotato import core as _core
from hotato import evidence as _evidence
from hotato import fix_trial as _fix_trial
from hotato import fixplan as _fixplan
from hotato import patch as _patch
from hotato.diagnose import OPPOSITE_RISK

import _trial_audio as ta

HARD = None
try:
    from importlib import resources
    HARD = str(resources.files("hotato").joinpath(
        "data", "audio", "01-hard-interruption.example.wav"))
except Exception:  # pragma: no cover - resource lookup always succeeds here
    pass


# --- patch builders (mirror test_apply_clone.py) ----------------------------

def _diagnosis(finding: str) -> dict:
    coverage = {}
    key = OPPOSITE_RISK.get(finding, {}).get("coverage_key")
    if key:
        coverage[key] = True
    return {
        "battery": {"finding": None, "failed": 1, "events": 2,
                    "opposite_risk_coverage": coverage},
        "diagnoses": [
            {"finding": finding, "config_only_safe": True, "event_id": "e1",
             "scenario_id": "s1", "notes": "measured note.", "evidence": {}},
        ],
    }


def _config_plan(stack="vapi", current=3, source="interrupt_min_words"):
    inspected = {"stack": stack,
                 "turn_taking": {source: current, "raw": {source: current}}}
    return _fixplan.build_plan(
        diagnosis=_diagnosis("missed_real_interruption"),
        inspected=inspected, stack=stack,
        target_info={"assistant_id": "asst_9"},
    )


def _funnel_plan():
    diag = {"battery": {"finding": "threshold_funnel", "failed": 2, "events": 3,
                        "opposite_risk_coverage": {}}, "diagnoses": []}
    return _fixplan.build_plan(diagnosis=diag, inspected=None, stack="vapi")


def _write_patch(tmp_path, plan, *, plan_name="fixplan.json",
                 patch_name="patch.json"):
    (tmp_path / plan_name).write_text(json.dumps(plan), encoding="utf-8")
    p = _patch.build_patch(plan, source=plan_name)
    patch_path = tmp_path / patch_name
    patch_path.write_text(json.dumps(p), encoding="utf-8")
    return patch_path


@pytest.fixture()
def config_patch(tmp_path):
    return _write_patch(tmp_path, _config_plan())


@pytest.fixture()
def funnel_patch(tmp_path):
    return _write_patch(tmp_path, _funnel_plan())


# --- real-audio trial builder -----------------------------------------------
#
# A trial is a scenarios/ dir (labels), a before/ dir (the original failing
# capture), and an after/ dir (the re-captured, passing evidence). Each side is
# scored with core.run_suite, which writes a real multi-event envelope with
# audio_provenance whose files sit next to the run.json (exactly where the
# recompute resolves them at trial time). The AGENT channel differs before vs
# after (a genuine fresh recapture); the CALLER channel is the SAME scripted
# stimulus per fixture (a fix changes the agent, not the caller). Onset is late
# and the total long so the agent dominates activity -> no false channel-swap
# flag -> the trust preflight reads "clean/confirmed" -> tier PAIRED.

ONSET = 5.0
TOTAL = 8.0
SUF = ".example.wav"

# (fixture id, expect_yield). Three yield targets (>= the default --min-n 3)
# plus a hold, so the before battery carries BOTH a yield and a hold fixture
# (apply's opposite-risk gate) and there are enough previously-failing fixtures.
FIXTURES = [("f1", True), ("f2", True), ("f3", True), ("h1", False)]


def _yield_before(p):
    """A missed interruption: the agent talks straight through -> fails yield."""
    ta.talkover_call(p, onset=ONSET, total=TOTAL)


def _yield_after(p):
    """The fix: the agent yields promptly to the same interruption -> passes."""
    ta.yielding_call(p, onset=ONSET, total=TOTAL)


def _hold_before(p):
    """The agent wrongly drops the floor for a backchannel -> fails hold."""
    ta.yielded_to_backchannel_call(p, onset=ONSET, total=TOTAL)


def _hold_after(p):
    """The agent keeps the floor through the backchannel -> passes hold."""
    ta.holding_call(p, onset=ONSET, total=TOTAL)


_DEFAULT_BEFORE = {"f1": _yield_before, "f2": _yield_before, "f3": _yield_before,
                   "h1": _hold_before}
_DEFAULT_AFTER = {"f1": _yield_after, "f2": _yield_after, "f3": _yield_after,
                  "h1": _hold_after}


def _write_scenarios(scen_dir):
    for sid, ey in FIXTURES:
        (scen_dir / f"{sid}.json").write_text(
            json.dumps({"id": sid, "title": sid, "caller_onset_sec": ONSET,
                        "expected": {"yield": ey}}), encoding="utf-8")


def _write_audio(audio_dir, writers):
    for sid, _ in FIXTURES:
        writer = writers.get(sid)
        if writer is not None:
            writer(str(audio_dir / f"{sid}{SUF}"))


def _score_side(scen_dir, audio_dir):
    env = _core.run_suite(scenarios_dir=str(scen_dir), audio_dir=str(audio_dir),
                          suffix=SUF)
    (audio_dir / "run.json").write_text(json.dumps(env), encoding="utf-8")
    return env


def build_trial(tmp_path, *, before_overrides=None, after_overrides=None):
    """Write scenarios + before-audio (failing) + after-audio (passing, SAME
    caller stimulus) + run.json envelopes for both sides. Returns
    ``(before_dir, after_dir, battery_dir)`` (battery defaults to before)."""
    scen = tmp_path / "scenarios"
    before = tmp_path / "before"
    after = tmp_path / "after"
    for d in (scen, before, after):
        d.mkdir(exist_ok=True)
    _write_scenarios(scen)
    before_writers = dict(_DEFAULT_BEFORE, **(before_overrides or {}))
    after_writers = dict(_DEFAULT_AFTER, **(after_overrides or {}))
    _write_audio(before, before_writers)
    _write_audio(after, after_writers)
    _score_side(scen, before)
    _score_side(scen, after)
    return str(before), str(after), str(before)


def _edit_after(after_dir, mutate):
    """Load after/run.json, apply ``mutate(env)`` in place, rewrite it."""
    p = os.path.join(after_dir, "run.json")
    with open(p, encoding="utf-8") as fh:
        env = json.load(fh)
    mutate(env)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(env, fh)


def _run(patch_path, before, after, *extra):
    return cli.main([
        "fix", "trial", str(patch_path), "--name", "staging-x",
        "--before", before, "--after", after, *extra,
    ])


# --- IMPROVED: a legit real before(fail)/after(pass) proof ------------------

def test_legit_improvement_reaches_improved_at_paired_tier(
        tmp_path, config_patch, capsys):
    before, after, _battery = build_trial(tmp_path)
    rc = _run(config_patch, before, after, "--format", "json")
    assert rc == _fix_trial.EXIT_IMPROVED == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "improved"
    assert payload["exit_code"] == 0
    assert payload["refusal"] is None
    # every previously-failing fixture now passes on the RECOMPUTED verdicts.
    assert payload["verify"]["regression_axis"]["now_pass"] == 4
    assert payload["verify"]["regression_axis"]["used_to_fail"] == 4
    assert payload["verify"]["claim"]["supported"] is True
    # the trust preflight lifted the recompute-only MEASURED tier to PAIRED.
    assert payload["evidence"]["tier"] >= _evidence.TIER_PAIRED
    assert payload["evidence"]["allows_positive_paired"] is True
    assert payload["evidence"]["vector"]["input_health"] == "clean"
    assert payload["evidence"]["vector"]["channel_mapping"] == "confirmed"
    # the recompute raised no tampering flag.
    assert payload["recompute"]["flags"] == {
        "score_mismatch": False, "same_pcm": False,
        "stimulus_mismatch": False, "unrecomputable": False}
    assert payload["apply"]["clone"]["name"] == "staging-x"


def test_run_trial_improved_reuses_verify_and_apply_exactly(
        tmp_path, config_patch):
    before, after, _battery = build_trial(tmp_path)
    with open(config_patch, encoding="utf-8") as fh:
        patch = json.load(fh)
    plan = _apply.load_referenced_plan(patch, str(config_patch))
    t = _fix_trial.run_trial(
        patch, name="staging-x", before=before, after=after,
        patch_source=str(config_patch), plan=plan,
    )
    assert t["verdict"] == _fix_trial.VERDICT_IMPROVED
    assert t["exit_code"] == 0
    assert t["apply"]["clone"]["name"] == "staging-x"
    assert t["clone_only"] is True
    assert t["production_apply_supported"] is False
    # the default --battery reuses --before (it already carries the labels).
    assert t["battery"] == before
    assert t["evidence"]["tier"] >= _evidence.TIER_PAIRED
    assert t["recompute"]["manifest_hash"]


def test_min_n_below_target_count_still_improves(tmp_path, config_patch, capsys):
    # 4 previously-failing fixtures clears --min-n 3 comfortably; lowering the
    # bar to 2 keeps it improved but the lowered floor stays visible.
    before, after, _battery = build_trial(tmp_path)
    rc = _run(config_patch, before, after, "--min-n", "2", "--format", "json")
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "improved"
    assert payload["min_n"] == 2


# --- REFUSED: recompute hard-gates (the crown jewels) -----------------------

def test_hand_edited_verdict_over_failing_audio_refuses_score_mismatch(
        tmp_path, config_patch, capsys):
    # f1's AFTER audio is the real FAILING recording (talkover), but the stored
    # verdict is hand-edited to passed. hotato re-scores from audio (fail) and
    # refuses: the envelope was not produced by scoring this audio.
    before, after, _battery = build_trial(
        tmp_path, after_overrides={"f1": _yield_before})  # failing audio for f1

    def flip(env):
        for ev in env["events"]:
            if ev["event_id"] == "f1":
                ev["verdict"]["passed"] = True  # the tamper

    _edit_after(after, flip)
    rc = _run(config_patch, before, after, "--format", "json")
    assert rc == _fix_trial.EXIT_REFUSED == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "refused"
    assert payload["refusal_kind"] == "score_mismatch"
    assert payload["recompute"]["flags"]["score_mismatch"] is True
    assert "recomputed" in payload["refusal"]["reason"]
    # real before/after evidence WAS read: verify still ran and renders.
    assert payload["verify"] is not None


def test_after_equals_before_audio_refuses_same_audio(
        tmp_path, config_patch, capsys):
    # f1's AFTER audio decodes to the SAME PCM as its BEFORE audio (the same
    # conversation re-scored). The stored verdict is honest, so the ONLY problem
    # is that it is not a fresh recording -> refused (same_audio).
    before, after, _battery = build_trial(
        tmp_path, after_overrides={"f1": _yield_before})  # == before f1 audio
    rc = _run(config_patch, before, after, "--format", "json")
    assert rc == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "refused"
    assert payload["refusal_kind"] == "same_audio"
    assert payload["recompute"]["flags"]["same_pcm"] is True
    assert payload["refusal"]["headline"] == \
        "No fix will be certified from re-scored audio"
    assert "same" in payload["refusal"]["reason"].lower()


def test_after_drops_a_fixture_refuses_incomplete_fixture_set(
        tmp_path, config_patch, capsys):
    # The after set silently drops f1: a cherry-picked comparison over a subset
    # of the pinned fixture universe -> refused (incomplete_fixture_set).
    before, after, _battery = build_trial(tmp_path)

    def drop_f1(env):
        env["events"] = [e for e in env["events"] if e["event_id"] != "f1"]

    _edit_after(after, drop_f1)
    rc = _run(config_patch, before, after, "--format", "json")
    assert rc == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "refused"
    assert payload["refusal_kind"] == "incomplete_fixture_set"
    assert payload["recompute"]["coverage"]["after"]["complete"] is False
    assert "f1::f1" in payload["recompute"]["coverage"]["after"]["missing"]


def test_unrelated_caller_audio_is_never_improved(tmp_path, config_patch, capsys):
    # f1's AFTER caller channel is a DIFFERENT scripted stimulus than the before
    # side (a different caller window), so the pair is not the same scenario
    # recaptured. stimulus_mismatch (or, if it also drops coverage, incomplete):
    # either way it must never read as improved.
    def unrelated_caller(p):
        ta.write_stereo(p, caller_windows=[(ONSET, TOTAL - 0.5)],
                        agent_windows=[(0.2, ONSET + 0.3)], total_sec=TOTAL)

    before, after, _battery = build_trial(
        tmp_path, after_overrides={"f1": unrelated_caller})
    rc = _run(config_patch, before, after, "--format", "json")
    assert rc != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] != "improved"
    assert payload["refusal_kind"] in ("stimulus_mismatch",
                                       "incomplete_fixture_set")
    assert payload["recompute"]["flags"]["stimulus_mismatch"] is True


def test_missing_audio_is_inconclusive_not_improved(
        tmp_path, config_patch, capsys):
    # The envelopes still assert real verdicts, but the audio is gone: hotato
    # can recompute nothing, so the evidence tier collapses below PAIRED and the
    # verdict downgrades to inconclusive -- an unrecomputable claim is not a fix.
    before, after, _battery = build_trial(tmp_path)
    for d in (before, after):
        for name in os.listdir(d):
            if name.endswith(SUF):
                os.unlink(os.path.join(d, name))
    rc = _run(config_patch, before, after, "--format", "json")
    assert rc == _fix_trial.EXIT_FAIL == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "inconclusive"
    assert payload["refusal"] is None
    assert payload["recompute"]["flags"]["unrecomputable"] is True
    assert payload["evidence"]["tier"] < _evidence.TIER_PAIRED


# --- REGRESSED: a hold-axis regression forces a fail ------------------------

def test_hold_axis_regression_forces_fail(tmp_path, config_patch, capsys):
    # The three yield targets are fixed, but the hold h1 flips from passing (the
    # agent held the floor) to failing (the agent now yields to the backchannel):
    # a naive bandaid that trades talk-over for a false yield. Fail-closed.
    before, after, _battery = build_trial(
        tmp_path,
        before_overrides={"h1": _hold_after},   # h1 PASSES before (agent holds)
        after_overrides={"h1": _hold_before},   # h1 FAILS after (agent yields)
    )
    rc = _run(config_patch, before, after, "--format", "json")
    assert rc == _fix_trial.EXIT_FAIL == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "regressed"
    assert "h1" in payload["verify"]["regressions"]
    assert "REGRESSED" in payload["conclusion"]
    assert "fail-closed" in payload["conclusion"]
    # a regression is never a fix: refusal stays None but exit is fail-closed.
    assert payload["refusal"] is None


def test_policy_violation_forces_fail(tmp_path, config_patch, capsys):
    before, after, _battery = build_trial(tmp_path)
    policy_path = tmp_path / "hotato.verify.yaml"
    policy_path.write_text(
        "guardrails:\n  max_new_false_yields: 0\n  require_hold_fixture: true\n"
        "target:\n  improve:\n    talk_over_sec_p95: -100\n",  # impossible target
        encoding="utf-8",
    )
    rc = _run(config_patch, before, after, "--policy", str(policy_path),
              "--format", "json")
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "regressed"
    assert payload["verify"]["policy"]["passed"] is False


@pytest.mark.skipif(HARD is None, reason="bundled example audio unavailable")
def test_contract_regression_forces_fail_even_when_battery_improved(
        tmp_path, config_patch, capsys):
    contracts_dir = tmp_path / "contracts"
    # HARD actually yields; labelling it "hold" makes this contract fail its own
    # policy immediately, independent of anything the trial changed.
    rc = cli.main([
        "contract", "create", "--stereo", HARD, "--id", "neighbour-1",
        "--onset", "2.40", "--expect", "hold", "--out", str(contracts_dir),
    ])
    assert rc in (0, 1)
    capsys.readouterr()  # discard the contract-create output

    before, after, _battery = build_trial(tmp_path)
    rc = _run(config_patch, before, after, "--contracts", str(contracts_dir),
              "--format", "json")
    assert rc == _fix_trial.EXIT_FAIL == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "regressed"
    assert payload["contract_verify"]["summary"]["failed"] >= 1
    assert "contract" in payload["conclusion"].lower()


# --- REFUSED: the both-axes threshold funnel, before any evidence is read ----

def test_threshold_funnel_refuses_before_reading_before_after(
        funnel_patch, tmp_path, capsys, monkeypatch):
    # The apply-gate refusal fires before verify / recompute / explain ever run:
    # even nonexistent before/after paths are never opened.
    from hotato import verify as _verify
    from hotato import recompute as _recompute

    def boom(*a, **k):
        raise AssertionError("fix trial read before/after on the refused path")

    monkeypatch.setattr(_verify, "verify_sides", boom)
    monkeypatch.setattr(_recompute, "recompute_trial", boom)
    rc = cli.main([
        "fix", "trial", str(funnel_patch), "--name", "staging-x",
        "--before", str(tmp_path / "nope-before.json"),
        "--after", str(tmp_path / "nope-after.json"),
        "--format", "json",
    ])
    assert rc == _fix_trial.EXIT_REFUSED == _apply.REFUSAL_EXIT_CODE == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "refused"
    assert payload["verify"] is None
    assert payload["contract_verify"] is None
    assert payload["attribution"] is None
    assert payload["evidence"] is None
    assert "No config patch will be applied" in payload["refusal"]["headline"]
    assert payload["refusal"]["reason"] == _apply.REFUSAL_REASON


def test_threshold_funnel_refuses_in_text_surface(funnel_patch, capsys):
    rc = cli.main([
        "fix", "trial", str(funnel_patch), "--before", "nope-b.json",
        "--after", "nope-a.json",
    ])
    assert rc == 3
    out = capsys.readouterr().out
    assert "No config patch will be applied" in out
    assert "Recommended: enable or add engagement-control" in out


# --- clone isolation: never creates a clone, never touches the network ------

def test_never_creates_a_clone_or_touches_the_network(
        tmp_path, config_patch, capsys, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("fix trial hit the network / created a clone")

    monkeypatch.setattr(_apply, "_http_json", boom)
    monkeypatch.setattr(_apply, "create_clone", boom)

    before, after, _battery = build_trial(tmp_path)
    rc = _run(config_patch, before, after, "--format", "json")
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["clone_only"] is True
    assert payload["production_apply_supported"] is False


# --- attribution: folds in hotato explain on the BEFORE evidence ------------

def test_attribution_section_is_populated_from_explain(
        tmp_path, config_patch, capsys):
    before, after, _battery = build_trial(tmp_path)
    rc = _run(config_patch, before, after, "--format", "json")
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    a = payload["attribution"]
    assert a["schema"] == "hotato.explain.v1"
    # --before is a directory of run envelopes; explain reads its run.json.
    assert any(s.endswith("run.json") for s in a["sources"])
    assert a["explanations"]
    assert a["explanations"][0]["input_kind"] == "run_envelope"


# --- usage errors: the same gates apply already enforces ---------------------

def test_missing_name_is_a_usage_error(tmp_path, config_patch, capsys):
    before, after, _battery = build_trial(tmp_path)
    rc = cli.main([
        "fix", "trial", str(config_patch), "--before", before, "--after", after,
    ])
    assert rc == 2
    assert "--name" in capsys.readouterr().err


def test_bad_contracts_dir_is_a_usage_error(tmp_path, config_patch, capsys):
    before, after, _battery = build_trial(tmp_path)
    empty = tmp_path / "no-contracts-here"
    empty.mkdir()
    rc = _run(config_patch, before, after, "--contracts", str(empty))
    assert rc == 2
    assert "no hotato contracts" in capsys.readouterr().err


def test_missing_patch_file_is_a_usage_error(tmp_path, capsys):
    before, after, _battery = build_trial(tmp_path)
    rc = cli.main([
        "fix", "trial", str(tmp_path / "nope.json"), "--name", "x",
        "--before", before, "--after", after,
    ])
    assert rc == 2


# --- --out / --html ----------------------------------------------------------

def test_out_writes_full_json_proof(tmp_path, config_patch, capsys):
    before, after, _battery = build_trial(tmp_path)
    out_path = tmp_path / "fix-trial.json"
    rc = _run(config_patch, before, after, "--out", str(out_path))
    assert rc == 0
    written = json.loads(out_path.read_text(encoding="utf-8"))
    assert written["verdict"] == "improved"
    assert written["schema"] == "hotato.fix_trial.v1"


def test_html_writes_a_self_contained_report(tmp_path, config_patch, capsys):
    before, after, _battery = build_trial(tmp_path)
    html_path = tmp_path / "fix-trial.html"
    rc = _run(config_patch, before, after, "--html", str(html_path))
    assert rc == 0
    html = html_path.read_text(encoding="utf-8")
    assert "<!doctype html>" in html
    assert "hotato fix trial" in html
    assert "IMPROVED" in html
    assert "coincidence" in html
    assert "Evidence: what this" in html


def test_refused_html_report_renders(tmp_path, funnel_patch, capsys):
    html_path = tmp_path / "refused.html"
    rc = cli.main([
        "fix", "trial", str(funnel_patch), "--name", "x",
        "--before", "nope-b.json", "--after", "nope-a.json",
        "--html", str(html_path),
    ])
    assert rc == 3
    html = html_path.read_text(encoding="utf-8")
    assert "No config patch will be applied" in html


def test_same_audio_refusal_html_still_shows_the_real_evidence(
        tmp_path, config_patch, capsys):
    before, after, _battery = build_trial(
        tmp_path, after_overrides={"f1": _yield_before})
    html_path = tmp_path / "refused-provenance.html"
    rc = _run(config_patch, before, after, "--html", str(html_path))
    assert rc == 3
    html = html_path.read_text(encoding="utf-8")
    assert "No fix will be certified from re-scored audio" in html
    # Unlike the apply-gate refusal report, verify's own proof still renders.
    assert "Verify: battery-scale proof" in html


# --- min-n echoed in every surface ------------------------------------------

def test_min_n_is_echoed_in_every_surface(tmp_path, config_patch, capsys):
    before, after, _battery = build_trial(tmp_path)
    rc = _run(config_patch, before, after, "--min-n", "2")
    assert rc == 0
    text_out = capsys.readouterr().out
    assert "min-n=2" in text_out
    assert "min-n 2" in text_out  # in the conclusion line

    rc = _run(config_patch, before, after, "--min-n", "2", "--format", "json")
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["min_n"] == 2

    html_path = tmp_path / "fix-trial.html"
    rc = _run(config_patch, before, after, "--min-n", "2", "--html",
              str(html_path))
    assert rc == 0
    html = html_path.read_text(encoding="utf-8")
    assert "min-n" in html and ">2<" in html


# --- report-facing evidence + provenance caution ----------------------------

def test_evidence_line_and_caution_appear_in_text(tmp_path, config_patch, capsys):
    before, after, _battery = build_trial(tmp_path)
    rc = _run(config_patch, before, after)
    assert rc == 0
    text_out = capsys.readouterr().out
    assert "Evidence: PAIRED EVIDENCE IMPROVED (tier 3)" in text_out
    assert _fix_trial._PROVENANCE_CAUTION in text_out


def test_provenance_caution_appears_in_html(tmp_path, config_patch, capsys):
    before, after, _battery = build_trial(tmp_path)
    html_path = tmp_path / "fix-trial.html"
    rc = _run(config_patch, before, after, "--html", str(html_path))
    assert rc == 0
    html = html_path.read_text(encoding="utf-8")
    assert "Provenance caution" in html
    assert "at the revision it was captured from" in html


def test_evidence_and_caution_render_even_on_refusal(
        tmp_path, config_patch, capsys):
    before, after, _battery = build_trial(
        tmp_path, after_overrides={"f1": _yield_before})  # same_audio refusal
    rc = _run(config_patch, before, after)
    assert rc == 3
    text_out = capsys.readouterr().out
    assert _fix_trial._PROVENANCE_CAUTION in text_out
    assert "Evidence:" in text_out


# --- no em or en dashes anywhere ---------------------------------------------

def test_no_em_or_en_dashes_in_any_rendered_output(tmp_path, config_patch, capsys):
    before, after, _battery = build_trial(tmp_path)
    rc = _run(config_patch, before, after)
    assert rc == 0
    text_out = capsys.readouterr().out
    assert "—" not in text_out
    assert "–" not in text_out

    html_path = tmp_path / "fix-trial.html"
    rc = _run(config_patch, before, after, "--html", str(html_path))
    assert rc == 0
    capsys.readouterr()
    html = html_path.read_text(encoding="utf-8")
    assert "—" not in html
    assert "–" not in html


# --- apply receipt: rendered beside the verdict, never buried ---------------

def test_apply_receipt_json_fields_present_on_improved(
        tmp_path, config_patch, capsys):
    before, after, _battery = build_trial(tmp_path)
    rc = _run(config_patch, before, after, "--format", "json")
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["apply_dry_run"] is True
    assert payload["apply_created"] is False
    assert payload["apply_applies_change"] is False
    assert "DRY-RUN patch proposal" in payload["apply_receipt_note"]
    assert "does not attest that the change was applied" in \
        payload["apply_receipt_note"]


def test_apply_receipt_renders_in_text_next_to_the_verdict(
        tmp_path, config_patch, capsys):
    before, after, _battery = build_trial(tmp_path)
    rc = _run(config_patch, before, after)
    assert rc == 0
    text_out = capsys.readouterr().out
    verdict_idx = text_out.find("[IMPROVED]")
    receipt_idx = text_out.find(
        "dry_run=True created=False applies_change=False")
    note_idx = text_out.find(
        "does not attest that the change was applied to a clone or an agent")
    verify_idx = text_out.find("-- verify:")
    assert -1 not in (verdict_idx, receipt_idx, note_idx, verify_idx)
    assert verdict_idx < receipt_idx < note_idx < verify_idx


def test_apply_receipt_renders_in_html_header_block(
        tmp_path, config_patch, capsys):
    before, after, _battery = build_trial(tmp_path)
    html_path = tmp_path / "fix-trial.html"
    rc = _run(config_patch, before, after, "--html", str(html_path))
    assert rc == 0
    html = html_path.read_text(encoding="utf-8")
    header = html[html.find("<header"):html.find("</header>") + len("</header>")]
    assert "apply dry_run" in header
    assert "apply created" in header
    assert "apply applies_change" in header
    assert ("does not attest that the change was applied to a clone or an "
            "agent") in header


def test_apply_receipt_present_even_on_apply_gate_refusal(
        tmp_path, funnel_patch, capsys):
    rc = cli.main([
        "fix", "trial", str(funnel_patch), "--name", "staging-x",
        "--before", str(tmp_path / "nope-before.json"),
        "--after", str(tmp_path / "nope-after.json"),
        "--format", "json",
    ])
    assert rc == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "refused"
    assert payload["apply_dry_run"] is True
    assert payload["apply_created"] is False
    assert payload["apply_applies_change"] is False


def test_apply_receipt_present_on_recompute_refusal(
        tmp_path, config_patch, capsys):
    before, after, _battery = build_trial(
        tmp_path, after_overrides={"f1": _yield_before})  # same_audio refusal
    rc = _run(config_patch, before, after)
    assert rc == 3
    text_out = capsys.readouterr().out
    assert "dry_run=True created=False applies_change=False" in text_out
    assert "does not attest that the change was applied" in text_out


# --- no positive CLAIM under a non-improved parent (rank 4) ------------------

def test_nested_claim_forced_unsupported_when_parent_not_improved(
        tmp_path, config_patch, capsys):
    # The three targets improve, but the hold regresses, so the PARENT verdict
    # is regressed. The embedded verify claim must not read positive to any
    # consumer: claim.supported is forced False in the DATA, and no bare
    # positive "CLAIM:" line leaks into the text report.
    before, after, _battery = build_trial(
        tmp_path,
        before_overrides={"h1": _hold_after},
        after_overrides={"h1": _hold_before},
    )
    rc = _run(config_patch, before, after, "--format", "json")
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "regressed"
    assert payload["verify"]["claim"]["supported"] is False
    assert payload["verify"]["claim"]["superseded_by"] == "regressed"

    rc = _run(config_patch, before, after)
    assert rc == 1
    text_out = capsys.readouterr().out
    assert "\n  CLAIM: " not in text_out


def test_claim_supported_on_a_genuinely_improved_verdict(
        tmp_path, config_patch, capsys):
    # Regression guard: the happy path keeps the positive claim, so a green
    # report is not saddled with a spurious "not supported" caveat.
    before, after, _battery = build_trial(tmp_path)
    rc = _run(config_patch, before, after)
    assert rc == 0
    text_out = capsys.readouterr().out
    assert "[IMPROVED]" in text_out
    assert "  CLAIM: " in text_out
    assert "SUPERSEDED" not in text_out
