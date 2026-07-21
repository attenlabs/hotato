"""`hotato diagnose` (Level 0 of the guarded fix ladder): every finding class,
the threshold-funnel rule, the slow-yield ambiguity rule, and the not-scorable
exclusion. Real inputs come from the packaged demo battery and the bundled
suite; synthetic envelopes pin each classification branch."""

import json
from importlib import resources

import pytest

from hotato import cli
from hotato.core import run_suite
from hotato.diagnose import (
    FINDINGS,
    LAYERS,
    advisory_for,
    cluster_fleet,
    diagnose_envelope,
    fingerprint_failure,
    magnitude_bucket,
    opposite_risk_coverage,
    render_fleet_text,
    render_text,
    scan_fleet,
)

# --- helpers ----------------------------------------------------------------

def _event(
    event_id,
    *,
    expected_yield,
    did_yield,
    passed,
    reasons=(),
    seconds_to_yield=None,
    talk_over_sec=0.0,
    scenario_id=None,
    scorable=None,
    not_scorable_reason=None,
    response_gap_sec=None,
    premature_start_sec=None,
):
    ev = {
        "event_id": event_id,
        "scenario_id": scenario_id or event_id,
        "title": event_id,
        "category": "should_yield" if expected_yield else "should_not_yield",
        "expected_yield": expected_yield,
        "verdict": {
            "passed": passed,
            "did_yield": did_yield,
            "seconds_to_yield": seconds_to_yield,
            "talk_over_sec": talk_over_sec,
            "reasons": list(reasons),
        },
        "measurements": {
            "caller_onset_sec": 2.0,
            "agent_talking_at_onset": True,
            "hop_sec": 0.01,
            "notes": "",
        },
        "signals": {
            "barge_in": {
                "did_yield": did_yield,
                "time_to_yield_sec": seconds_to_yield,
                "talk_over_sec": talk_over_sec,
            },
            "latency": {
                "response_gap_sec": response_gap_sec,
                "premature_start_sec": premature_start_sec,
            },
        },
        "fix": None,
    }
    if scorable is False:
        ev["scorable"] = False
        ev["not_scorable_reason"] = not_scorable_reason or "input problem"
    return ev


def _envelope(events):
    return {
        "tool": "hotato",
        "schema_version": "1",
        "mode": "suite",
        "stack": "generic",
        "offline": True,
        "events": events,
        "exit_code": 1,
    }


MISSED = dict(expected_yield=True, did_yield=False, passed=False,
              reasons=["expected the agent to yield but it kept talking"],
              talk_over_sec=2.5)
FALSE_STOP = dict(expected_yield=False, did_yield=True, passed=False,
                  reasons=["expected the agent to keep the floor but it "
                           "yielded (a false or phantom barge-in)"],
                  seconds_to_yield=0.25, talk_over_sec=0.25)
SLOW = dict(expected_yield=True, did_yield=True, passed=False,
            reasons=["yielded in 1.40s, slower than the 0.70s bound"],
            seconds_to_yield=1.4, talk_over_sec=1.4)
TALK_OVER = dict(expected_yield=True, did_yield=True, passed=False,
                 reasons=["talked over the caller for 1.20s, more than the "
                          "0.50s bound"],
                 seconds_to_yield=1.2, talk_over_sec=1.2)
PASS_HOLD = dict(expected_yield=False, did_yield=False, passed=True)
PASS_YIELD = dict(expected_yield=True, did_yield=True, passed=True,
                  seconds_to_yield=0.3, talk_over_sec=0.3)


def _one(diagnosis, event_id):
    matches = [d for d in diagnosis["diagnoses"] if d["event_id"] == event_id]
    assert len(matches) == 1, f"expected one diagnosis for {event_id}"
    return matches[0]


def _demo_envelope():
    root = resources.files("hotato").joinpath("data", "demo", "failing")
    return run_suite(
        scenarios_dir=str(root.joinpath("scenarios")),
        audio_dir=str(root.joinpath("audio")),
        stack="generic",
    )


# --- per-finding classification ----------------------------------------------

def test_missed_real_interruption_alone():
    d = diagnose_envelope(_envelope([_event("a", **MISSED),
                                     _event("b", **PASS_HOLD)]))
    dg = _one(d, "a")
    assert dg["finding"] == "missed_real_interruption"
    assert dg["likely_layer"] == "interruption_detection"
    assert dg["config_only_safe"] is True
    assert dg["evidence"]["talk_over_sec"] == 2.5
    assert dg["evidence"]["did_yield"] is False
    assert d["battery"]["decision"] == "tune_one_step_with_verification"


def test_false_stop_on_backchannel_alone():
    d = diagnose_envelope(_envelope([_event("a", **FALSE_STOP),
                                     _event("b", **PASS_YIELD)]))
    dg = _one(d, "a")
    assert dg["finding"] == "false_stop_on_backchannel"
    assert dg["likely_layer"] == "interruption_detection"
    assert dg["config_only_safe"] is True
    assert d["battery"]["finding"] is None  # no funnel with one axis failing


def test_slow_yield_without_opposite_risk_is_unknown_root_cause():
    d = diagnose_envelope(_envelope([_event("a", **SLOW)]))
    dg = _one(d, "a")
    assert dg["finding"] == "slow_yield"
    assert dg["likely_layer"] == "unknown_root_cause"
    assert dg["config_only_safe"] is False
    # The ambiguity must be stated: one recording cannot separate the layers.
    assert "indistinguishable" in dg["notes"]
    assert "TTS" in dg["notes"]
    assert d["battery"]["decision"] == "needs_instrumentation"


def test_slow_yield_with_passing_opposite_risk_fixture_is_config_safe():
    d = diagnose_envelope(_envelope([_event("a", **SLOW),
                                     _event("b", **PASS_HOLD)]))
    dg = _one(d, "a")
    assert dg["finding"] == "slow_yield"
    assert dg["likely_layer"] == "endpointing"
    assert dg["config_only_safe"] is True
    # Root cause stays inferred, and honesty about that survives coverage.
    assert "indistinguishable" in dg["notes"]


def test_excess_talk_over():
    d = diagnose_envelope(_envelope([_event("a", **TALK_OVER),
                                     _event("b", **PASS_HOLD)]))
    dg = _one(d, "a")
    assert dg["finding"] == "excess_talk_over"
    assert dg["likely_layer"] == "interruption_detection"
    assert dg["config_only_safe"] is True


def test_endpointing_miss_from_reason_text():
    ev = _event("a", expected_yield=True, did_yield=True, passed=False,
                reasons=["response gap 1.80s exceeded the 1.00s bound"],
                seconds_to_yield=0.3, talk_over_sec=0.3,
                response_gap_sec=1.8)
    d = diagnose_envelope(_envelope([ev]))
    dg = _one(d, "a")
    assert dg["finding"] == "endpointing_miss"
    assert dg["likely_layer"] == "endpointing"
    assert dg["config_only_safe"] is True
    assert dg["evidence"]["response_gap_sec"] == 1.8


def test_echo_false_stop_is_not_a_threshold_problem():
    ev = _event("echo-1", scenario_id="07-echo-bleed", **FALSE_STOP)
    d = diagnose_envelope(_envelope([ev]))
    dg = _one(d, "echo-1")
    assert dg["finding"] == "false_stop_on_backchannel"
    assert dg["likely_layer"] == "unknown_root_cause"
    assert dg["config_only_safe"] is False
    assert "audio" in dg["notes"].lower()


def test_echo_false_stop_does_not_arm_the_funnel():
    events = [_event("a", **MISSED),
              _event("echo-1", scenario_id="07-echo-bleed", **FALSE_STOP)]
    d = diagnose_envelope(_envelope(events))
    assert d["battery"]["finding"] is None
    assert d["battery"]["decision"] != "do_not_tune_single_threshold"


# --- the threshold funnel ------------------------------------------------------

def test_funnel_on_packaged_demo_battery():
    env = _demo_envelope()
    assert env["funnel"] is not None  # the envelope itself flags it
    d = diagnose_envelope(env)
    assert d["battery"]["finding"] == "threshold_funnel"
    assert d["battery"]["decision"] == "do_not_tune_single_threshold"
    missed = _one(d, "fd-01-missed-interruption")
    false_stop = _one(d, "fd-02-backchannel-yielded")
    assert missed["finding"] == "missed_real_interruption"
    assert false_stop["finding"] == "false_stop_on_backchannel"
    # Both participating findings are locked out of single-threshold tuning.
    assert missed["config_only_safe"] is False
    assert false_stop["config_only_safe"] is False


def test_funnel_battery_finding_is_a_declared_finding():
    env = _demo_envelope()
    d = diagnose_envelope(env)
    assert d["battery"]["finding"] in FINDINGS
    for dg in d["diagnoses"]:
        assert dg["finding"] in FINDINGS
        assert dg["likely_layer"] in LAYERS or dg["likely_layer"] is None


# --- not-scorable exclusion -----------------------------------------------------

def test_not_scorable_event_is_never_an_agent_failure():
    ns = _event("bad-input", expected_yield=True, did_yield=False, passed=False,
                reasons=["the agent was not talking at the caller onset"],
                scorable=False,
                not_scorable_reason="the agent was not talking at the caller "
                                    "onset, so a should-yield verdict has no "
                                    "meaning for this recording.")
    d = diagnose_envelope(_envelope([ns]))
    dg = _one(d, "bad-input")
    assert dg["finding"] == "not_scorable"
    assert dg["config_only_safe"] is False
    assert dg["likely_layer"] is None
    assert "never an agent failure" in dg["notes"].lower()
    # Excluded from the battery judgement entirely.
    assert d["battery"]["failed"] == 0
    assert d["battery"]["not_scorable"] == 1
    assert d["battery"]["decision"] == "no_failures"


def test_missing_audio_event_is_treated_as_input_problem():
    ev = _event("gone", expected_yield=True, did_yield=False, passed=False,
                reasons=["missing audio: /nowhere/gone.wav"])
    d = diagnose_envelope(_envelope([ev]))
    dg = _one(d, "gone")
    assert dg["finding"] == "not_scorable"
    assert d["battery"]["failed"] == 0


def test_not_scorable_never_arms_funnel_or_coverage():
    ns_hold = _event("ns", expected_yield=False, did_yield=True, passed=False,
                     scorable=False, not_scorable_reason="input problem")
    d = diagnose_envelope(_envelope([_event("a", **MISSED), ns_hold]))
    assert d["battery"]["finding"] is None  # no funnel from a not-scorable event
    cov = opposite_risk_coverage([ns_hold])
    assert cov["backchannel_hold_pass"] is False


# --- battery decisions -----------------------------------------------------------

def test_bundled_suite_diagnoses_clean():
    env = run_suite()
    d = diagnose_envelope(env)
    assert d["diagnoses"] == []
    assert d["battery"]["decision"] == "no_failures"
    assert d["battery"]["failed"] == 0


def test_insufficient_coverage_when_opposite_risk_fixture_missing():
    d = diagnose_envelope(_envelope([_event("a", **MISSED)]))
    assert d["battery"]["decision"] == "insufficient_coverage"
    assert "backchannel" in d["battery"]["notes"]


def test_opposite_risk_coverage_scan():
    events = [_event("a", **PASS_HOLD), _event("b", **PASS_YIELD)]
    cov = opposite_risk_coverage(events)
    assert cov["backchannel_hold_pass"] is True
    assert cov["real_interruption_pass"] is True
    assert cov["measured_latency_pass"] is False


# --- text mode (the Level 0 advisory) ---------------------------------------------

def test_text_mode_prints_the_honest_advisory():
    d = diagnose_envelope(_envelope([_event("a", **MISSED),
                                     _event("b", **PASS_HOLD)]))
    text = render_text(d)
    assert "Missed real interruption. Likely config layer." in text
    assert "lowering the stop-speaking word threshold one step" in text
    assert "Tradeoff: may increase false stops on short acknowledgements" in text


def test_every_config_safe_advisory_states_a_tradeoff():
    for finding in ("missed_real_interruption", "false_stop_on_backchannel",
                    "slow_yield", "excess_talk_over", "endpointing_miss"):
        assert "Tradeoff:" in advisory_for(finding, True)


def test_unsafe_advisory_fallback_never_suggests_a_knob():
    # Fail-closed: a finding marked NOT config-only-safe must never fall back
    # to a "try lowering/raising ..." advisory, even where no specific
    # unsafe-variant text exists.
    for finding in ("excess_talk_over", "endpointing_miss"):
        text = advisory_for(finding, False)
        assert "Try lowering" not in text
        assert "Try raising" not in text
        assert "not safe" in text


# --- CLI surface -------------------------------------------------------------------

def test_cli_diagnose_exit_1_on_failing_envelope(tmp_path, capsys):
    path = tmp_path / "demo.json"
    path.write_text(json.dumps(_demo_envelope()), encoding="utf-8")
    assert cli.main(["diagnose", str(path)]) == 1
    out = capsys.readouterr().out
    assert "do_not_tune_single_threshold" in out


def test_cli_diagnose_exit_0_on_clean_envelope(tmp_path):
    path = tmp_path / "clean.json"
    path.write_text(json.dumps(run_suite()), encoding="utf-8")
    assert cli.main(["diagnose", str(path)]) == 0


def test_cli_diagnose_exit_2_on_garbage(tmp_path):
    path = tmp_path / "junk.json"
    path.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    assert cli.main(["diagnose", str(path)]) == 2
    assert cli.main(["diagnose", "/nonexistent/nope.json"]) == 2


def test_cli_diagnose_json_shape(tmp_path, capsys):
    path = tmp_path / "demo.json"
    path.write_text(json.dumps(_demo_envelope()), encoding="utf-8")
    cli.main(["diagnose", str(path), "--format", "json"])
    doc = json.loads(capsys.readouterr().out)
    assert doc["kind"] == "diagnosis"
    for dg in doc["diagnoses"]:
        assert set(dg) >= {"finding", "evidence", "likely_layer",
                           "config_only_safe", "notes"}


def test_diagnose_rejects_frame_dump():
    with pytest.raises(ValueError):
        diagnose_envelope({"tool": "hotato", "kind": "frame-dump",
                           "events": []})


# --- defect (round 3): non-speech ambient false-yield diagnosis --------------

def test_ambient_noise_false_yield_is_config_not_backchannel_or_funnel():
    """A non-speech ambient false-yield (marker) beside a missed real
    interruption: diagnose must (1) find false_stop_on_ambient_noise (config,
    not backchannel), (2) never fabricate 'I'm listening' caller intent, and
    (3) NOT declare the both-axes threshold funnel."""
    missed = _event("fd-missed", expected_yield=True, did_yield=False,
                    passed=False, reasons=["kept talking over the caller"])
    ambient = _event("nh-cafe", expected_yield=False, did_yield=True,
                     passed=False, reasons=["yielded to the caller"])
    ambient["non_speech"] = True          # the durable corpus marker
    ambient["tags"] = ["noise-hold", "ambient", "non-speech"]
    ambient["family"] = "noise-hold"

    diag = diagnose_envelope({"tool": "hotato", "mode": "suite",
                              "events": [missed, ambient]})
    by = {d["scenario_id"]: d for d in diag["diagnoses"]}
    assert by["nh-cafe"]["finding"] == "false_stop_on_ambient_noise"
    assert by["nh-cafe"]["config_only_safe"] is True
    notes = by["nh-cafe"]["notes"].lower()
    # the fabricated caller-intent claim from the old backchannel notes is gone
    assert "signalled" not in notes and "only signalled" not in notes
    assert "ambient" in notes or "noise" in notes
    # the funnel must NOT fire -> no do_not_tune_single_threshold, no SAA pointer
    assert diag["battery"]["finding"] != "threshold_funnel"
    assert diag["battery"]["decision"] != "do_not_tune_single_threshold"


def test_real_backchannel_beside_missed_still_funnels():
    """Control: a genuine backchannel false-stop (no non_speech marker) beside a
    missed interruption still trips the threshold funnel."""
    missed = _event("miss", expected_yield=True, did_yield=False,
                    passed=False, reasons=["kept talking"])
    bc = _event("bc", expected_yield=False, did_yield=True, passed=False,
                reasons=["yielded to a backchannel"])
    diag = diagnose_envelope({"tool": "hotato", "mode": "suite",
                              "events": [missed, bc]})
    assert diag["battery"]["finding"] == "threshold_funnel"
    assert diag["battery"]["decision"] == "do_not_tune_single_threshold"


def test_ambient_finding_is_in_findings_enum():
    assert "false_stop_on_ambient_noise" in FINDINGS


# --- cross-run failure clustering (hotato diagnose --fleet DIR) --------------

def _write_fleet(tmp_path, files):
    """Write {name: envelope-dict} into a directory and return its path."""
    d = tmp_path / "runs"
    d.mkdir()
    for name, env in files.items():
        (d / name).write_text(json.dumps(env), encoding="utf-8")
    return str(d)


def test_magnitude_bucket_boundaries_are_half_open_and_stable():
    # Fixed boundaries; a bucket edge lands in the UPPER bucket [lo, hi).
    assert magnitude_bucket(0.0) == "0.0-0.2s"
    assert magnitude_bucket(0.19) == "0.0-0.2s"
    assert magnitude_bucket(0.2) == "0.2-0.5s"
    assert magnitude_bucket(0.49) == "0.2-0.5s"
    assert magnitude_bucket(0.5) == "0.5-1.0s"
    assert magnitude_bucket(0.999) == "0.5-1.0s"
    assert magnitude_bucket(1.0) == "1.0-2.0s"
    assert magnitude_bucket(2.0) == "2.0s+"
    assert magnitude_bucket(12.3) == "2.0s+"
    # None / non-numeric never crash: they land in the 'unknown' bucket.
    assert magnitude_bucket(None) == "unknown"
    assert magnitude_bucket("nope") == "unknown"


def test_fingerprint_axes_per_finding():
    missed = diagnose_envelope(_envelope([_event("a", **MISSED),
                                          _event("b", **PASS_HOLD)]))["diagnoses"]
    fp = fingerprint_failure(next(d for d in missed if d["finding"] ==
                                  "missed_real_interruption"))
    assert fp == {"dimension": "interruption_yield", "direction": "no_yield",
                  "magnitude_bucket": "2.0s+", "config_hash": None}

    talk = diagnose_envelope(_envelope([_event("a", **TALK_OVER),
                                        _event("b", **PASS_HOLD)]))["diagnoses"]
    fp = fingerprint_failure(next(d for d in talk if d["finding"] ==
                                  "excess_talk_over"))
    assert fp["dimension"] == "talk_over"
    assert fp["direction"] == "over"
    assert fp["magnitude_bucket"] == "1.0-2.0s"


def test_known_corpus_clusters_exactly():
    # run1: a missed interruption (talk_over 2.5s) + a passing hold guard.
    # run2: the SAME missed interruption + an excess talk-over (1.2s).
    # run3: a slow yield (1.4s).
    files = {
        "run1.json": _envelope([_event("a", **MISSED), _event("b", **PASS_HOLD)]),
        "run2.json": _envelope([_event("a", **MISSED), _event("c", **TALK_OVER)]),
        "run3.json": _envelope([_event("a", **SLOW)]),
    }
    result = cluster_fleet([(n, e) for n, e in sorted(files.items())])

    assert result["kind"] == "fleet-clusters"
    assert result["envelopes_scanned"] == 3
    assert result["failures"] == 4  # missed x2, talk_over x1, slow x1
    clusters = result["clusters"]
    assert len(clusters) == 3

    # Ranked by count: the recurring missed interruption first, at count 2.
    top = clusters[0]
    assert top["finding"] == "missed_real_interruption"
    assert top["count"] == 2
    assert top["fingerprint"]["magnitude_bucket"] == "2.0s+"
    assert [(m["source"], m["event_id"]) for m in top["members"]] == [
        ("run1.json", "a"), ("run2.json", "a")]

    # The two singletons are ordered by fingerprint id (deterministic tiebreak).
    rest = clusters[1:]
    assert {c["finding"] for c in rest} == {"excess_talk_over", "slow_yield"}
    assert all(c["count"] == 1 for c in rest)
    ids = [c["fingerprint_id"] for c in rest]
    assert ids == sorted(ids)


def test_config_hash_splits_otherwise_identical_failures():
    """The SAME failure under two different agent configs must NOT collapse into
    one cluster; the config hash is part of the fingerprint when present."""
    base = _envelope([_event("a", **MISSED)])
    cfg_a = dict(base, config={"interrupt_min_words": 2})
    cfg_b = dict(base, config={"interrupt_min_words": 5})
    result = cluster_fleet([("a.json", cfg_a), ("b.json", cfg_b)])
    assert len(result["clusters"]) == 2
    hashes = {c["fingerprint"]["config_hash"] for c in result["clusters"]}
    assert None not in hashes and len(hashes) == 2
    # No config present -> null hash, and the two collapse back to one cluster.
    plain = cluster_fleet([("a.json", base), ("b.json", base)])
    assert len(plain["clusters"]) == 1
    assert plain["clusters"][0]["fingerprint"]["config_hash"] is None


def test_not_scorable_events_never_become_fleet_failures():
    ns = _event("bad", expected_yield=True, did_yield=False, passed=False,
                scorable=False, not_scorable_reason="input problem")
    env = _envelope([ns, _event("ok", **PASS_HOLD)])
    result = cluster_fleet([("run.json", env)])
    assert result["failures"] == 0
    assert result["clusters"] == []


def test_scan_fleet_is_byte_reproducible(tmp_path):
    files = {
        "run1.json": _envelope([_event("a", **MISSED), _event("b", **PASS_HOLD)]),
        "run2.json": _envelope([_event("a", **MISSED), _event("c", **TALK_OVER)]),
        "run3.json": _envelope([_event("a", **SLOW)]),
    }
    d = _write_fleet(tmp_path, files)
    a = json.dumps(scan_fleet(d), indent=2, sort_keys=True)
    b = json.dumps(scan_fleet(d), indent=2, sort_keys=True)
    assert a == b
    # And independent of file-visit order: renaming does not shuffle output
    # (source names change, but the ranking/bucketing logic is identical).
    assert scan_fleet(d)["clusters"][0]["fingerprint_id"] == \
        scan_fleet(d)["clusters"][0]["fingerprint_id"]


def test_scan_fleet_refuses_empty_directory(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    with pytest.raises(ValueError):
        scan_fleet(str(d))


def test_scan_fleet_refuses_missing_directory(tmp_path):
    with pytest.raises(ValueError):
        scan_fleet(str(tmp_path / "nope"))


def test_scan_fleet_refuses_malformed_member(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    (d / "good.json").write_text(json.dumps(_envelope([_event("a", **MISSED)])),
                                 encoding="utf-8")
    (d / "bad.json").write_text("{ not valid json", encoding="utf-8")
    with pytest.raises(ValueError):
        scan_fleet(str(d))


def test_scan_fleet_refuses_non_envelope_member(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    (d / "not-hotato.json").write_text(json.dumps({"hello": "world"}),
                                       encoding="utf-8")
    with pytest.raises(ValueError):
        scan_fleet(str(d))


def test_render_fleet_text_ranks_clusters():
    files = {
        "run1.json": _envelope([_event("a", **MISSED)]),
        "run2.json": _envelope([_event("a", **MISSED)]),
    }
    result = cluster_fleet([(n, e) for n, e in sorted(files.items())])
    text = render_fleet_text(result)
    assert "2 failure(s), 1 cluster(s)" in text
    assert "[1] x2  missed_real_interruption" in text
    assert "run1.json:a" in text and "run2.json:a" in text


# --- CLI surface for --fleet -------------------------------------------------

def test_cli_diagnose_fleet_exit_1_on_failures(tmp_path, capsys):
    d = tmp_path / "runs"
    d.mkdir()
    (d / "run1.json").write_text(json.dumps(_envelope([_event("a", **MISSED)])),
                                 encoding="utf-8")
    (d / "run2.json").write_text(json.dumps(_envelope([_event("a", **MISSED)])),
                                 encoding="utf-8")
    assert cli.main(["diagnose", "--fleet", str(d)]) == 1
    assert "missed_real_interruption" in capsys.readouterr().out


def test_cli_diagnose_fleet_exit_0_when_clean(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    (d / "clean.json").write_text(json.dumps(run_suite()), encoding="utf-8")
    assert cli.main(["diagnose", "--fleet", str(d)]) == 0


def test_cli_diagnose_fleet_json_shape(tmp_path, capsys):
    d = tmp_path / "runs"
    d.mkdir()
    (d / "run1.json").write_text(json.dumps(_envelope([_event("a", **MISSED),
                                                       _event("c", **TALK_OVER)])),
                                 encoding="utf-8")
    assert cli.main(["diagnose", "--fleet", str(d), "--format", "json"]) == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["kind"] == "fleet-clusters"
    assert doc["tool"] == "hotato" and doc["schema_version"] == "1"
    for c in doc["clusters"]:
        assert set(c) >= {"fingerprint_id", "fingerprint", "finding",
                          "count", "members"}
        assert set(c["fingerprint"]) == {"dimension", "direction",
                                         "magnitude_bucket", "config_hash"}


def test_cli_diagnose_fleet_refuses_malformed(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    (d / "bad.json").write_text("not json at all", encoding="utf-8")
    assert cli.main(["diagnose", "--fleet", str(d)]) == 2


def test_cli_diagnose_without_envelope_or_fleet_is_usage_error(capsys):
    assert cli.main(["diagnose"]) == 2
