"""Custom metric plugins (``hotato.metrics``): teams' own checks as plain Python
functions, reported like the built-in ones.

Pinned here:

* discovery: ``metric_*`` functions in ``hotato_metrics/*.py`` (and
  ``--metrics PATH``, and ``hotato.metrics`` entry points), with the
  ``@metric`` decorator setting labels; a broken plugin is exit 2;
* the deterministic lane: ``kind: custom`` results are ``assert.v1`` results
  (schema-valid, ``deterministic: true``) with a ``custom`` block naming the
  plugin, and they gate exactly like built-in kinds;
* hotato's rules, enforced rather than trusted: a pass needs evidence that
  matches the call; missing inputs give INCONCLUSIVE (exit 2 under refuse);
  a metric that answers differently twice is refused (exit 2); a judged
  metric never counts in the deterministic lane;
* the judged lane: a judged metric becomes a ``rubric.v1`` result
  (``deterministic: false``) with the plugin as the declared judge;
* reports label custom checks (HTML and Markdown), and the bundled example
  under ``examples/custom-metrics`` runs green.
"""

from __future__ import annotations

import json
import os
import textwrap
from importlib import resources
from pathlib import Path

import pytest

from hotato import assert_ as A
from hotato import cli
from hotato import metrics as M
from hotato import rubric as R

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "examples" / "custom-metrics"

TRANSCRIPT = [
    {"role": "agent", "text": "Thanks for calling Acme Clinic, how can I help?"},
    {"role": "caller", "text": "I want to book a cleaning"},
    {"role": "agent", "text": "You're booked for Tuesday at nine."},
]
SPANS = [{"type": "tool_call", "name": "book_appointment", "arguments": {}}]


@pytest.fixture(autouse=True)
def _clean_registry():
    M.clear_registry()
    yield
    M.clear_registry()


def _schema(name):
    return json.loads(resources.files("hotato").joinpath("schema", name).read_text("utf-8"))


def _plugin(tmp_path, body, name="checks.py", folder="hotato_metrics"):
    d = tmp_path / folder
    d.mkdir(exist_ok=True)
    (d / name).write_text(textwrap.dedent(body), encoding="utf-8")
    return d


GREET = """
    from hotato.metrics import metric, passed, failed, unknown

    @metric(needs=["transcript"], dimension="conversation")
    def metric_greets(call):
        '''First agent turn names the business.'''
        name = call.params.get("name", "Acme")
        turns = call.turns(role="agent")
        i, t = turns[0]
        if name.lower() in t["text"].lower():
            return passed([call.cite_turn(i, f"for calling {name}")], reason="named")
        return failed("not named", evidence=[call.cite_turn(i)])
"""


def _doc(*assertions, policy=None):
    doc = {"version": 1, "assertions": list(assertions)}
    if policy:
        doc["inconclusive_policy"] = policy
    return doc


def _custom(aid, metric, **extra):
    return {"id": aid, "kind": "custom", "metric": metric, **extra}


# --- discovery ---------------------------------------------------------------

def test_discovers_metric_functions_and_decorator_labels(tmp_path):
    folder = _plugin(tmp_path, GREET + """
    def metric_plain(call):
        return passed([call.cite_turn(0)])

    def helper_not_a_metric(call):
        return None
    """)
    assert M.load_plugins([str(folder)], default_dir=False, entry_points=False) == 2
    reg = M.registry()
    assert sorted(reg) == ["greets", "plain"]
    g = reg["greets"]
    assert g.lane == "deterministic" and g.needs == ("transcript",)
    assert g.dimension == "conversation"
    assert g.description == "First agent turn names the business."
    assert g.source.endswith("checks.py") and not os.path.isabs(g.source)
    assert len(g.source_sha256) == 64


def test_default_folder_is_found_only_when_autoload_runs(tmp_path, monkeypatch):
    _plugin(tmp_path, GREET)
    monkeypatch.chdir(tmp_path)
    M.configure_autoload()
    assert M.registry() == {}              # nothing imported yet
    assert M.get_metric("greets").name == "greets"


def test_entry_point_plugins_are_loaded(tmp_path, monkeypatch):
    mod = tmp_path / "acme_metrics_ep.py"
    mod.write_text(textwrap.dedent(GREET), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    class EP:
        name = "acme"
        value = "acme_metrics_ep"

        def load(self):
            import importlib
            return importlib.import_module("acme_metrics_ep")

    monkeypatch.setattr(M, "_entry_points", lambda: [EP()])
    M.load_plugins(default_dir=False)
    assert M.registry()["greets"].source == "entry-point:acme"


@pytest.mark.parametrize("body,needle", [
    ("raise RuntimeError('boom')\n", "failed to import"),
    ("from hotato.metrics import metric\n@metric(needs=['audio'])\n"
     "def metric_x(call):\n    return None\n", "'needs'"),
    ("from hotato.metrics import metric\n@metric(judged=True)\n"
     "def metric_x(call):\n    return None\n", "pinned model"),
    ("from hotato.metrics import metric\n@metric(judged=True, model='m:7b')\n"
     "def metric_x(call):\n    return None\n", "temperature=0"),
    ("from hotato.metrics import metric\n@metric(model='m:7b')\n"
     "def metric_x(call):\n    return None\n", "judged=True"),
    ("def metric_x(a, b, c):\n    return None\n", "(call) or (call, params)"),
])
def test_malformed_plugins_are_refused(tmp_path, body, needle):
    folder = _plugin(tmp_path, body)
    with pytest.raises(M.MetricError) as exc:
        M.load_plugins([str(folder)], default_dir=False, entry_points=False)
    assert needle in str(exc.value)


def test_duplicate_metric_names_are_refused(tmp_path):
    _plugin(tmp_path, GREET, name="a.py")
    folder = _plugin(tmp_path, GREET, name="b.py")
    with pytest.raises(M.MetricError, match="defined twice"):
        M.load_plugins([str(folder)], default_dir=False, entry_points=False)


def test_unknown_metric_is_a_validation_error_before_anything_runs(tmp_path):
    with pytest.raises(ValueError, match="unknown custom metric 'nope'"):
        A.validate_assertions_doc(_doc(_custom("a", "nope")))
    with pytest.raises(ValueError, match="'metric' is required"):
        A.validate_assertions_doc(_doc({"id": "a", "kind": "custom"}))


# --- the deterministic lane --------------------------------------------------

def _load(tmp_path, body=GREET):
    M.load_plugins([str(_plugin(tmp_path, body))], default_dir=False, entry_points=False)


def test_pass_and_fail_are_assert_v1_results_with_a_custom_block(tmp_path):
    import jsonschema

    _load(tmp_path)
    ctx = A.build_context(transcript=TRANSCRIPT)
    env = A.run_assertions(_doc(
        _custom("named", "greets", params={"name": "Acme"}),
        _custom("wrong", "greets", params={"name": "Globex"}),
    ), ctx)
    jsonschema.validate(env, _schema("assert.v1.json"))
    named, wrong = env["results"]
    assert named["status"] == "PASS" and named["deterministic"] is True
    assert named["dimension"] == "conversation"          # from the decorator
    assert named["custom"]["metric"] == "greets"
    assert named["custom"]["lane"] == "deterministic"
    assert named["custom"]["evidence"] == [{"turn": 0, "quote": "for calling Acme"}]
    assert wrong["status"] == "FAIL"
    assert wrong["public_reason"] == "The custom metric greets was not satisfied."
    assert env["exit_code"] == 1
    assert env["summary"]["deterministic"] == {"pass": 1, "fail": 1, "inconclusive": 0}
    assert env["summary"]["judge"] == {"pass": 0, "fail": 0}
    assert "overall_score" not in json.dumps(env)


def test_custom_results_compose_with_formula_and_when(tmp_path):
    _load(tmp_path)
    ctx = A.build_context(transcript=TRANSCRIPT)
    env = A.run_assertions(_doc(
        _custom("named", "greets"),
        {"id": "booked", "kind": "phrase", "regex": "booked", "role": "agent",
         "when": "named"},
        {"id": "both", "kind": "formula", "expr": "named and booked"},
    ), ctx)
    assert [r["status"] for r in env["results"]] == ["PASS", "PASS", "PASS"]


def test_pass_without_evidence_is_inconclusive(tmp_path):
    _load(tmp_path, """
    from hotato.metrics import passed
    def metric_bare(call):
        return passed([])
    """)
    r = A.run_assertions(_doc(_custom("a", "bare")), A.build_context(transcript=TRANSCRIPT))["results"][0]
    assert r["status"] == "INCONCLUSIVE"
    assert r["custom"]["claimed_status"] == "PASS"
    assert "without evidence" in r["reason"]


@pytest.mark.parametrize("cite,needle", [
    ("call.cite_turn(9)", "does not exist"),
    ("call.cite_turn(0, 'not in that turn')", "does not occur"),
    ("call.cite_span(0)", "no trace was supplied"),
    ("{'turn': 0, 'nonsense': 1} if False else {'note': 'trust me'}", "must cite"),
])
def test_evidence_that_does_not_match_the_call_is_never_a_verdict(tmp_path, cite, needle):
    _load(tmp_path, f"""
    from hotato.metrics import passed
    def metric_liar(call):
        return passed([{cite}])
    """)
    r = A.run_assertions(_doc(_custom("a", "liar")), A.build_context(transcript=TRANSCRIPT))["results"][0]
    assert r["status"] == "INCONCLUSIVE"
    assert needle in r["reason"]
    assert r["custom"]["evidence"] == []


@pytest.mark.parametrize("item,needle", [
    ({"turn": 0, "span_id": "s_999"}, "must cite one thing"),
    ({"turn": 0, "event": 0, "field": "x"}, "must cite one thing"),
    ({"turn": 0, "note": "trust me"}, "unknown key"),
    ({"span_id": "s_0", "quote": "Thanks for calling"}, "unknown key"),
])
def test_every_key_of_a_citation_is_accounted_for(tmp_path, item, needle):
    _load(tmp_path, f"""
    from hotato.metrics import passed
    def metric_mixed(call):
        return passed([{item!r}])
    """)
    ctx = A.build_context(transcript=TRANSCRIPT, spans=SPANS)
    r = A.run_assertions(_doc(_custom("a", "mixed")), ctx)["results"][0]
    assert r["status"] == "INCONCLUSIVE"
    assert needle in r["reason"]
    assert r["custom"]["evidence"] == []


@pytest.mark.parametrize("quote,status", [
    ("o", "INCONCLUSIVE"),
    ("Acme Clinic", "INCONCLUSIVE"),
    ("calling Acme Clinic", "PASS"),
])
def test_a_pass_needs_the_same_three_word_quote_as_test_run(tmp_path, quote, status):
    from hotato.sim_test import MIN_PASS_QUOTE_WORDS

    assert MIN_PASS_QUOTE_WORDS == 3
    _load(tmp_path, f"""
    from hotato.metrics import passed
    def metric_short(call):
        return passed([call.cite_turn(0, {quote!r})])
    """)
    r = A.run_assertions(_doc(_custom("a", "short")), A.build_context(transcript=TRANSCRIPT))["results"][0]
    assert r["status"] == status
    if status == "INCONCLUSIVE":
        assert "too short to support a pass" in r["reason"]


def test_a_short_quote_still_backs_a_fail(tmp_path):
    _load(tmp_path, """
    from hotato.metrics import failed
    def metric_no(call):
        return failed("said no", evidence=[call.cite_turn(0, "Acme")])
    """)
    r = A.run_assertions(_doc(_custom("a", "no")), A.build_context(transcript=TRANSCRIPT))["results"][0]
    assert r["status"] == "FAIL"


def test_a_downgraded_verdict_is_warned_about_on_stderr(tmp_path, capsys):
    _load(tmp_path, """
    from hotato.metrics import failed, passed
    def metric_bad_fail(call):
        return failed("boom", evidence=[call.cite_turn(9)])
    def metric_good(call):
        return passed([call.cite_turn(0, "for calling Acme")])
    """)
    env = A.run_assertions(_doc(_custom("a", "bad_fail"), _custom("b", "good")),
                           A.build_context(transcript=TRANSCRIPT))
    assert [r["status"] for r in env["results"]] == ["INCONCLUSIVE", "PASS"]
    assert env["results"][0]["custom"]["claimed_status"] == "FAIL"
    assert env["exit_code"] == 0
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1
    assert "custom metric bad_fail returned FAIL, reported INCONCLUSIVE" in err[0]
    assert "turn 9" in err[0]


def test_span_and_timing_citations_are_checked(tmp_path):
    _load(tmp_path, """
    from hotato.metrics import metric, passed
    @metric(needs=["spans", "timing"])
    def metric_fast_tool(call):
        (i, _), = call.spans_of("tool_call", name="book_appointment")
        return passed([call.cite_span(i),
                       call.cite_timing(0, "verdict.did_yield", True)])
    """)
    ctx = A.build_context(spans=SPANS, timing=[{"verdict": {"did_yield": True}}])
    r = A.run_assertions(_doc(_custom("a", "fast_tool")), ctx)["results"][0]
    assert r["status"] == "PASS"
    assert r["custom"]["evidence"] == [
        {"span_id": "s_0"}, {"event": 0, "field": "verdict.did_yield", "value": True}]
    ctx = A.build_context(spans=SPANS, timing=[{"verdict": {"did_yield": False}}])
    r = A.run_assertions(_doc(_custom("a", "fast_tool")), ctx)["results"][0]
    assert r["status"] == "INCONCLUSIVE" and "cited value" in r["reason"]


def test_missing_input_is_inconclusive_and_refuse_exits_2(tmp_path):
    _load(tmp_path)
    called = []
    M.registry()["greets"].func.__globals__["_seen"] = called
    ctx = A.build_context(spans=SPANS)        # no transcript
    env = A.run_assertions(_doc(_custom("a", "greets")), ctx)
    r = env["results"][0]
    assert r["status"] == "INCONCLUSIVE"
    assert r["custom"]["missing"] == ["transcript"]
    assert env["exit_code"] == 0              # default policy: report
    env = A.run_assertions(_doc(_custom("a", "greets"), policy="refuse"), ctx)
    assert env["exit_code"] == 2


def test_a_metric_that_answers_differently_twice_is_refused(tmp_path):
    _load(tmp_path, """
    import itertools
    from hotato.metrics import passed, failed
    _n = itertools.count()
    def metric_flaky(call):
        return passed([call.cite_turn(0)]) if next(_n) % 2 == 0 else failed("flip")
    """)
    with pytest.raises(ValueError, match="not deterministic"):
        A.run_assertions(_doc(_custom("a", "flaky")), A.build_context(transcript=TRANSCRIPT))


@pytest.mark.parametrize("ret,needle", [
    ("True", "a bare bool carries no evidence"),
    ("{'status': 'maybe'}", "status must be one of"),
    ("{'status': 'pass', 'score': 0.9}", "unknown key"),
    ("1 / 0", "raised ZeroDivisionError"),
])
def test_a_plugin_that_breaks_the_contract_stops_the_run(tmp_path, ret, needle):
    _load(tmp_path, f"""
    def metric_bad(call):
        return {ret}
    """)
    with pytest.raises(ValueError, match=needle.replace("(", r"\(")):
        A.run_assertions(_doc(_custom("a", "bad")), A.build_context(transcript=TRANSCRIPT))


def test_a_metric_cannot_change_what_other_checks_read(tmp_path):
    _load(tmp_path, """
    from hotato.metrics import passed
    def metric_vandal(call):
        call.transcript[0]["text"] = "changed"
        call.transcript.append({"role": "agent", "text": "extra"})
        return passed([call.cite_turn(0, "changed")])
    """)
    ctx = A.build_context(transcript=TRANSCRIPT)
    env = A.run_assertions(_doc(
        _custom("v", "vandal"),
        {"id": "p", "kind": "phrase", "regex": "Acme Clinic", "role": "agent", "position": "first"},
    ), ctx)
    assert env["results"][1]["status"] == "PASS"
    assert ctx.transcript[0]["text"].startswith("Thanks for calling")


def test_params_are_passed_to_two_argument_metrics(tmp_path):
    _load(tmp_path, """
    from hotato.metrics import passed, failed
    def metric_turns_at_most(call, params):
        n = len(call.transcript)
        if n <= params["max"]:
            return passed([call.cite_turn(n - 1)], measured={"turns": n})
        return failed(f"{n} turns", measured={"turns": n})
    """)
    ctx = A.build_context(transcript=TRANSCRIPT)
    ok = A.run_assertions(_doc(_custom("a", "turns_at_most", params={"max": 5})), ctx)
    bad = A.run_assertions(_doc(_custom("a", "turns_at_most", params={"max": 2})), ctx)
    assert ok["results"][0]["status"] == "PASS"
    assert ok["results"][0]["custom"]["measured"] == {"turns": 3}
    assert bad["results"][0]["status"] == "FAIL"


JUDGED = """
    from hotato.metrics import metric, passed, failed
    @metric(judged=True, model="local-judge:8b", temperature=0, needs=["transcript"])
    def metric_polite(call):
        '''The agent stays polite.'''
        if call.params.get("explode"):
            raise ConnectionError("judge backend unreachable")
        if call.params.get("verdict") == "fail":
            return failed("rude", evidence=[call.cite_turn(2)])
        return passed([call.cite_turn(0, "how can I help")], reason="polite")
"""


def test_a_judged_metric_never_counts_in_the_deterministic_lane(tmp_path):
    _load(tmp_path, JUDGED)
    env = A.run_assertions(_doc(_custom("p", "polite")), A.build_context(transcript=TRANSCRIPT))
    r = env["results"][0]
    assert r["status"] == "INCONCLUSIVE" and "rubric lane" in r["reason"]
    assert r["custom"]["lane"] == "judged"


# --- the judged lane (rubric.v1) ----------------------------------------------

def test_a_judged_metric_is_a_rubric_v1_result_with_the_plugin_as_judge(tmp_path):
    import jsonschema

    _load(tmp_path, JUDGED)
    env = R.evaluate_rubric_lane(
        [{"id": "polite", "kind": "custom", "metric": "polite"},
         {"id": "rude", "kind": "custom", "metric": "polite", "params": {"verdict": "fail"}}],
        transcript=TRANSCRIPT,
    )
    jsonschema.validate(env, _schema("rubric.v1.json"))
    ok, bad = env["results"]
    assert ok["deterministic"] is False and ok["status"] == "PASS"
    assert ok["judge"]["provider"] == "custom"
    assert ok["judge"]["model"] == "local-judge:8b"
    assert ok["judge"]["declared_by_plugin"] == ["model", "temperature"]
    assert ok["judge"]["prompt_sha256"] == ok["custom"]["source_sha256"]
    assert ok["judge"]["citations"] == [
        {"type": "transcript_span", "turn": 0, "quote": "how can I help"}]
    assert bad["status"] == "FAIL" and bad["review"]["human_required"] is True
    assert env["exit_code"] == 0              # advisory by default
    gated = R.rubric_envelope(env["results"], gate=True)
    assert gated["exit_code"] == 1


def test_a_judged_metric_that_raises_is_an_error_which_refuses_when_gated(tmp_path):
    _load(tmp_path, JUDGED)
    env = R.evaluate_rubric_lane(
        [{"id": "p", "kind": "custom", "metric": "polite", "params": {"explode": True}}],
        transcript=TRANSCRIPT, gate=True,
    )
    assert env["results"][0]["status"] == "ERROR"
    assert env["exit_code"] == 2


def test_a_deterministic_metric_is_refused_in_the_rubric_lane(tmp_path):
    _load(tmp_path)
    with pytest.raises(ValueError, match="is deterministic"):
        R.validate_rubric_object({"id": "g", "kind": "custom", "metric": "greets"})


# --- CLI, conversation tests and reports ---------------------------------------

def _example_args(*extra):
    return ["assert", "run", "--assertions", str(EXAMPLE / "assertions.yaml"),
            "--transcript", str(EXAMPLE / "call.transcript.json"),
            "--metrics", str(EXAMPLE / "hotato_metrics"), *extra]


def test_bundled_example_runs_green(capsys):
    rc = cli.main(_example_args("--trace", str(EXAMPLE / "voice_trace.jsonl"), "--format", "json"))
    env = json.loads(capsys.readouterr().out)
    assert rc == 0
    custom = [r for r in env["results"] if r["kind"] == "custom"]
    assert len(custom) == 3 and all(r["status"] == "PASS" for r in custom)


def test_bundled_example_refuses_without_the_trace(capsys):
    # the example's assertions file sets inconclusive_policy: refuse
    rc = cli.main(_example_args())
    out = capsys.readouterr().out
    assert rc == 2
    assert "needs trace, which was not supplied" in out
    assert "(custom metric claim_backed_by_tool" in out


def test_cli_assert_metrics_lists_the_example(capsys):
    rc = cli.main(["assert", "metrics", "--metrics", str(EXAMPLE / "hotato_metrics"),
                   "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    names = {m["name"]: m for m in payload["metrics"]}
    assert set(names) == {"greets_with_name", "max_agent_turn_words", "claim_backed_by_tool"}
    assert names["claim_backed_by_tool"]["needs"] == ["transcript", "spans"]
    assert all(m["lane"] == "deterministic" for m in payload["metrics"])


def test_cli_missing_metrics_path_is_exit_2(tmp_path, capsys):
    rc = cli.main(["assert", "metrics", "--metrics", str(tmp_path / "nope")])
    assert rc == 2


def test_conversation_test_runs_both_lanes_and_reports_label_custom(tmp_path, capsys):
    _plugin(tmp_path, GREET, name="det.py")
    folder = _plugin(tmp_path, JUDGED, name="judged.py")
    tx = tmp_path / "t.json"
    tx.write_text(json.dumps(TRANSCRIPT), encoding="utf-8")
    test_doc = {
        "kind": "hotato.conversation-test", "version": 1, "id": "custom-demo",
        "agent": "a",
        "assertions": {
            "deterministic": [_custom("named", "greets")],
            "rubric": [{"id": "polite", "kind": "custom", "metric": "polite"}],
        },
    }
    tf = tmp_path / "custom.conversation-test.json"
    tf.write_text(json.dumps(test_doc), encoding="utf-8")
    rc = cli.main(["test", "run", str(tf), "--agent", "a", "--transcript", str(tx),
                   "--metrics", str(folder), "--format", "json"])
    result = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert result["assertions"]["results"][0]["custom"]["metric"] == "greets"
    assert result["rubric"]["results"][0]["custom"]["metric"] == "polite"
    assert result["assertions"]["summary"]["deterministic"]["pass"] == 1

    from hotato import report as RP

    html = RP._assertions_section(result["assertions"], result["rubric"])
    assert "custom metric greets v1" in html and "rubric · custom" in html
    md = "\n".join(RP._assertions_md(result["assertions"], result["rubric"]))
    assert "custom metric greets v1" in md
    assert "local-judge:8b (declared by the plugin)" in md
