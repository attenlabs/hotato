"""``hotato fix trial`` (S4): compose apply's clone-only offline gate + verify's
battery-scale rollup + contract verify + explain into ONE before/after proof,
fail-closed.

Covers the load-bearing behaviours from the spec:

* an improving before/after battery -> verdict "improved", exit 0;
* a hold/opposite-risk fixture flipping pass -> fail -> verdict "regressed",
  exit 1 (fail-closed, never a soft pass);
* too few previously-failing fixtures (below --min-n) -> verdict
  "inconclusive", exit 1 (fail-closed, NOT a pass);
* a --contracts regression, and a --policy violation, each independently
  force "regressed" even when the battery itself improved;
* the both-axes threshold-funnel patch REFUSES before any before/after
  evidence is read (exit 3, the same code hotato apply uses);
* fix trial never creates a clone and never touches the network (the same
  clone-only, production-unmutatable guarantee hotato apply's dry run gives);
* the attribution section folds in hotato explain's output on the BEFORE
  evidence;
* --out / --html write the JSON proof / the self-contained HTML report;
* no em or en dashes in any rendered output.
"""

from __future__ import annotations

import json
import math
import os
import struct
import wave

import pytest

from hotato import apply as _apply
from hotato import cli
from hotato import core as _core
from hotato import fix_trial as _fix_trial
from hotato import fixplan as _fixplan
from hotato import patch as _patch
from hotato.diagnose import OPPOSITE_RISK

HARD = None
BACKCHANNEL = None
try:
    from importlib import resources
    HARD = str(resources.files("hotato").joinpath(
        "data", "audio", "01-hard-interruption.example.wav"))
    BACKCHANNEL = str(resources.files("hotato").joinpath(
        "data", "audio", "02-backchannel-mhm.example.wav"))
except Exception:  # pragma: no cover - resource lookup always succeeds here
    pass


# --- shared fixture builders (mirrors test_apply_clone.py / test_verify.py) --

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


def _config_plan(stack="vapi", current=3, source="interrupt_min_words",
                 target_info=None):
    inspected = {"stack": stack,
                 "turn_taking": {source: current, "raw": {source: current}}}
    return _fixplan.build_plan(
        diagnosis=_diagnosis("missed_real_interruption"),
        inspected=inspected, stack=stack,
        target_info=target_info if target_info is not None
        else {"assistant_id": "asst_9"},
    )


def _funnel_plan():
    diag = {"battery": {"finding": "threshold_funnel", "failed": 2, "events": 3,
                        "opposite_risk_coverage": {}}, "diagnoses": []}
    return _fixplan.build_plan(diagnosis=diag, inspected=None, stack="vapi")


def _write_patch(tmp_path, plan, *, plan_name="fixplan.json",
                 patch_name="patch.json"):
    plan_path = tmp_path / plan_name
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
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


# --- real-WAV provenance helpers --------------------------------------------
#
# The provenance guard now VALIDATES and RECOMPUTES audio identity, so the test
# envelopes carry provenance built from real WAV files on disk (via the same
# core._audio_provenance the capture path uses). A fixture's WAV lives next to
# its envelope (tmp_path) under a unique basename, exactly where fix trial
# resolves it at trial time.

def _make_wav(path, *, seed=0, n=1600, rate=16000, extra_bytes=b""):
    """Write a mono 16-bit WAV whose samples depend on ``seed`` (so different
    seeds decode to different PCM). ``extra_bytes`` is appended AFTER the file
    is closed -- past the declared data chunk -- to model a trailing-byte edit
    that changes the raw file while leaving the decoded PCM identical."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        buf = bytearray()
        for i in range(n):
            val = int(2000 * math.sin(2 * math.pi * (110 + seed) * i / rate))
            buf += struct.pack("<h", max(-32768, min(32767, val)))
        w.writeframes(bytes(buf))
    if extra_bytes:
        with open(path, "ab") as fh:
            fh.write(extra_bytes)
    return str(path)


def _prov(tmp_path, basename, *, seed, n=1600, rate=16000, extra_bytes=b""):
    """A real, recomputable audio_provenance block for one side, backed by a
    WAV written to ``tmp_path/{basename}.wav`` (resolvable next to the
    envelope). Distinct seeds => distinct decoded PCM => a genuine recapture."""
    p = tmp_path / f"{basename}.wav"
    _make_wav(p, seed=seed, n=n, rate=rate, extra_bytes=extra_bytes)
    return _core._audio_provenance(("stereo", str(p)))


def _ev(eid, expected_yield, passed, tov=0.0, tty=None, scorable=True,
        provenance=None):
    v = {
        "passed": passed,
        "did_yield": expected_yield if passed else (not expected_yield),
        "talk_over_sec": tov,
        "seconds_to_yield": tty,
        "reasons": [] if passed else ["out of bound"],
    }
    e = {"event_id": eid, "scenario_id": eid,
         "expected_yield": expected_yield, "verdict": v}
    if not scorable:
        e["scorable"] = False
    # provenance=None mirrors an envelope with no audio_provenance field at all
    # (an older hotato, or a hand-built one); pass a dict (usually from _prov)
    # to simulate a real captured event with recomputable identity.
    if provenance is not None:
        e["audio_provenance"] = provenance
    return e


def _env(events):
    passed = sum(1 for e in events if e["verdict"]["passed"])
    return {
        "tool": "hotato", "mode": "suite", "stack": "vapi", "offline": True,
        "events": events,
        "summary": {"events": len(events), "passed": passed,
                    "failed": len(events) - passed},
        "exit_code": 0,
    }


def _write_env(tmp_path, name, events):
    p = tmp_path / name
    p.write_text(json.dumps(_env(events)), encoding="utf-8")
    return str(p)


def _improving_sides(tmp_path):
    """Three previously-failing yield fixtures fixed + a hold guard still
    passing: a clean, min-n-supported improvement with no regression. Each
    guarded fixture (the 3 targets AND the hold) carries DISTINCT before/after
    audio_provenance built from real WAVs on disk -- a genuine fresh recapture,
    recomputable at trial time -- so the provenance guard lets the 'improved'
    verdict stand. Seeds are unique per (fixture, side) so decoded PCM differs."""
    before = _write_env(tmp_path, "before.json", [
        _ev("f1", True, False, 1.2, provenance=_prov(tmp_path, "f1-before", seed=1)),
        _ev("f2", True, False, 0.9, 2.1, provenance=_prov(tmp_path, "f2-before", seed=2)),
        _ev("f3", True, False, 1.5, provenance=_prov(tmp_path, "f3-before", seed=3)),
        _ev("h1", False, True, 0.0, provenance=_prov(tmp_path, "h1-before", seed=4)),
    ])
    after = _write_env(tmp_path, "after.json", [
        _ev("f1", True, True, 0.3, 0.4, provenance=_prov(tmp_path, "f1-after", seed=11)),
        _ev("f2", True, True, 0.2, 0.5, provenance=_prov(tmp_path, "f2-after", seed=12)),
        _ev("f3", True, True, 0.4, 0.6, provenance=_prov(tmp_path, "f3-after", seed=13)),
        _ev("h1", False, True, 0.0, provenance=_prov(tmp_path, "h1-after", seed=14)),
    ])
    return before, after


def _run(tmp_path, patch_path, before, after, *extra):
    return cli.main([
        "fix", "trial", str(patch_path), "--name", "staging-x",
        "--before", before, "--after", after, *extra,
    ])


# --- IMPROVED -----------------------------------------------------------

def test_improvement_path_verdict_and_exit_code(tmp_path, config_patch, capsys):
    before, after = _improving_sides(tmp_path)
    rc = _run(tmp_path, config_patch, before, after, "--format", "json")
    assert rc == _fix_trial.EXIT_IMPROVED == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "improved"
    assert payload["exit_code"] == 0
    assert payload["verify"]["regression_axis"]["now_pass"] == 3
    assert payload["verify"]["hold_axis"]["still_pass"] == 1
    assert payload["contract_verify"] is None
    assert payload["refusal"] is None
    assert payload["apply"]["clone"]["name"] == "staging-x"


def test_run_trial_improved_reuses_verify_and_apply_exactly(tmp_path, config_patch):
    before, after = _improving_sides(tmp_path)
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
    # the default --battery reuses --before (it already carries the labels)
    assert t["battery"] == before


# --- REGRESSED: opposite-risk / hold-axis regression forces fail ------------

def test_opposite_risk_regression_forces_fail(tmp_path, config_patch, capsys):
    # f1/f2/f3 fixed (an apparent improvement), but the hold guard h1 flips
    # from passing to failing: a naive bandaid that trades talk-over for a
    # false yield. This must fail the trial even though 3 fixtures improved.
    before = _write_env(tmp_path, "before.json", [
        _ev("f1", True, False, 1.2), _ev("f2", True, False, 0.9, 2.1),
        _ev("f3", True, False, 1.5), _ev("h1", False, True, 0.0),
    ])
    after = _write_env(tmp_path, "after.json", [
        _ev("f1", True, True, 0.3, 0.4), _ev("f2", True, True, 0.2, 0.5),
        _ev("f3", True, True, 0.4, 0.6), _ev("h1", False, False, 1.1),
    ])
    rc = _run(tmp_path, config_patch, before, after, "--format", "json")
    assert rc == _fix_trial.EXIT_FAIL == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "regressed"
    assert "h1" in payload["verify"]["regressions"]
    assert "REGRESSED" in payload["conclusion"]
    assert "fail-closed" in payload["conclusion"]


def test_any_fixture_regression_forces_fail(tmp_path, config_patch, capsys):
    before = _write_env(tmp_path, "before.json", [
        _ev("f1", True, False, 1.2), _ev("f2", True, True, 0.2, 0.4),
        _ev("h1", False, True, 0.0),
    ])
    after = _write_env(tmp_path, "after.json", [
        _ev("f1", True, True, 0.2, 0.3),   # fixed
        _ev("f2", True, False, 1.0, 2.0),  # regressed (used to pass)
        _ev("h1", False, True, 0.0),
    ])
    rc = _run(tmp_path, config_patch, before, after, "--min-n", "1",
             "--format", "json")
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "regressed"
    assert "f2" in payload["verify"]["regressions"]


# --- INCONCLUSIVE: fail-closed, never a soft pass ----------------------------

def test_low_n_is_inconclusive_and_fails_closed(tmp_path, config_patch, capsys):
    # Only 2 previously-failing fixtures, below the default --min-n 3: the
    # claim is refused at the verify layer, and the trial must not pass.
    before = _write_env(tmp_path, "before.json", [
        _ev("f1", True, False, 1.2), _ev("f2", True, False, 0.9),
        _ev("h1", False, True, 0.0),
    ])
    after = _write_env(tmp_path, "after.json", [
        _ev("f1", True, True, 0.3, 0.4), _ev("f2", True, True, 0.2, 0.5),
        _ev("h1", False, True, 0.0),
    ])
    rc = _run(tmp_path, config_patch, before, after, "--format", "json")
    assert rc == _fix_trial.EXIT_FAIL == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "inconclusive"
    assert "INCONCLUSIVE" in payload["conclusion"]
    assert "not a soft pass" in payload["conclusion"]
    assert payload["verify"]["claim"]["supported"] is False


def test_zero_improvement_is_inconclusive(tmp_path, config_patch, capsys):
    # Enough previously-failing fixtures (>= min-n) but NONE now pass: not a
    # regression (nothing that used to pass now fails), but not an
    # improvement either.
    before = _write_env(tmp_path, "before.json", [
        _ev("f1", True, False, 1.2), _ev("f2", True, False, 0.9),
        _ev("f3", True, False, 1.1), _ev("h1", False, True, 0.0),
    ])
    after = _write_env(tmp_path, "after.json", [
        _ev("f1", True, False, 1.2), _ev("f2", True, False, 0.9),
        _ev("f3", True, False, 1.1), _ev("h1", False, True, 0.0),
    ])
    rc = _run(tmp_path, config_patch, before, after, "--min-n", "3",
             "--format", "json")
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "inconclusive"


# --- --contracts and --policy independently force a regression --------------

@pytest.mark.skipif(HARD is None, reason="bundled example audio unavailable")
def test_contract_regression_forces_fail_even_when_battery_improved(
        tmp_path, config_patch, capsys):
    contracts_dir = tmp_path / "contracts"
    # HARD actually yields; labelling it "hold" makes this contract fail its
    # own policy immediately, independent of anything the trial changed --
    # exactly the "neighbouring cases" check: any contract regression fails.
    rc = cli.main([
        "contract", "create", "--stereo", HARD, "--id", "neighbour-1",
        "--onset", "2.40", "--expect", "hold", "--out", str(contracts_dir),
    ])
    assert rc in (0, 1)  # create scores immediately; a failing label is fine
    capsys.readouterr()  # discard the `contract create` output

    before, after = _improving_sides(tmp_path)
    rc = _run(tmp_path, config_patch, before, after, "--contracts",
             str(contracts_dir), "--format", "json")
    assert rc == _fix_trial.EXIT_FAIL == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "regressed"
    assert payload["contract_verify"]["summary"]["failed"] >= 1
    assert "contract" in payload["conclusion"].lower()


def test_policy_violation_forces_fail(tmp_path, config_patch, capsys):
    before, after = _improving_sides(tmp_path)
    policy_path = tmp_path / "hotato.verify.yaml"
    policy_path.write_text(
        "guardrails:\n  max_new_false_yields: 0\n  require_hold_fixture: true\n"
        "target:\n  improve:\n    talk_over_sec_p95: -100\n",  # impossible target
        encoding="utf-8",
    )
    rc = _run(tmp_path, config_patch, before, after, "--policy",
             str(policy_path), "--format", "json")
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "regressed"
    assert payload["verify"]["policy"]["passed"] is False


# --- REFUSED: the both-axes threshold funnel, before any evidence is read ---

def test_threshold_funnel_refuses_before_reading_before_after(
        tmp_path, funnel_patch, capsys, monkeypatch):
    # Even nonexistent before/after paths never get opened: the refusal
    # fires before verify (or explain, or contract verify) ever runs.
    from hotato import verify as _verify

    def boom(*a, **k):
        raise AssertionError("fix trial read before/after on the refused path")

    monkeypatch.setattr(_verify, "verify_sides", boom)
    rc = cli.main([
        "fix", "trial", str(funnel_patch), "--name", "staging-x",
        "--before", str(tmp_path / "does-not-exist-before.json"),
        "--after", str(tmp_path / "does-not-exist-after.json"),
        "--format", "json",
    ])
    assert rc == _fix_trial.EXIT_REFUSED == _apply.REFUSAL_EXIT_CODE == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "refused"
    assert payload["verify"] is None
    assert payload["contract_verify"] is None
    assert payload["attribution"] is None
    assert "No config patch will be applied" in payload["refusal"]["headline"]
    assert payload["refusal"]["reason"] == _apply.REFUSAL_REASON


def test_threshold_funnel_refuses_without_name_or_before_after_text(
        funnel_patch, capsys):
    # REFUSAL-FIRST holds even with no --before/--after/--name (argparse
    # still requires --before/--after; give harmless nonexistent paths).
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

    before, after = _improving_sides(tmp_path)
    rc = _run(tmp_path, config_patch, before, after, "--format", "json")
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["clone_only"] is True
    assert payload["production_apply_supported"] is False
    # the tripwire above proves it: had run_trial called either networked
    # primitive, monkeypatch's `boom` would have raised and this line would
    # never be reached.


# --- attribution: folds in hotato explain on the BEFORE evidence ------------

def test_attribution_section_is_populated_from_explain(
        tmp_path, config_patch, capsys):
    before, after = _improving_sides(tmp_path)
    rc = _run(tmp_path, config_patch, before, after, "--format", "json")
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    a = payload["attribution"]
    assert a["schema"] == "hotato.explain.v1"
    assert a["sources"] == [before]
    assert len(a["explanations"]) == 1
    assert a["explanations"][0]["input_kind"] == "run_envelope"
    # 3 missed-interruption events with opposite-risk coverage (the hold
    # fixture) -> explain attributes them safe_to_patch, not a refusal.
    assert a["attributions"], a


def test_attribution_degrades_honestly_on_unreadable_before(
        tmp_path, config_patch, capsys):
    before = tmp_path / "before-dir"
    before.mkdir()
    (before / "not-an-envelope.json").write_text(
        json.dumps({"tool": "hotato", "kind": "not-a-run", "events": []}),
        encoding="utf-8")
    # a same-shaped envelope so verify still has something to pair; use the
    # improving after/before pair but drop the extra file into --before's dir.
    # The before WAVs move with the envelope so the provenance guard can still
    # recompute them next to it (--before is now this dir).
    before_env, after = _improving_sides(tmp_path)
    import shutil
    shutil.copy(before_env, before / "before.json")
    for name in ("f1-before", "f2-before", "f3-before", "h1-before"):
        shutil.copy(tmp_path / f"{name}.wav", before / f"{name}.wav")
    rc = _run(tmp_path, config_patch, str(before), after, "--format", "json")
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    a = payload["attribution"]
    assert a["unreadable"], a
    assert any("not-an-envelope.json" in u["source"] for u in a["unreadable"])


# --- usage errors: the same gates apply already enforces ---------------------

def test_missing_name_is_a_usage_error(tmp_path, config_patch, capsys):
    before, after = _improving_sides(tmp_path)
    rc = cli.main([
        "fix", "trial", str(config_patch), "--before", before, "--after", after,
    ])
    assert rc == 2
    assert "--name" in capsys.readouterr().err


def test_bad_contracts_dir_is_a_usage_error(tmp_path, config_patch, capsys):
    before, after = _improving_sides(tmp_path)
    empty = tmp_path / "no-contracts-here"
    empty.mkdir()
    rc = _run(tmp_path, config_patch, before, after, "--contracts", str(empty))
    assert rc == 2
    assert "no hotato contracts" in capsys.readouterr().err


def test_missing_patch_file_is_a_usage_error(tmp_path, capsys):
    before, after = _improving_sides(tmp_path)
    rc = cli.main([
        "fix", "trial", str(tmp_path / "nope.json"), "--name", "x",
        "--before", before, "--after", after,
    ])
    assert rc == 2


# --- --out / --html ----------------------------------------------------------

def test_out_writes_full_json_proof(tmp_path, config_patch, capsys):
    before, after = _improving_sides(tmp_path)
    out_path = tmp_path / "fix-trial.json"
    rc = _run(tmp_path, config_patch, before, after, "--out", str(out_path))
    assert rc == 0
    written = json.loads(out_path.read_text(encoding="utf-8"))
    assert written["verdict"] == "improved"
    assert written["schema"] == "hotato.fix_trial.v1"


def test_html_writes_a_self_contained_report(tmp_path, config_patch, capsys):
    before, after = _improving_sides(tmp_path)
    html_path = tmp_path / "fix-trial.html"
    rc = _run(tmp_path, config_patch, before, after, "--html", str(html_path))
    assert rc == 0
    html = html_path.read_text(encoding="utf-8")
    assert "<!doctype html>" in html
    assert "hotato fix trial" in html
    assert "IMPROVED" in html
    assert "coincidence" in html


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


# --- fresh-capture provenance guard ------------------------------------------

def _same_audio_sides(tmp_path):
    """Like ``_improving_sides``, but f1's AFTER audio decodes to the exact SAME
    PCM as its BEFORE audio, only with a trailing byte appended so the RAW file
    differs (the recon header-flip / append forgery: make the container digest
    change while the conversation is identical, then pass the re-score off as a
    verified fix). Every side's provenance is real and recomputable, so the ONLY
    thing wrong is that f1 is the same conversation twice."""
    _make_wav(tmp_path / "f1-same.wav", seed=1)
    f1_before = _core._audio_provenance(("stereo", str(tmp_path / "f1-same.wav")))
    # f1 after: identical samples, one trailing byte past the data chunk -> same
    # decoded PCM, different raw bytes.
    _make_wav(tmp_path / "f1-same-after.wav", seed=1, extra_bytes=b"\x00")
    f1_after = _core._audio_provenance(
        ("stereo", str(tmp_path / "f1-same-after.wav")))
    before = _write_env(tmp_path, "before.json", [
        _ev("f1", True, False, 1.2, provenance=f1_before),
        _ev("f2", True, False, 0.9, 2.1, provenance=_prov(tmp_path, "f2-before", seed=2)),
        _ev("f3", True, False, 1.5, provenance=_prov(tmp_path, "f3-before", seed=3)),
        _ev("h1", False, True, 0.0, provenance=_prov(tmp_path, "h1-before", seed=4)),
    ])
    after = _write_env(tmp_path, "after.json", [
        _ev("f1", True, True, 0.3, 0.4, provenance=f1_after),
        _ev("f2", True, True, 0.2, 0.5, provenance=_prov(tmp_path, "f2-after", seed=12)),
        _ev("f3", True, True, 0.4, 0.6, provenance=_prov(tmp_path, "f3-after", seed=13)),
        _ev("h1", False, True, 0.0, provenance=_prov(tmp_path, "h1-after", seed=14)),
    ])
    return before, after


def test_same_pcm_refuses_even_though_raw_digests_differ(
        tmp_path, config_patch, capsys):
    # THE decoded-PCM guard: f1's before/after RAW sha256 differ (a trailing
    # byte was appended), so the old byte-identity guard would have said
    # "different" and passed. The decoded PCM is identical -> same conversation
    # -> refused.
    before, after = _same_audio_sides(tmp_path)
    rc = _run(tmp_path, config_patch, before, after, "--format", "json")
    assert rc == _fix_trial.EXIT_REFUSED == _apply.REFUSAL_EXIT_CODE == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "refused"
    assert payload["exit_code"] == 3
    # Real before/after evidence WAS read: the provenance guard fires only
    # after verify already ran.
    assert payload["verify"] is not None
    assert payload["verify"]["regression_axis"]["now_pass"] == 3
    assert payload["refusal_kind"] == "same_audio_recapture"
    assert payload["refusal"]["headline"] == \
        "No fix will be certified from re-scored audio"
    assert "f1" in payload["refusal"]["reason"]
    assert "decoded PCM" in payload["refusal"]["reason"]
    assert "same conversation" in payload["refusal"]["reason"].lower()
    assert "recapture" in payload["refusal"]["recommended"]
    assert "REFUSED" in payload["conclusion"]
    prov = payload["provenance"]
    same = [f for f in prov["target_fixtures"] if f["status"] == "same_audio"]
    assert [f["fixture"] for f in same] == ["f1"]
    # the raw container digests genuinely differ; only the decoded PCM matches.
    f1 = next(f for f in prov["target_fixtures"] if f["fixture"] == "f1")
    assert f1["before_short"] != f1["after_short"]
    assert prov["issue"]["kind"] == "same_audio"


def test_same_audio_refusal_html_report_still_shows_the_real_evidence(
        tmp_path, config_patch, capsys):
    before, after = _same_audio_sides(tmp_path)
    html_path = tmp_path / "refused-provenance.html"
    rc = _run(tmp_path, config_patch, before, after, "--html", str(html_path))
    assert rc == 3
    html = html_path.read_text(encoding="utf-8")
    assert "No fix will be certified from re-scored audio" in html
    assert "Audio provenance" in html
    # Unlike the apply-gate refusal report, verify's own proof still renders:
    # the reader can see exactly what was measured, not just that it refused.
    assert "Verify: battery-scale proof" in html


def test_frozen_hold_refuses_same_pcm_on_the_hold_axis(
        tmp_path, config_patch, capsys):
    # A naive bandaid freezes the hold's audio (byte-identical before/after) so
    # it never appears to regress. The guard now covers holds too: the hold's
    # decoded PCM is identical on both sides -> same conversation -> refused,
    # even though the three targets are genuine fresh recaptures.
    _make_wav(tmp_path / "h1-frozen.wav", seed=7)
    frozen = _core._audio_provenance(("stereo", str(tmp_path / "h1-frozen.wav")))
    before = _write_env(tmp_path, "before.json", [
        _ev("f1", True, False, 1.2, provenance=_prov(tmp_path, "f1-before", seed=1)),
        _ev("f2", True, False, 0.9, 2.1, provenance=_prov(tmp_path, "f2-before", seed=2)),
        _ev("f3", True, False, 1.5, provenance=_prov(tmp_path, "f3-before", seed=3)),
        _ev("h1", False, True, 0.0, provenance=frozen),
    ])
    after = _write_env(tmp_path, "after.json", [
        _ev("f1", True, True, 0.3, 0.4, provenance=_prov(tmp_path, "f1-after", seed=11)),
        _ev("f2", True, True, 0.2, 0.5, provenance=_prov(tmp_path, "f2-after", seed=12)),
        _ev("f3", True, True, 0.4, 0.6, provenance=_prov(tmp_path, "f3-after", seed=13)),
        _ev("h1", False, True, 0.0, provenance=frozen),  # SAME block, frozen
    ])
    rc = _run(tmp_path, config_patch, before, after, "--format", "json")
    assert rc == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "refused"
    assert payload["refusal_kind"] == "same_audio_recapture"
    prov = payload["provenance"]
    same = [f for f in prov["target_fixtures"] if f["status"] == "same_audio"]
    assert [f["fixture"] for f in same] == ["h1"]
    assert same[0]["role"] == "hold"


def test_missing_provenance_both_sides_never_reaches_improved(
        tmp_path, config_patch, capsys):
    # A back-compat envelope from before this field existed (or hand-built):
    # no audio_provenance anywhere. Absence must never be silently read as
    # proof of a fresh capture.
    before = _write_env(tmp_path, "before.json", [
        _ev("f1", True, False, 1.2), _ev("f2", True, False, 0.9, 2.1),
        _ev("f3", True, False, 1.5), _ev("h1", False, True, 0.0),
    ])
    after = _write_env(tmp_path, "after.json", [
        _ev("f1", True, True, 0.3, 0.4), _ev("f2", True, True, 0.2, 0.5),
        _ev("f3", True, True, 0.4, 0.6), _ev("h1", False, True, 0.0),
    ])
    rc = _run(tmp_path, config_patch, before, after, "--format", "json")
    assert rc == _fix_trial.EXIT_FAIL == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "inconclusive"
    assert payload["refusal"] is None
    assert "UNKNOWN" in payload["conclusion"]
    assert "before and after missing" in payload["conclusion"]
    prov = payload["provenance"]
    assert prov["issue"]["kind"] == "unknown_provenance"
    assert all(f["status"] == "missing" for f in prov["target_fixtures"])


def test_missing_file_is_inconclusive_when_provenance_is_valid_but_unrecomputable(
        tmp_path, config_patch, capsys):
    # THE decisive forgery, hardened: a fully hand-written envelope carrying
    # well-formed, internally consistent provenance (valid hex, plausible
    # metadata) but NO audio files on disk. It cannot be recomputed, so it is
    # unverifiable -> inconclusive, never improved.
    before = _write_env(tmp_path, "before.json", [
        _ev("f1", True, False, 1.2, provenance=_prov(tmp_path, "f1-b", seed=1)),
        _ev("f2", True, False, 0.9, 2.1, provenance=_prov(tmp_path, "f2-b", seed=2)),
        _ev("f3", True, False, 1.5, provenance=_prov(tmp_path, "f3-b", seed=3)),
        _ev("h1", False, True, 0.0, provenance=_prov(tmp_path, "h1-b", seed=4)),
    ])
    after = _write_env(tmp_path, "after.json", [
        _ev("f1", True, True, 0.3, 0.4, provenance=_prov(tmp_path, "f1-a", seed=11)),
        _ev("f2", True, True, 0.2, 0.5, provenance=_prov(tmp_path, "f2-a", seed=12)),
        _ev("f3", True, True, 0.4, 0.6, provenance=_prov(tmp_path, "f3-a", seed=13)),
        _ev("h1", False, True, 0.0, provenance=_prov(tmp_path, "h1-a", seed=14)),
    ])
    # Delete every WAV: the envelopes still ASSERT valid identities, but hotato
    # can recompute nothing.
    for wav in tmp_path.glob("*.wav"):
        wav.unlink()
    rc = _run(tmp_path, config_patch, before, after, "--format", "json")
    assert rc == _fix_trial.EXIT_FAIL == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "inconclusive"
    assert payload["refusal"] is None
    assert "not recomputed at trial time" in payload["conclusion"]
    assert "requires provenance hotato can recompute" in payload["conclusion"]
    prov = payload["provenance"]
    assert prov["issue"]["kind"] == "unverifiable"
    assert all(f["status"] == "unverifiable" for f in prov["target_fixtures"])


def test_non_hex_digest_is_inconclusive(tmp_path, config_patch, capsys):
    # A hand-written provenance block whose sha256 is not real hex. It is an
    # unvalidated assertion, not a distinct recording -> inconclusive.
    bad = {"schema_version": "1", "sha256": "not-a-real-sha",
           "sides": [{"role": "stereo", "path": "x.wav", "sha256": "not-a-real-sha",
                      "sample_rate": 16000, "num_samples": 16000}]}
    good_a = _prov(tmp_path, "f1-a", seed=11)
    before = _write_env(tmp_path, "before.json", [
        _ev("f1", True, False, 1.2, provenance=bad),
        _ev("f2", True, False, 0.9, 2.1, provenance=_prov(tmp_path, "f2-b", seed=2)),
        _ev("f3", True, False, 1.5, provenance=_prov(tmp_path, "f3-b", seed=3)),
        _ev("h1", False, True, 0.0, provenance=_prov(tmp_path, "h1-b", seed=4)),
    ])
    after = _write_env(tmp_path, "after.json", [
        _ev("f1", True, True, 0.3, 0.4, provenance=good_a),
        _ev("f2", True, True, 0.2, 0.5, provenance=_prov(tmp_path, "f2-a", seed=12)),
        _ev("f3", True, True, 0.4, 0.6, provenance=_prov(tmp_path, "f3-a", seed=13)),
        _ev("h1", False, True, 0.0, provenance=_prov(tmp_path, "h1-a", seed=14)),
    ])
    rc = _run(tmp_path, config_patch, before, after, "--format", "json")
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "inconclusive"
    assert payload["provenance"]["issue"]["kind"] == "invalid_provenance"
    assert "MALFORMED" in payload["conclusion"]
    assert "64-char lowercase hex" in payload["conclusion"]


def test_absurd_metadata_is_inconclusive(tmp_path, config_patch, capsys):
    # Well-formed hex, but absurd metadata (recon's forged block: negative
    # frame count, an impossible sample rate). Treated as UNKNOWN, never a
    # distinct recording.
    digest = "a" * 64
    absurd = {"schema_version": "1", "sha256": digest,
              "sides": [{"role": "stereo", "path": "x.wav", "sha256": digest,
                         "sample_rate": 123, "num_samples": -5}]}
    before = _write_env(tmp_path, "before.json", [
        _ev("f1", True, False, 1.2, provenance=absurd),
        _ev("f2", True, False, 0.9, 2.1, provenance=_prov(tmp_path, "f2-b", seed=2)),
        _ev("f3", True, False, 1.5, provenance=_prov(tmp_path, "f3-b", seed=3)),
        _ev("h1", False, True, 0.0, provenance=_prov(tmp_path, "h1-b", seed=4)),
    ])
    after = _write_env(tmp_path, "after.json", [
        _ev("f1", True, True, 0.3, 0.4, provenance=_prov(tmp_path, "f1-a", seed=11)),
        _ev("f2", True, True, 0.2, 0.5, provenance=_prov(tmp_path, "f2-a", seed=12)),
        _ev("f3", True, True, 0.4, 0.6, provenance=_prov(tmp_path, "f3-a", seed=13)),
        _ev("h1", False, True, 0.0, provenance=_prov(tmp_path, "h1-a", seed=14)),
    ])
    rc = _run(tmp_path, config_patch, before, after, "--format", "json")
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "inconclusive"
    assert payload["provenance"]["issue"]["kind"] == "invalid_provenance"


def test_inconsistent_top_level_digest_is_inconclusive(tmp_path, config_patch, capsys):
    # Well-formed side digest and valid metadata, but the top-level sha256 does
    # not equal what core would compose from the sides: the structure and the
    # headline disagree -> UNKNOWN.
    inconsistent = {"schema_version": "1", "sha256": "b" * 64,
                    "sides": [{"role": "stereo", "path": "x.wav", "sha256": "c" * 64,
                               "sample_rate": 16000, "num_samples": 16000}]}
    before = _write_env(tmp_path, "before.json", [
        _ev("f1", True, False, 1.2, provenance=inconsistent),
        _ev("f2", True, False, 0.9, 2.1, provenance=_prov(tmp_path, "f2-b", seed=2)),
        _ev("f3", True, False, 1.5, provenance=_prov(tmp_path, "f3-b", seed=3)),
        _ev("h1", False, True, 0.0, provenance=_prov(tmp_path, "h1-b", seed=4)),
    ])
    after = _write_env(tmp_path, "after.json", [
        _ev("f1", True, True, 0.3, 0.4, provenance=_prov(tmp_path, "f1-a", seed=11)),
        _ev("f2", True, True, 0.2, 0.5, provenance=_prov(tmp_path, "f2-a", seed=12)),
        _ev("f3", True, True, 0.4, 0.6, provenance=_prov(tmp_path, "f3-a", seed=13)),
        _ev("h1", False, True, 0.0, provenance=_prov(tmp_path, "h1-a", seed=14)),
    ])
    rc = _run(tmp_path, config_patch, before, after, "--format", "json")
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "inconclusive"
    assert payload["provenance"]["issue"]["kind"] == "invalid_provenance"


def test_recompute_mismatch_refuses_when_the_file_disagrees(
        tmp_path, config_patch, capsys):
    # Hand-edited provenance WHERE THE FILE EXISTS: build valid provenance from
    # a real WAV, then modify a sample byte on disk so the envelope's recorded
    # digest no longer matches the audio. hotato recomputes at trial time and
    # refuses.
    before, after = _improving_sides(tmp_path)
    # tamper f1's AFTER file after the envelope recorded its identity.
    victim = tmp_path / "f1-after.wav"
    data = bytearray(victim.read_bytes())
    data[-1] ^= 0xFF  # flip a byte inside the last sample
    victim.write_bytes(bytes(data))
    rc = _run(tmp_path, config_patch, before, after, "--format", "json")
    assert rc == _fix_trial.EXIT_REFUSED == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "refused"
    assert payload["refusal_kind"] == "recompute_mismatch"
    assert payload["refusal"]["headline"] == \
        "Envelope provenance does not match the audio on disk"
    assert "f1" in payload["refusal"]["reason"]
    assert "match the audio present on disk" in payload["refusal"]["reason"]
    assert payload["provenance"]["issue"]["kind"] == "recompute_mismatch"


def test_omit_hold_from_after_refuses_incomplete_battery(
        tmp_path, config_patch, capsys):
    # The hold h1 passes before but is DROPPED from the after set: an
    # incomplete, cherry-picked comparison. The targets improved, but the
    # comparison is not over the same battery -> refused, with h1 named.
    before, after_full = _improving_sides(tmp_path)
    # rewrite after.json without h1
    after_events = _env([
        _ev("f1", True, True, 0.3, 0.4, provenance=_prov(tmp_path, "f1-after2", seed=21)),
        _ev("f2", True, True, 0.2, 0.5, provenance=_prov(tmp_path, "f2-after2", seed=22)),
        _ev("f3", True, True, 0.4, 0.6, provenance=_prov(tmp_path, "f3-after2", seed=23)),
    ])
    after = tmp_path / "after.json"
    after.write_text(json.dumps(after_events), encoding="utf-8")
    rc = _run(tmp_path, config_patch, before, str(after), "--format", "json")
    assert rc == _fix_trial.EXIT_REFUSED == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "refused"
    assert payload["refusal_kind"] == "incomplete_after"
    assert payload["refusal"]["headline"] == \
        "No fix will be certified from an incomplete after set"
    assert "h1" in payload["refusal"]["reason"]
    assert "hold" in payload["refusal"]["reason"]
    required = payload["verify"]["unpaired"]["only_before_required"]
    assert [r["fixture"] for r in required] == ["h1"]
    assert required[0]["role"] == "hold"


def test_omit_target_from_after_refuses_incomplete_battery(
        tmp_path, config_patch, capsys):
    # A previously-failing target dropped from the after set (cherry-picking the
    # ones that got fixed) is equally incomplete -> refused.
    before, _after_full = _improving_sides(tmp_path)
    after_events = _env([
        _ev("f1", True, True, 0.3, 0.4, provenance=_prov(tmp_path, "f1-after2", seed=21)),
        _ev("f2", True, True, 0.2, 0.5, provenance=_prov(tmp_path, "f2-after2", seed=22)),
        # f3 (a target that used to fail) is omitted
        _ev("h1", False, True, 0.0, provenance=_prov(tmp_path, "h1-after2", seed=24)),
    ])
    after = tmp_path / "after.json"
    after.write_text(json.dumps(after_events), encoding="utf-8")
    rc = _run(tmp_path, config_patch, before, str(after), "--min-n", "2",
             "--format", "json")
    assert rc == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "refused"
    assert payload["refusal_kind"] == "incomplete_after"
    required = payload["verify"]["unpaired"]["only_before_required"]
    assert [r["fixture"] for r in required] == ["f3"]
    assert required[0]["role"] == "target"


def test_distinct_audio_reaches_improved_with_provenance_surfaced(
        tmp_path, config_patch, capsys):
    before, after = _improving_sides(tmp_path)
    rc = _run(tmp_path, config_patch, before, after, "--format", "json")
    assert rc == _fix_trial.EXIT_IMPROVED == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "improved"
    prov = payload["provenance"]
    assert prov["issue"] is None
    fixtures = {f["fixture"]: f for f in prov["target_fixtures"]}
    # the hold h1 is now guarded too, so it appears alongside the targets.
    assert set(fixtures) == {"f1", "f2", "f3", "h1"}
    for f in fixtures.values():
        assert f["status"] == "verified"
        assert f["before_short"] and f["after_short"]
        assert f["before_short"] != f["after_short"]


def test_min_n_is_echoed_in_every_surface(tmp_path, config_patch, capsys):
    before, after = _improving_sides(tmp_path)
    # text
    rc = _run(tmp_path, config_patch, before, after, "--min-n", "2")
    assert rc == 0
    text_out = capsys.readouterr().out
    assert "min-n=2" in text_out
    assert "min-n 2" in text_out  # in the conclusion line
    # json
    rc = _run(tmp_path, config_patch, before, after, "--min-n", "2",
             "--format", "json")
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["min_n"] == 2
    # html
    html_path = tmp_path / "fix-trial.html"
    rc = _run(tmp_path, config_patch, before, after, "--min-n", "2",
             "--html", str(html_path))
    assert rc == 0
    html = html_path.read_text(encoding="utf-8")
    assert "min-n" in html and ">2<" in html


# --- report-facing provenance caution (fresh-capture report) ----------------

def test_provenance_caution_appears_in_text_output(tmp_path, config_patch, capsys):
    before, after = _improving_sides(tmp_path)
    rc = _run(tmp_path, config_patch, before, after)
    assert rc == 0
    text_out = capsys.readouterr().out
    assert _fix_trial._PROVENANCE_CAUTION in text_out


def test_provenance_caution_appears_in_html_output(tmp_path, config_patch, capsys):
    before, after = _improving_sides(tmp_path)
    html_path = tmp_path / "fix-trial.html"
    rc = _run(tmp_path, config_patch, before, after, "--html", str(html_path))
    assert rc == 0
    html = html_path.read_text(encoding="utf-8")
    assert "Provenance caution" in html
    assert "at the revision it was captured from" in html


def test_provenance_caution_appears_even_on_same_audio_refusal(
        tmp_path, config_patch, capsys):
    # The provenance section (and its caution) renders below the refusal
    # banner too: the guard fires AFTER verify already ran, so the full
    # report -- including this caution -- is not withheld just because the
    # verdict downgraded to refused.
    before, after = _same_audio_sides(tmp_path)
    rc = _run(tmp_path, config_patch, before, after)
    assert rc == 3
    text_out = capsys.readouterr().out
    assert _fix_trial._PROVENANCE_CAUTION in text_out


# --- no em or en dashes anywhere ---------------------------------------------

def test_no_em_or_en_dashes_in_any_rendered_output(tmp_path, config_patch, capsys):
    before, after = _improving_sides(tmp_path)
    rc = _run(tmp_path, config_patch, before, after)
    assert rc == 0
    text_out = capsys.readouterr().out
    assert "—" not in text_out
    assert "–" not in text_out

    html_path = tmp_path / "fix-trial.html"
    rc = _run(tmp_path, config_patch, before, after, "--html", str(html_path))
    assert rc == 0
    capsys.readouterr()
    html = html_path.read_text(encoding="utf-8")
    assert "—" not in html
    assert "–" not in html
