"""Phase-2 2.2: the parallel scenario-matrix runner (the "simulate hundreds"
deterministic EXIT).

Pins the invariants that are the point of the slice:

* a scenario whose variation_matrix expands to >= 100 concrete runs executes via
  run_matrix and returns a summary with the right total;
* DETERMINISM UNDER PARALLELISM -- run_matrix twice is byte-identical AND
  run_matrix at max_workers=1 vs 8 is byte-identical (per-run seeds are pure
  hashes, no shared mutable state, results sorted deterministically);
* a conversation-test scored over the matrix yields correct per-variation
  pass@1/pass@k/pass^k (reusing reliability());
* a SIMULATOR_INVALID run lands in its OWN bucket and is EXCLUDED from the agent
  pass/fail aggregate (a broken fixture, never an agent PASS/FAIL);
* NO overall_score anywhere in the summary or the CLI json;
* the CLI `hotato simulate --matrix` produces artifacts `hotato conversation
  verify` accepts and prints an ATTRIBUTABLE per-variation summary.
"""

import json

from hotato import cli
from hotato import simulate as SIM


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

def _scenario(**over):
    doc = {
        "kind": "hotato.scenario", "version": 1, "id": "refund-basic",
        "goal": {"type": "get_refund", "target": "order A-1001"},
        "facts": {"order_id": "A-1001"},
        "caller": {
            "script": [
                {"say": "Hi, my order A-1001 arrived damaged and I want a refund."},
                {"when_agent_asks": "order_id", "say": "It is A-1001."},
                {"say": "Please send the refund to my card."},
            ],
            "behavior": {
                "speaking_rate": 1.0,
                "interruptions": [{"trigger": "greeting", "offset_ms": 800}],
                "backchannels": {"probability": 0.5},
            },
        },
        "environment": {"noise": "clean", "locale": "en-US"},
        "variation_matrix": {
            "locale": ["en-US", "es-ES"],
            "speaking_rate": [0.9, 1.1],
            "noise": ["clean", "cafe"],
            "repetitions": 2,
        },
        "seed": 7,
    }
    doc.update(over)
    return doc


def _big_scenario():
    # 5 locale x 5 rate x 2 noise x 1 behavior x 2 reps = 100 concrete runs.
    return _scenario(variation_matrix={
        "locale": ["en-US", "es-ES", "fr-FR", "de-DE", "ja-JP"],
        "speaking_rate": [0.8, 0.9, 1.0, 1.1, 1.2],
        "noise": ["clean", "cafe"],
        "repetitions": 2,
    })


def _conv_test(deterministic):
    return {
        "kind": "hotato.conversation-test", "version": 1,
        "id": "refund-test", "agent": "my-agent-v1",
        "assertions": {"deterministic": deterministic},
    }


def _write(tmp_path, doc, name):
    p = tmp_path / name
    p.write_text(json.dumps(doc), encoding="utf-8")
    return str(p)


# --------------------------------------------------------------------------
# >= 100 concrete runs, right total
# --------------------------------------------------------------------------

def test_matrix_expands_to_at_least_100_runs_with_right_total():
    doc = _big_scenario()
    assert len(SIM.expand(doc)) == 100
    summary = SIM.run_matrix(doc)
    assert summary["total"] == 100
    assert summary["counts"]["runs"] == 100
    assert len(summary["runs"]) == 100
    # every run is attributable: variation tuple + seed + status + (artifact) path
    for rec in summary["runs"]:
        assert set(rec["variation"]) == {
            "locale", "speaking_rate", "noise", "behavior", "repetition"}
        assert isinstance(rec["seed"], int)
        assert rec["origin_kind"] == "simulated"
        assert "valid" in rec and "run_id" in rec
    assert summary["all_simulated"] is True


# --------------------------------------------------------------------------
# DETERMINISM UNDER PARALLELISM
# --------------------------------------------------------------------------

def _canon(summary):
    return json.dumps(summary, sort_keys=True)


def test_run_matrix_twice_is_byte_identical():
    doc = _big_scenario()
    a = SIM.run_matrix(doc)
    b = SIM.run_matrix(doc)
    assert _canon(a) == _canon(b)


def test_run_matrix_byte_identical_across_worker_counts():
    # THE determinism-under-parallelism proof: the same scenario -> the same
    # seeds -> a byte-identical summary regardless of the worker count.
    doc = _big_scenario()
    one = SIM.run_matrix(doc, max_workers=1)
    eight = SIM.run_matrix(doc, max_workers=8)
    assert _canon(one) == _canon(eight)


def test_run_matrix_scored_is_byte_identical_across_worker_counts():
    # ... and it holds with a conversation-test scored over every run too.
    doc = _big_scenario()
    ct = _conv_test([{"id": "asked-refund", "kind": "phrase",
                      "regex": "refund", "role": "caller"}])
    one = SIM.run_matrix(doc, conversation_test=ct, max_workers=1)
    eight = SIM.run_matrix(doc, conversation_test=ct, max_workers=8)
    assert _canon(one) == _canon(eight)


# --------------------------------------------------------------------------
# scored: correct per-variation pass@1 / pass@k / pass^k
# --------------------------------------------------------------------------

def test_scored_all_pass_per_variation():
    doc = _scenario()  # 2 locale x 2 rate x 2 noise x 1 behavior x 2 reps = 16
    ct = _conv_test([{"id": "asked-refund", "kind": "phrase",
                      "regex": "refund", "role": "caller"}])
    summary = SIM.run_matrix(doc, conversation_test=ct)
    assert summary["scored"] is True
    assert summary["reliability_basis"] == "agent_deterministic"
    # 2 x 2 x 2 x 1 = 8 cells, each with 2 repetitions
    assert len(summary["variation_cells"]) == 8
    for cell in summary["variation_cells"]:
        rel = cell["reliability"]
        assert cell["runs"] == 2
        assert rel["n"] == 2
        assert rel["pass_at_1"] == 1.0
        assert rel["pass_at_k"] == 1.0
        assert rel["pass_caret_k"] == 1.0  # deterministic caller: pass^k == pass@1
    # the whole-scenario aggregate is over all 16 valid runs
    assert summary["reliability"]["n"] == 16
    assert summary["reliability"]["pass_caret_k"] == 1.0
    assert summary["exit_code"] == 0


def test_scored_all_fail_per_variation():
    doc = _scenario()
    ct = _conv_test([{"id": "never", "kind": "phrase",
                      "regex": "zzz-not-in-any-transcript", "role": "caller"}])
    summary = SIM.run_matrix(doc, conversation_test=ct)
    for cell in summary["variation_cells"]:
        rel = cell["reliability"]
        assert rel["pass_at_1"] == 0.0
        assert rel["pass_at_k"] == 0.0
        assert rel["pass_caret_k"] == 0.0
    assert summary["reliability"]["pass_at_1"] == 0.0
    # a scored FAIL aggregate gates non-zero (report policy)
    assert summary["exit_code"] == 1


def test_scored_per_variation_matches_independent_recompute():
    # A cell whose repetitions have DIFFERENT seeds -> different backchannels ->
    # a count assertion on backchannel spans is genuinely MIXED within a cell, so
    # pass@k (>= 1 of k) and pass^k (all k) can differ. run_matrix's aggregate
    # must equal an independent recompute done straight from the renders.
    doc = _scenario(variation_matrix={
        "locale": ["en-US", "es-ES"], "repetitions": 6,
    })
    ct = _conv_test([{"id": "one-backchannel", "kind": "count",
                      "span_type": "backchannel", "count": {"min": 1}}])
    summary = SIM.run_matrix(doc, conversation_test=ct)

    # recompute expected pass booleans per cell straight from expand()+render()
    from collections import defaultdict
    expected = defaultdict(list)
    for run in SIM.expand(doc):
        produced = SIM.render(run["scenario"], run["seed"])
        n_bc = sum(1 for s in produced["trace"]["spans"]
                   if s["type"] == "backchannel")
        cell = (run["variation"]["locale"], run["variation"]["speaking_rate"],
                run["variation"]["noise"], run["variation"]["behavior"])
        expected[cell].append(n_bc >= 1)

    assert len(summary["variation_cells"]) == len(expected)
    for cell in summary["variation_cells"]:
        v = cell["cell"]
        key = (v["locale"], v["speaking_rate"], v["noise"], v["behavior"])
        want = expected[key]
        rel = cell["reliability"]
        assert rel["n"] == len(want)
        assert rel["pass_at_1"] == sum(want) / len(want)
        assert rel["pass_at_k"] == (1.0 if any(want) else 0.0)
        assert rel["pass_caret_k"] == (1.0 if all(want) else 0.0)
    # at least one cell actually exercises the pass@k != pass^k distinction
    mixed = [c for c in summary["variation_cells"]
             if c["reliability"]["pass_at_k"] != c["reliability"]["pass_caret_k"]]
    assert mixed, "expected at least one cell with mixed within-cell outcomes"


# --------------------------------------------------------------------------
# SIMULATOR_INVALID: its own bucket, excluded from agent pass/fail
# --------------------------------------------------------------------------

def _invalid_scenario():
    # facts declare order_id A-1001 but the script only ever says A-9999: EVERY
    # produced conversation violates the scenario's own ground-truth.
    return _scenario(
        facts={"order_id": "A-1001"},
        caller={"script": [{"say": "My order A-9999 is broken."}],
                "behavior": {"backchannels": {"probability": 0.0}}},
        variation_matrix={"locale": ["en-US", "es-ES"], "repetitions": 2},
    )


def test_simulator_invalid_is_bucketed_and_excluded_from_agent_pass_fail():
    doc = _invalid_scenario()
    ct = _conv_test([{"id": "asked-refund", "kind": "phrase",
                      "regex": "refund", "role": "caller"}])
    summary = SIM.run_matrix(doc, conversation_test=ct)

    total = len(SIM.expand(doc))
    assert summary["total"] == total
    assert summary["counts"]["simulator_invalid"] == total
    assert summary["counts"]["valid"] == 0
    assert summary["counts"]["scored"] == 0
    # every invalid run is in the SEPARATE bucket, attributable, with its reason
    assert len(summary["simulator_invalid"]) == total
    for r in summary["simulator_invalid"]:
        assert "A-1001" in r["reason"]
        assert "variation" in r and "seed" in r
    # ... and NONE of them was scored as an agent PASS/FAIL
    for rec in summary["runs"]:
        assert rec["valid"] is False
        assert "score" not in rec
        assert rec["simulation_status"] == SIM.SIMULATOR_INVALID
    # the agent reliability aggregate has n == 0 (invalid runs are excluded, not
    # counted as failures)
    assert summary["reliability"]["n"] == 0
    # a broken fixture gates non-zero, exactly as `hotato simulate` reports it
    assert summary["exit_code"] == 1


def test_simulator_invalid_without_test_still_bucketed():
    doc = _invalid_scenario()
    summary = SIM.run_matrix(doc)
    assert summary["scored"] is False
    assert summary["counts"]["simulator_invalid"] == len(summary["runs"])
    assert summary["reliability"]["n"] == 0
    assert summary["exit_code"] == 1


# --------------------------------------------------------------------------
# NO overall_score anywhere
# --------------------------------------------------------------------------

def test_no_overall_score_in_summary():
    doc = _scenario()
    ct = _conv_test([{"id": "asked-refund", "kind": "phrase",
                      "regex": "refund", "role": "caller"}])
    summary = SIM.run_matrix(doc, conversation_test=ct)
    assert "overall_score" not in json.dumps(summary)


# --------------------------------------------------------------------------
# CLI: --matrix produces verifiable artifacts + an attributable summary
# --------------------------------------------------------------------------

def test_cli_matrix_produces_verifiable_artifacts(tmp_path, capsys):
    s = _write(tmp_path, _scenario(variation_matrix={
        "locale": ["en-US", "es-ES"], "repetitions": 1}), "s.json")
    out = tmp_path / "matrix"
    code = cli.main(["simulate", "--matrix", s, "--out", str(out)])
    assert code == 0
    printed = capsys.readouterr().out
    # an ATTRIBUTABLE per-variation summary is printed (never blended)
    assert "per-variation reliability" in printed
    assert "refund-basic" in printed

    # each run wrote its own origin=simulated artifact `conversation verify` accepts
    run_dirs = sorted(p for p in out.iterdir() if p.is_dir())
    assert len(run_dirs) == 2
    for d in run_dirs:
        manifest = json.loads((d / "conversation.json").read_text(encoding="utf-8"))
        assert manifest["origin"]["kind"] == "simulated"
        vcode = cli.main(["conversation", "verify", str(d)])
        assert vcode == 0
        assert "VERIFIED" in capsys.readouterr().out


def test_cli_matrix_json_has_no_overall_score_and_scored_gate(tmp_path, capsys):
    s = _write(tmp_path, _scenario(), "s.json")
    ct = _write(tmp_path, _conv_test([
        {"id": "asked-refund", "kind": "phrase", "regex": "refund",
         "role": "caller"}]), "t.json")
    code = cli.main(["simulate", "--matrix", s, "--conversation-test", ct,
                     "--parallel", "4", "--format", "json"])
    assert code == 0
    raw = capsys.readouterr().out
    assert "overall_score" not in raw
    payload = json.loads(raw)
    assert payload["kind"] == "simulate-matrix"
    assert payload["scored"] is True
    assert payload["conversation_test_id"] == "refund-test"
    assert payload["all_simulated"] is True
    assert payload["counts"]["valid"] == 16


def test_cli_matrix_scored_fail_gates_nonzero(tmp_path, capsys):
    s = _write(tmp_path, _scenario(), "s.json")
    ct = _write(tmp_path, _conv_test([
        {"id": "never", "kind": "phrase", "regex": "zzz-absent",
         "role": "caller"}]), "t.json")
    code = cli.main(["simulate", "--matrix", s, "--conversation-test", ct])
    assert code == 1
    capsys.readouterr()


def test_cli_matrix_and_positional_conflict_is_usage_error(tmp_path):
    s = _write(tmp_path, _scenario(), "s.json")
    # positional + --matrix both given is a usage error (exit 2 via ValueError)
    code = cli.main(["simulate", s, "--matrix", s])
    assert code == 2
