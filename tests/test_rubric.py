"""``hotato.rubric`` -- the REAL model-judge lane (schema ``rubric.v1``).

Unit tests use a deterministic FAKE judge (canned responses, injected) so the
shipped model path is exercised without a live daemon in CI. ONE live-Ollama
integration test (``test_ollama_judge_live``) runs the REAL path end to end when
a daemon is reachable, and is skipped where it is not.

What is pinned here:
  * rubric.v1 result carries deterministic:false + full provenance, no overall_score;
  * the SEPARATE-schema invariant: assert.v1's kinds keep deterministic:const:true;
  * categorical parse + one repair retry; a second miss is an honest inconclusive;
  * a cache hit is byte-identical; --no-cache re-queries and DIFFS (drift);
  * missing/insufficient evidence -> INCONCLUSIVE (no model call); human_rubric
    is never model-scored; a judge-backend failure -> ERROR (advisory);
  * the report judge shelf populates with real counts and NEVER blends;
  * --gate flips the exit code; a rubric FAIL is advisory by default;
  * calibration produces a reproducible agreement artifact.
"""
import json
import os

import pytest

from hotato import rubric as R


# =========================================================================
# The deterministic FAKE judge (canned responses, injected) -- test double only
# =========================================================================

class FakeJudge(R.Judge):
    """Returns canned RAW model responses in sequence (repeating the last once
    exhausted), records call count, and reports a fixed digest. Never touches a
    network. The SHIPPED judge (OllamaJudge/HostedJudge) calls a real model."""

    provider = "fake"

    def __init__(self, responses, *, model="fake-judge-1b", digest="cafef00d",
                 raise_on_call=None):
        self.model = model
        self._responses = list(responses)
        self._digest = digest
        self.calls = 0
        self._raise = raise_on_call

    def complete(self, system, user):
        if self._raise is not None:
            raise self._raise
        r = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return r

    def model_digest(self):
        return self._digest


def _pass(rationale="looks good", turn=0, quote="thanks"):
    return json.dumps({"verdict": "pass", "rationale": rationale,
                       "citations": [{"turn": turn, "quote": quote}]})


def _fail(rationale="nope"):
    return json.dumps({"verdict": "fail", "rationale": rationale, "citations": []})


def _rubric(rid="polite", kind="judge_rubric", reps=1, conf=0.5, evidence=None,
            dimension="conversation"):
    r = {"id": rid, "kind": kind, "criterion": "was the agent polite?",
         "evidence": evidence or ["transcript"],
         "evaluation": {"repetitions": reps, "confidence_required": conf}}
    if dimension:
        r["dimension"] = dimension
    return r


_TX = [{"role": "caller", "text": "this is frustrating"},
       {"role": "agent", "text": "I am sorry, thank you for your patience"}]


# =========================================================================
# rubric.v1 result shape + provenance + no overall_score
# =========================================================================

def test_result_is_deterministic_false_with_full_provenance():
    res = R.evaluate_rubric(_rubric(), transcript=_TX, judge=FakeJudge([_pass()]))
    assert res["kind"] == "rubric"
    assert res["deterministic"] is False
    assert res["status"] == "PASS"
    assert "overall_score" not in json.dumps(res)
    j = res["judge"]
    for key in ("model", "model_digest", "provider", "prompt_id",
                "prompt_version", "prompt_sha256", "temperature", "input_sha256",
                "cache_key", "cached", "votes", "repetitions", "aggregation",
                "disagreement", "confidence", "confidence_required", "citations",
                "verdict_sha256"):
        assert key in j, f"missing provenance field {key!r}"
    assert j["temperature"] == 0
    assert j["model_digest"] == "cafef00d"
    assert j["cached"] is False
    assert j["cache_key"] == R._sha256_text(
        f"fake:{j['model']}\n{j['prompt_sha256']}\n{j['input_sha256']}")
    # a real citation to the exact transcript turn
    assert j["citations"] and j["citations"][0]["turn"] == 0


def test_no_overall_score_anywhere_in_envelope():
    env = R.evaluate_rubric_lane([_rubric()], transcript=_TX,
                                 judge=FakeJudge([_pass()]))
    assert env["schema"] == "rubric.v1"
    assert "overall_score" not in json.dumps(env)
    assert set(env["summary"]) >= {"pass", "fail", "inconclusive", "error", "note"}
    assert env["summary"]["pass"] == 1


# =========================================================================
# categorical parse + repair retry
# =========================================================================

def test_parse_verdict_categorical_and_synonyms():
    assert R.parse_verdict('{"verdict":"pass"}')["verdict"] == "pass"
    assert R.parse_verdict('{"verdict":"FAIL","rationale":"x"}')["verdict"] == "fail"
    # embedded in prose / code fence
    assert R.parse_verdict('```json\n{"verdict":"inconclusive"}\n```')["verdict"] == "inconclusive"
    # a synonym maps
    assert R.parse_verdict('{"verdict":"yes"}')["verdict"] == "pass"


def test_parse_miss_raises():
    with pytest.raises(R.ParseMiss):
        R.parse_verdict("I think the agent did fine, honestly.")
    with pytest.raises(R.ParseMiss):
        R.parse_verdict('{"verdict":"maybe"}')


def test_repair_retry_recovers_a_verdict():
    # first response is garbage, the repair response is valid -> parsed, PASS
    judge = FakeJudge(["not json at all", _pass()])
    res = R.evaluate_rubric(_rubric(), transcript=_TX, judge=judge)
    assert res["status"] == "PASS"
    assert judge.calls == 2  # one real call + one repair call


def test_two_misses_is_an_honest_inconclusive_not_a_guess():
    judge = FakeJudge(["garbage", "still garbage"])
    res = R.evaluate_rubric(_rubric(), transcript=_TX, judge=judge)
    assert res["status"] == "INCONCLUSIVE"
    assert res["judge"]["votes"] == ["inconclusive"]


# =========================================================================
# cache: byte-identical hit + --no-cache drift diff
# =========================================================================

def test_cache_hit_is_byte_identical_and_skips_the_model(tmp_path):
    cache = R.VerdictCache(str(tmp_path / "c"))
    rub = _rubric()
    j1 = FakeJudge([_pass("first")])
    r1 = R.evaluate_rubric(rub, transcript=_TX, judge=j1, cache=cache)
    assert r1["judge"]["cached"] is False and j1.calls == 1
    # a different judge would answer FAIL, but the cache hit must NOT call it
    j2 = FakeJudge([_fail("second")])
    r2 = R.evaluate_rubric(rub, transcript=_TX, judge=j2, cache=cache)
    assert j2.calls == 0
    assert r2["judge"]["cached"] is True
    assert r2["status"] == "PASS"
    assert r2["judge"]["verdict_sha256"] == r1["judge"]["verdict_sha256"]
    # byte-identical apart from the runtime cached flag
    a = json.loads(R._canonical(R._signable(r1)))
    b = json.loads(R._canonical(R._signable(r2)))
    assert a == b


def test_no_cache_requeries_and_surfaces_drift(tmp_path):
    cache = R.VerdictCache(str(tmp_path / "c"))
    rub = _rubric()
    R.evaluate_rubric(rub, transcript=_TX, judge=FakeJudge([_pass()]), cache=cache)
    # --no-cache re-queries; the model now disagrees -> drift is surfaced
    j = FakeJudge([_fail("changed my mind")])
    r = R.evaluate_rubric(rub, transcript=_TX, judge=j, cache=cache, no_cache=True)
    assert j.calls == 1
    assert r["judge"]["cached"] is False
    drift = r["judge"]["drift"]
    assert drift and drift["changed"] is True
    assert drift["cached_status"] == "PASS" and drift["fresh_status"] == "FAIL"
    assert drift["cached_verdict_sha256"] != drift["fresh_verdict_sha256"]


def test_no_cache_no_drift_when_verdict_matches(tmp_path):
    cache = R.VerdictCache(str(tmp_path / "c"))
    rub = _rubric()
    R.evaluate_rubric(rub, transcript=_TX, judge=FakeJudge([_pass()]), cache=cache)
    r = R.evaluate_rubric(rub, transcript=_TX, judge=FakeJudge([_pass()]),
                          cache=cache, no_cache=True)
    assert r["judge"].get("drift") is None  # identical verdict -> no drift


# =========================================================================
# missing evidence -> INCONCLUSIVE; human_rubric never model-scored; ERROR
# =========================================================================

def test_missing_evidence_is_inconclusive_without_a_model_call():
    judge = FakeJudge([_pass()])
    res = R.evaluate_rubric(_rubric(), transcript=None, judge=judge)
    assert res["status"] == "INCONCLUSIVE"
    assert judge.calls == 0
    assert "required evidence absent" in res["rationale"]


def test_tool_trace_evidence_required_but_absent_is_inconclusive():
    judge = FakeJudge([_pass()])
    rub = _rubric(evidence=["tool_trace"])
    res = R.evaluate_rubric(rub, transcript=_TX, trace=None, judge=judge)
    assert res["status"] == "INCONCLUSIVE"
    assert judge.calls == 0


def test_human_rubric_is_never_model_scored():
    judge = FakeJudge([_pass()])
    res = R.evaluate_rubric(_rubric(kind="human_rubric"), transcript=_TX, judge=judge)
    assert res["status"] == "INCONCLUSIVE"
    assert res["review"]["human_required"] is True
    assert judge.calls == 0


def test_judge_backend_failure_is_error_not_a_fake_verdict():
    judge = FakeJudge([], raise_on_call=R.JudgeError("endpoint unreachable"))
    res = R.evaluate_rubric(_rubric(), transcript=_TX, judge=judge)
    assert res["status"] == "ERROR"
    assert "unreachable" in res["rationale"]
    assert res["deterministic"] is False


# =========================================================================
# multi-run voting: disagreement -> INCONCLUSIVE (unanimous_or_inconclusive)
# =========================================================================

def test_disagreement_across_repetitions_is_inconclusive():
    judge = FakeJudge([_pass(), _fail(), _pass()])  # 2 pass / 1 fail -> not unanimous
    res = R.evaluate_rubric(_rubric(reps=3), transcript=_TX, judge=judge)
    assert res["status"] == "INCONCLUSIVE"
    assert res["judge"]["disagreement"] is True
    assert res["judge"]["votes"] == ["pass", "fail", "pass"]


def test_unanimous_repetitions_is_decisive():
    judge = FakeJudge([_pass(), _pass(), _pass()])
    res = R.evaluate_rubric(_rubric(reps=3), transcript=_TX, judge=judge)
    assert res["status"] == "PASS"
    assert res["judge"]["disagreement"] is False
    assert res["judge"]["confidence"] == 1.0


# =========================================================================
# --gate flips the exit code; advisory by default
# =========================================================================

def test_gate_flips_exit_code_on_fail():
    fail = R.evaluate_rubric(_rubric(), transcript=_TX, judge=FakeJudge([_fail()]))
    advisory = R.rubric_envelope([fail], gate=False)
    gated = R.rubric_envelope([fail], gate=True)
    assert advisory["exit_code"] == 0 and advisory["advisory"] is True
    assert gated["exit_code"] == 1 and gated["gated"] is True
    assert gated["summary"]["fail"] == 1


def test_inconclusive_and_error_never_gate_even_with_gate():
    inconc = R.evaluate_rubric(_rubric(), transcript=None, judge=FakeJudge([_pass()]))
    env = R.rubric_envelope([inconc], gate=True)
    assert env["exit_code"] == 0  # INCONCLUSIVE never gates by itself


# =========================================================================
# the report judge shelf populates with REAL counts and never blends
# =========================================================================

def test_report_shelf_populates_and_never_blends():
    from hotato import report, assert_ as A
    ctx = A.build_context(
        transcript=[{"role": "agent", "text": "thank you, so sorry"}], spans=[])
    assert_env = A.run_assertions(
        {"version": 1, "assertions": [
            {"id": "x", "kind": "phrase", "regex": "sorry", "role": "agent",
             "dimension": "policy"}]}, ctx)
    rub_env = R.evaluate_rubric_lane([_rubric()], transcript=ctx.transcript,
                                     judge=FakeJudge([_pass("apologized")]))
    html, _ = report.build_report_html(suite="barge-in", assertions=assert_env,
                                       rubric=rub_env)
    # populated shelf with real model id + provenance + rationale
    assert ">Model-assisted (advisory)<" in html
    assert "fake-judge-1b@cafef00d" in html
    assert "deterministic:false" in html
    assert "apologized" in html
    # headline shows the judge count SIDE BY SIDE, never merged
    assert "1 deterministic pass / 0 fail  1 judge-scored (advisory)" in html
    # assert.v1's own judge lane is untouched -- the two are never conflated
    assert assert_env["summary"]["judge"] == {"pass": 0, "fail": 0}
    # markdown mirror also populates
    md, _ = report.build_report_md(suite="barge-in", assertions=assert_env,
                                   rubric=rub_env)
    assert "### Model-assisted (advisory)" in md
    assert "fake-judge-1b@cafef00d" in md


def test_report_shelf_stays_empty_without_a_rubric_envelope():
    from hotato import report, assert_ as A
    ctx = A.build_context(transcript=[{"role": "agent", "text": "hi"}], spans=[])
    env = A.run_assertions(
        {"version": 1, "assertions": [
            {"id": "x", "kind": "phrase", "regex": "hi", "role": "agent"}]}, ctx)
    html, _ = report.build_report_html(suite="barge-in", assertions=env)
    assert "No judge-scored assertions in this build" in html
    assert "0 judge-scored (advisory)" in html


# =========================================================================
# HONESTY INVARIANT: assert.v1 stays a deterministic:const:true wall
# =========================================================================

def _schema(name):
    from importlib import resources
    return json.loads(resources.files("hotato").joinpath("schema", name).read_text())


def test_assert_v1_five_kinds_stay_deterministic_const_true():
    s = _schema("assert.v1.json")
    result = s["definitions"]["result"]
    assert result["properties"]["deterministic"] == {"const": True} or \
        result["properties"]["deterministic"]["const"] is True
    # the judge lane inside assert.v1 stays the {0,0} quarantine, structurally
    judge = s["properties"]["summary"]["properties"]["judge"]["properties"]
    assert judge["pass"]["const"] == 0 and judge["fail"]["const"] == 0
    # the five original kinds are still enumerated
    kinds = result["properties"]["kind"]["enum"]
    for k in ("phrase", "pii", "policy", "tool_call", "outcome"):
        assert k in kinds


def test_rubric_v1_result_is_structurally_deterministic_false():
    s = _schema("rubric.v1.json")
    rr = s["definitions"]["rubric_result"]["properties"]
    assert rr["deterministic"]["const"] is False
    assert rr["kind"]["const"] == "rubric"
    # no overall_score permitted anywhere in the schema's own objects
    assert s["properties"]["summary"]["properties"]["overall_score"] is False


def test_rubric_v1_result_validates_against_the_schema():
    jsonschema = pytest.importorskip("jsonschema")
    s = _schema("rubric.v1.json")
    env = R.evaluate_rubric_lane([_rubric()], transcript=_TX,
                                 judge=FakeJudge([_pass()]))
    jsonschema.validate(env, s)
    # a result WITHOUT optional fields (missing evidence -> minimal) still validates
    minimal = R.evaluate_rubric(_rubric(dimension=None), transcript=None,
                                judge=FakeJudge([_pass()]))
    jsonschema.validate({"schema": "rubric.v1", "exit_code": 0,
                         "results": [minimal],
                         "summary": {"pass": 0, "fail": 0, "inconclusive": 1,
                                     "error": 0, "note": "n"}}, s)


# =========================================================================
# calibration: a reproducible agreement artifact on a human-labeled set
# =========================================================================

def _write_labeled(dirpath, items):
    os.makedirs(dirpath, exist_ok=True)
    for i, it in enumerate(items):
        with open(os.path.join(dirpath, f"item{i}.json"), "w") as fh:
            json.dump(it, fh)


def test_calibration_is_reproducible_and_computes_agreement(tmp_path):
    labeled = str(tmp_path / "labeled")
    # three held-out items with human labels; the fake judge always says PASS
    items = [
        {"rubric": _rubric(), "transcript": _TX, "label": "pass", "split": "held_out"},
        {"rubric": _rubric(), "transcript": _TX, "label": "fail", "split": "held_out"},
        {"rubric": _rubric(), "transcript": None, "label": "inconclusive",
         "split": "held_out"},  # missing evidence -> model abstains
    ]
    _write_labeled(labeled, items)
    loaded = R.load_labeled_corpus(labeled)

    def _art():
        return R.calibrate(loaded, judge=FakeJudge([_pass()]), held_out_pct=100)

    a1 = _art()
    a2 = _art()
    # reproducible: no timestamp / RNG -> byte-identical artifact
    assert R._canonical(a1) == R._canonical(a2)
    c = a1["counts"]
    assert c["held_out"] == 3
    # model says pass on 2 (agrees with the 1 pass-labeled), abstains on the
    # missing-evidence one; disagrees with the fail-labeled one.
    assert a1["counts"]["held_out_agree"] == 1        # only the pass-labeled agrees
    # selective accuracy: over ANSWERED items (2 pass-verdicts), 1 agrees
    assert a1["counts"]["held_out_answered"] == 2
    assert abs(a1["selective_accuracy"] - 0.5) < 1e-9
    assert a1["agreement"] is not None
    assert "overall_score" not in json.dumps(a1)


# =========================================================================
# egress is opt-in: a non-local Ollama endpoint / hosted judge is refused
# =========================================================================

def test_non_local_ollama_endpoint_requires_egress_opt_in():
    with pytest.raises(R.EgressRefused):
        R.OllamaJudge(endpoint="http://remote.example.com:11434")
    # localhost never needs the flag
    j = R.OllamaJudge(endpoint="http://localhost:11434")
    assert j.provider == "ollama"


def test_hosted_judge_refused_without_opt_in():
    with pytest.raises(R.EgressRefused):
        R.HostedJudge(model="gpt-4o-mini", endpoint="https://api.example.com/v1")
    j = R.HostedJudge(model="gpt-4o-mini", endpoint="https://api.example.com/v1",
                      egress_opt_in=True)
    assert j.provider == "hosted"


# =========================================================================
# THE LIVE PATH: a REAL local Ollama call end to end (skipped if unreachable)
# =========================================================================

def _ollama_up():
    try:
        R.OllamaJudge(model=R.DEFAULT_JUDGE_MODEL)._http_json("/api/tags", None, "GET")
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _ollama_up(),
                    reason="no local Ollama daemon at http://localhost:11434")
def test_ollama_judge_live(tmp_path):
    """REAL end-to-end: a rubric scored by a live local qwen2.5vl:3b, proving
    the shipped path works -- a valid categorical verdict + full provenance
    (model digest present, deterministic:false), plus a byte-identical cache
    replay."""
    judge = R.OllamaJudge(model=R.DEFAULT_JUDGE_MODEL)
    cache = R.VerdictCache(str(tmp_path / "cache"))
    rub = {
        "id": "acknowledged-frustration", "kind": "judge_rubric",
        "dimension": "conversation",
        "criterion": "Did the agent acknowledge the caller's frustration before "
                     "proposing a fix?",
        "evidence": ["transcript"],
        "evaluation": {"repetitions": 1, "confidence_required": 0.5},
    }
    transcript = [
        {"role": "caller", "text": "I am really upset, my order arrived broken "
                                   "again and this is the second time!"},
        {"role": "agent", "text": "I completely understand your frustration and "
                                  "I am sorry this happened again. Let me make it "
                                  "right and issue a full refund right now."},
    ]
    res = R.evaluate_rubric(rub, transcript=transcript, judge=judge, cache=cache)

    # a REAL categorical verdict
    assert res["status"] in ("PASS", "FAIL", "INCONCLUSIVE")
    assert res["deterministic"] is False
    j = res["judge"]
    # provenance from the real backend
    assert j["provider"] == "ollama"
    assert j["model"] == R.DEFAULT_JUDGE_MODEL
    assert j["model_digest"], "the live model digest must be recorded"
    assert j["temperature"] == 0
    assert j["prompt_id"] == R.PROMPT_ID
    assert j["votes"] and j["votes"][0] in R.CATEGORICAL_VALUES
    assert j["cached"] is False
    assert res["rationale"]  # a real rationale
    # cache replay is byte-identical (the exact reproducibility claim)
    again = R.evaluate_rubric(rub, transcript=transcript, judge=judge, cache=cache)
    assert again["judge"]["cached"] is True
    assert again["judge"]["verdict_sha256"] == j["verdict_sha256"]
    print(f"\nLIVE verdict: {res['status']} -- {res['rationale']}")


def test_judge_http_paths_install_hardened_opener(monkeypatch):
    """Both judge HTTP paths must install the credential-safe opener BEFORE any
    request (audit finding #1): a cross-host redirect from a judge endpoint must
    never carry Authorization/Cookie to another host, and every redirect target
    must re-pass the SSRF guard. Regression-pins the _ensure_safe_opener call."""
    import hotato.capture as capture
    import hotato.rubric as rubric
    calls = []
    monkeypatch.setattr(capture, "_ensure_safe_opener",
                        lambda: calls.append("installed"))

    class _Boom(Exception):
        pass

    def _no_net(*a, **k):
        raise rubric.JudgeError("stop before network")
    j = rubric.OllamaJudge(model="qwen2.5vl:3b")
    try:
        # urlopen will fail fast (nothing listening is fine too) -- the point is
        # the opener install happens first, which we capture via the monkeypatch.
        j._http_json("/api/tags", None, method="GET")
    except Exception:
        pass
    assert "installed" in calls, "OllamaJudge must install the safe opener"
    calls.clear()
    # egress_opt_in=True: the constructor gate is honest and refuses otherwise
    # (tested elsewhere); this test never reaches the network -- example.invalid
    # fails fast AFTER the opener install we are pinning.
    h = rubric.HostedJudge(model="m", endpoint="https://example.invalid",
                           egress_opt_in=True)
    try:
        h.complete("s", "u")
    except Exception:
        pass
    assert "installed" in calls, "HostedJudge must install the safe opener"


def test_shared_transport_installs_opener_before_request(monkeypatch):
    """Finding #8 moved the opener install into the shared ``_urllib_json_call``
    transport. Pin, at the consolidation point itself, that the hardened opener
    (finding #1) is installed BEFORE any request is issued -- so neither judge
    path can regress the credential-safe redirect protection."""
    import urllib.error
    import urllib.request

    import hotato.capture as capture
    import hotato.rubric as rubric

    order = []
    monkeypatch.setattr(capture, "_ensure_safe_opener",
                        lambda: order.append("opener"))

    def _fake_urlopen(req, timeout=None):
        order.append("urlopen")
        raise urllib.error.URLError("no network in test")

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    with pytest.raises(rubric.JudgeError):
        rubric._urllib_json_call(
            "http://localhost:11434/api/tags", data=None, headers={},
            method="GET", timeout=1.0,
            unreachable_subject="ollama endpoint", failed_subject="ollama",
        )
    # opener MUST be installed before the request is ever issued
    assert order == ["opener", "urlopen"]
