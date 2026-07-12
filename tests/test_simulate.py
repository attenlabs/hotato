"""Phase-2 2.1: the deterministic scripted-caller simulator.

Pins the honesty invariants that are the point of the slice:

* a SEEDED REPLAY is byte-identical (render + run_scripted content-hash equal for
  a fixed (scenario, seed); two runs identical) and different seeds differ ONLY
  where the scenario allows it (probabilistic backchannels), never in the
  scripted turns;
* origin.kind == "simulated" on EVERY produced conversation (render + manifest),
  never 'real' and never merged into a real bucket;
* a bad simulation is SIMULATOR_INVALID, NEVER an agent PASS/FAIL;
* expand() is fully deterministic (count + per-run seeds; two expansions match);
* reliability reports pass@1 / pass@k / pass^k correctly (all-pass + some-fail),
  with pass^k == pass@1 for the deterministic caller;
* a simulated conversation scored by the existing assert layer stays labelled
  simulated with no overall_score;
* the `hotato simulate` CLI produces an artifact `hotato conversation verify`
  accepts.
"""

import json

import pytest

from hotato import assert_ as A
from hotato import cli
from hotato import conversation as CV
from hotato import scenario as SC
from hotato import simulate as SIM


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


def _write_scenario(tmp_path, doc, name="s.json"):
    p = tmp_path / name
    p.write_text(json.dumps(doc), encoding="utf-8")
    return str(p)


# --------------------------------------------------------------------------
# byte-stability: a seeded replay is byte-identical
# --------------------------------------------------------------------------

def test_render_byte_stable_for_fixed_seed():
    doc = _scenario()
    a = SIM.render(doc, 11)
    b = SIM.render(doc, 11)
    assert a["content_hash"] == b["content_hash"]
    # the full transcript + trace are identical, not just the hash
    assert a["transcript"] == b["transcript"]
    assert a["trace"] == b["trace"]


def test_run_scripted_writes_byte_identical_transcript_for_fixed_seed(tmp_path):
    doc = _scenario()
    m1 = SIM.run_scripted(doc, 5, out_dir=str(tmp_path / "one"),
                          created_at="2026-07-12T00:00:00Z")
    m2 = SIM.run_scripted(doc, 5, out_dir=str(tmp_path / "two"),
                          created_at="2026-07-12T00:00:00Z")
    t1 = (tmp_path / "one" / "transcript.json").read_bytes()
    t2 = (tmp_path / "two" / "transcript.json").read_bytes()
    assert t1 == t2
    # the transcript sha256 bound in the manifest IS the render content_hash
    assert m1["artifacts"]["transcript"]["sha256"] == \
        m2["artifacts"]["transcript"]["sha256"]
    assert m1["artifacts"]["transcript"]["sha256"] == SIM.render(doc, 5)["content_hash"]


def test_different_seeds_differ_only_where_scenario_allows():
    doc = _scenario()  # backchannel probability 0.5 -> seed-dependent
    a = SIM.render(doc, 1)
    b = SIM.render(doc, 2)

    def scripted(r):
        return [s for s in r["transcript"]["segments"] if s["kind"] == "scripted"]

    # the scripted turns (the allowed-invariant part) are identical across seeds
    assert scripted(a) == scripted(b)
    # ... but the whole transcript differs somewhere (the backchannels the
    # scenario's behavior explicitly allows to vary)
    assert a["transcript"] != b["transcript"]
    bc_a = [s for s in a["transcript"]["segments"] if s["kind"] == "backchannel"]
    bc_b = [s for s in b["transcript"]["segments"] if s["kind"] == "backchannel"]
    assert bc_a != bc_b


def test_probability_zero_is_seed_invariant():
    # with no probabilistic behavior, EVERY seed produces the same bytes
    doc = _scenario()
    doc["caller"]["behavior"]["backchannels"] = {"probability": 0.0}
    assert SIM.render(doc, 1)["content_hash"] == SIM.render(doc, 999)["content_hash"]


# --------------------------------------------------------------------------
# origin.kind == simulated EVERYWHERE (never real, never a real bucket)
# --------------------------------------------------------------------------

def test_render_origin_is_simulated():
    r = SIM.render(_scenario(), 3)
    assert r["origin"]["kind"] == "simulated"
    sim = r["origin"]["simulator"]
    assert sim == {"model_id": "scripted", "scenario_id": "refund-basic", "seed": 3}


def test_manifest_origin_is_simulated(tmp_path):
    m = SIM.run_scripted(_scenario(), 3, out_dir=str(tmp_path),
                         created_at="2026-07-12T00:00:00Z")
    assert m["origin"]["kind"] == "simulated"
    assert m["origin"]["simulator"]["model_id"] == "scripted"
    # the manifest structurally validates as a conversation.v1
    CV.validate_conversation_doc(m)


def test_write_artifact_refuses_non_simulated_origin(tmp_path):
    r = SIM.render(_scenario(), 3)
    r = dict(r)
    r["origin"] = {"kind": "real"}  # tamper: try to mint a real conversation
    with pytest.raises(ValueError, match="simulated"):
        SIM.write_artifact(r, str(tmp_path), created_at="2026-07-12T00:00:00Z")


# --------------------------------------------------------------------------
# SIMULATOR_INVALID (never an agent PASS/FAIL)
# --------------------------------------------------------------------------

def test_faithful_simulation_validates_ok():
    doc = _scenario()
    v = SIM.validate_simulation(doc, SIM.render(doc, 4))
    assert v["ok"] is True and v["status"] == "ok"
    assert v["checks"]["caller_only"] and v["checks"]["perturbation_applied"]


def test_script_that_violates_its_facts_is_simulator_invalid():
    # facts declare order_id A-1001 but the script only ever says A-9999: the
    # produced conversation violates the scenario's own ground-truth.
    doc = _scenario(
        facts={"order_id": "A-1001"},
        caller={"script": [{"say": "My order A-9999 is broken."}],
                "behavior": {"backchannels": {"probability": 0.0}}},
    )
    v = SIM.validate_simulation(doc, SIM.render(doc, 1))
    assert v["ok"] is False
    assert v["status"] == SIM.SIMULATOR_INVALID
    assert "A-1001" in v["reason"]


def test_agent_turn_is_simulator_invalid():
    # a rendering that put words in the agent's mouth (solved the task for it)
    # is SIMULATOR_INVALID, never an agent PASS/FAIL
    doc = _scenario()
    r = SIM.render(doc, 1)
    r = json.loads(json.dumps(r))
    r["transcript"]["segments"].append(
        {"role": "agent", "text": "Your refund is issued.", "start": 99.0,
         "end": 100.0, "kind": "scripted"})
    v = SIM.validate_simulation(doc, r)
    assert v["ok"] is False and v["status"] == SIM.SIMULATOR_INVALID
    assert "agent" in v["reason"]


def test_missing_declared_interruption_is_simulator_invalid():
    doc = _scenario()
    r = SIM.render(doc, 1)
    r = json.loads(json.dumps(r))
    r["trace"]["spans"] = [s for s in r["trace"]["spans"]
                           if s["type"] != "caller_barge_in"]
    v = SIM.validate_simulation(doc, r)
    assert v["ok"] is False and v["status"] == SIM.SIMULATOR_INVALID
    assert "perturbation" in v["reason"]


def test_backchannel_when_probability_zero_is_simulator_invalid():
    doc = _scenario()
    doc["caller"]["behavior"]["backchannels"] = {"probability": 0.0}
    r = SIM.render(doc, 1)  # renders zero backchannels honestly
    r = json.loads(json.dumps(r))
    # inject a backchannel the behavior forbids
    r["trace"]["spans"].append({"type": "backchannel", "start_sec": 1.0, "end_sec": 1.3})
    v = SIM.validate_simulation(doc, r)
    assert v["ok"] is False and v["status"] == SIM.SIMULATOR_INVALID


# --------------------------------------------------------------------------
# expand(): deterministic count + per-run seeds
# --------------------------------------------------------------------------

def test_expand_count_matches_matrix_cross_product():
    doc = _scenario()  # 2 locale x 2 rate x 2 noise x 1 behavior x 2 reps = 16
    runs = SIM.expand(doc)
    assert len(runs) == 2 * 2 * 2 * 1 * 2
    assert all(r["scenario_id"] == "refund-basic" for r in runs)


def test_expand_is_deterministic():
    doc = _scenario()
    a = SIM.expand(doc)
    b = SIM.expand(doc)
    assert [r["seed"] for r in a] == [r["seed"] for r in b]
    assert [r["variation"] for r in a] == [r["variation"] for r in b]
    # no two variation cells collide by construction (distinct tuples)
    assert len({tuple(sorted(r["variation"].items())) for r in a}) == len(a)


def test_expand_matrix_less_scenario_yields_one_run():
    doc = {
        "kind": "hotato.scenario", "version": 1, "id": "single",
        "goal": {"type": "ask", "target": "hours"},
        "caller": {"script": [{"say": "When do you open?"}]},
    }
    runs = SIM.expand(doc)
    assert len(runs) == 1


def test_expand_base_seed_shifts_derived_seeds():
    doc = _scenario()
    s0 = [r["seed"] for r in SIM.expand({**doc, "seed": 0})]
    s1 = [r["seed"] for r in SIM.expand({**doc, "seed": 1})]
    assert s0 != s1  # the base seed folds into every per-run seed


# --------------------------------------------------------------------------
# reliability: pass@1 / pass@k / pass^k
# --------------------------------------------------------------------------

def test_reliability_all_pass():
    rel = SIM.reliability([True, True, True, True])
    assert rel["pass_at_1"] == 1.0
    assert rel["pass_at_k"] == 1.0
    assert rel["pass_caret_k"] == 1.0
    # for the deterministic caller pass^k == pass@1 (honest, not fabricated)
    assert rel["pass_caret_k"] == rel["pass_at_1"]
    assert rel["n"] == 4 and rel["passes"] == 4


def test_reliability_some_fail():
    rel = SIM.reliability([True, True, False, True])
    assert rel["pass_at_1"] == pytest.approx(0.75)
    assert rel["pass_at_k"] == 1.0  # at least one passed
    assert rel["pass_caret_k"] == 0.0  # not all passed
    assert rel["ci"]["low"] <= 0.75 <= rel["ci"]["high"]


def test_reliability_all_fail_and_empty():
    rel = SIM.reliability([False, False])
    assert rel["pass_at_1"] == 0.0 and rel["pass_at_k"] == 0.0
    assert rel["pass_caret_k"] == 0.0
    empty = SIM.reliability([])
    assert empty["n"] == 0 and empty["ci"] is None


def test_reliability_accepts_verdict_dicts():
    doc = _scenario()
    runs = SIM.expand(doc)[:3]
    verdicts = [SIM.validate_simulation(r["scenario"], SIM.render(r["scenario"], r["seed"]))
                for r in runs]
    rel = SIM.reliability(verdicts)
    # every scripted run is a faithful (valid) simulation -> pass^k == pass@1 == 1
    assert rel["pass_caret_k"] == 1.0 == rel["pass_at_1"]


# --------------------------------------------------------------------------
# scored by the existing assert layer: stays simulated, no overall_score
# --------------------------------------------------------------------------

def test_simulated_conversation_scored_by_assert_layer_stays_simulated():
    doc = _scenario()
    r = SIM.render(doc, 7)
    ctx = A.build_context(transcript=r["transcript"]["segments"],
                          spans=r["trace"]["spans"])
    env = A.run_assertions(
        {"version": 1,
         "assertions": [{"id": "asked-refund", "kind": "phrase",
                         "regex": "refund", "role": "caller"}],
         "inconclusive_policy": "report"},
        ctx, inconclusive_policy="report")
    assert env["results"][0]["status"] == "PASS"
    # the artifact stays labelled simulated, and NOTHING blended it into a score
    assert r["origin"]["kind"] == "simulated"
    assert "overall_score" not in json.dumps(env)


# --------------------------------------------------------------------------
# CLI: `hotato simulate` produces an artifact `conversation verify` accepts
# --------------------------------------------------------------------------

def test_cli_simulate_produces_verifiable_artifact(tmp_path, capsys):
    s = _write_scenario(tmp_path, _scenario(variation_matrix={}))  # single run
    out = tmp_path / "sim"
    code = cli.main(["simulate", s, "--out", str(out),
                     "--created-at", "2026-07-12T00:00:00Z"])
    assert code == 0
    capsys.readouterr()
    # the produced manifest is origin=simulated
    manifest = json.loads((out / "conversation.json").read_text(encoding="utf-8"))
    assert manifest["origin"]["kind"] == "simulated"
    # ... and `hotato conversation verify` accepts it (digests re-hash cleanly)
    code = cli.main(["conversation", "verify", str(out)])
    assert code == 0
    assert "VERIFIED" in capsys.readouterr().out


def test_cli_simulate_json_reports_reliability_and_all_simulated(tmp_path, capsys):
    s = _write_scenario(tmp_path, _scenario())
    code = cli.main(["simulate", s, "--repetitions", "3", "--format", "json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "simulate"
    assert payload["all_simulated"] is True
    assert payload["invalid_count"] == 0
    assert every_run_simulated(payload)
    # deterministic caller: pass^k == pass@1
    assert payload["reliability"]["pass_caret_k"] == payload["reliability"]["pass_at_1"]
    # no overall_score anywhere in the machine surface
    assert "overall_score" not in json.dumps(payload)


def every_run_simulated(payload):
    return all(r["origin_kind"] == "simulated" for r in payload["runs"])


def test_cli_simulate_malformed_scenario_is_exit_2(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"kind": "hotato.scenario", "version": 1}),
                 encoding="utf-8")
    assert cli.main(["simulate", str(p)]) == 2
