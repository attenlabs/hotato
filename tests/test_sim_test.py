"""hotato test run (simulated conversations): fake agent + fake judge, no network."""
import json

import pytest

from hotato import cli, sim_test as S


class FakeAgent(S.AgentTarget):
    stack, id, reports_tool_calls = "fake", "a1", True

    def __init__(self, replies=None, calls=None, fail=False):
        self.replies = replies or ["Hello, how can I help?", "Your refund is on its way."]
        self.calls, self.fail, self.n = calls or [], fail, 0

    def start(self, case):
        self.n = 0

    def send(self, text):
        if self.fail:
            raise S.SimError("agent down")
        r = self.replies[min(self.n, len(self.replies) - 1)]
        self.n += 1
        return S.AgentReply(r, list(self.calls) if self.n == 1 else [])


class FakeLLM(S.LLM):
    """Caller lines, then a judge JSON; the judge is told apart by its system prompt."""
    provider, model = "fake", "m1"

    def __init__(self, caller=None, verdicts=None):
        self.caller = list(caller or ["Hi, my order is broken.", "Thanks!" + S.END_REACHED])
        self.verdicts, self.i = verdicts, 0

    def chat(self, system, messages):
        if system.startswith("You grade"):
            if callable(self.verdicts):
                return json.dumps(self.verdicts())
            return json.dumps(self.verdicts)
        if len(messages) == 1:  # a fresh conversation
            self.i = 0
        line = self.caller[min(self.i, len(self.caller) - 1)]
        self.i += 1
        return line


CASE = {"name": "Refund", "persona": "angry {{who}}", "goal": "get refund",
        "success_criteria": ["agent offers refund"], "forbidden": ["insults"],
        "variables": {"who": "bob"}, "max_turns": 4, "repeat": 2}


def _case(**over):
    c = S.parse_case({**CASE, **over}, "x.json")
    c["case_id"] = "refund"
    return c


def _good():
    return [{"id": 0, "verdict": "pass", "confidence": 0.9,
             "evidence": "Your refund is on its way.", "explanation": "ok"},
            {"id": 1, "verdict": "pass", "confidence": 0.8, "evidence": "",
             "explanation": "no insults"}]


def test_parse_defaults_and_substitution():
    c = S.parse_case({"persona": "p {{a}}", "goal": "g", "success_criteria": ["x"],
                      "variables": {"a": 1}}, "f")
    assert c["persona"] == "p 1" and c["max_turns"] == 12 and c["repeat"] == 3


@pytest.mark.parametrize("bad", [
    {"persona": "p", "goal": "g"},
    {"persona": "p", "goal": "g", "success_criteria": ["x"], "sucess": 1},
    {"persona": "p {{missing}}", "goal": "g", "success_criteria": ["x"]},
    {"persona": "p", "goal": "g", "success_criteria": ["x"], "max_turns": 0},
])
def test_parse_refuses(bad):
    with pytest.raises(S.SimError):
        S.parse_case(bad, "f")


def test_run_pass_and_schema():
    llm = FakeLLM(verdicts=_good())
    res = S.run_cases([_case()], FakeAgent(), llm, llm)
    c = res["cases"][0]
    assert res["schema"] == "hotato.testrun.v1"
    assert (c["repeats"], c["passed"], c["pass_all"], c["pass_rate"]) == (2, 2, True, 1.0)
    run = c["runs"][0]
    assert [t["role"] for t in run["transcript"]] == ["caller", "agent", "caller", "agent"]
    assert run["error"] is None and set(run["verdicts"][0]) == {
        "criterion", "verdict", "confidence", "evidence", "explanation"}
    assert res["summary"] == {"cases": 1, "pass_rate": 1.0, "pass_all": 1}
    assert S.exit_code(res) == 0
    assert "PASS" in S.render_text(res) and "<table>" in S.render_html(res)


def test_pass_without_real_quote_is_unknown():
    v = _good()
    v[0]["evidence"] = "something never said"
    llm = FakeLLM(verdicts=v)
    res = S.run_cases([_case()], FakeAgent(), llm, llm, repeat=1)
    first = res["cases"][0]["runs"][0]["verdicts"][0]
    assert first["verdict"] == "unknown" and first["evidence"] == ""
    assert res["cases"][0]["pass_all"] is False and S.exit_code(res) == 1


def test_missing_verdict_is_unknown_and_forbidden_fail():
    v = [{"id": 1, "verdict": "fail", "confidence": 1, "evidence": "Hello, how can I help?",
          "explanation": "rude"}]
    llm = FakeLLM(verdicts=v)
    run = S.run_cases([_case()], FakeAgent(), llm, llm, repeat=1)["cases"][0]["runs"][0]
    assert [x["verdict"] for x in run["verdicts"]] == ["unknown", "fail"]
    assert run["verdicts"][1]["criterion"] == "forbidden: insults"


def test_tool_calls_deterministic():
    case = _case(expected_tool_calls=["lookup", {"name": "refund", "arguments": {"id": "7"}}],
                 success_criteria=[], forbidden=[])
    calls = [{"name": "lookup", "arguments": {}}, {"name": "refund", "arguments": {"id": 7, "x": 1}}]
    llm = FakeLLM(verdicts=[])
    ok = S.run_cases([case], FakeAgent(calls=calls), llm, llm, repeat=1)
    assert ok["cases"][0]["pass_all"] is True
    missing = S.run_cases([case], FakeAgent(calls=calls[:1]), llm, llm, repeat=1)
    assert missing["cases"][0]["runs"][0]["verdicts"][1]["verdict"] == "fail"
    silent = FakeAgent(calls=calls)
    silent.reports_tool_calls = False
    unk = S.run_cases([case], silent, llm, llm, repeat=1)
    assert {v["verdict"] for v in unk["cases"][0]["runs"][0]["verdicts"]} == {"unknown"}


def test_repeats_pass_rate_and_not_pass_all():
    state = {"n": 0}

    def verdicts():
        state["n"] += 1
        v = _good()
        if state["n"] == 2:
            v[0].update(verdict="fail", evidence="")
        return v

    llm = FakeLLM(verdicts=verdicts)
    c = S.run_cases([_case(repeat=4)], FakeAgent(), llm, llm)["cases"][0]
    assert (c["passed"], c["pass_rate"], c["pass_all"]) == (3, 0.75, False)
    assert c["criteria"][0] == {"name": "agent offers refund", "pass_rate": 0.75}
    assert c["criteria"][1]["pass_rate"] == 1.0


def test_max_turns_and_agent_error_recorded():
    llm = FakeLLM(caller=["still here"], verdicts=_good())
    run = S.run_one(_case(max_turns=3), FakeAgent(), llm, llm, 0)
    assert len([t for t in run["transcript"] if t["role"] == "caller"]) == 3
    bad = S.run_one(_case(), FakeAgent(fail=True), llm, llm, 0)
    assert bad["error"] == "agent down"
    res = S.run_cases([_case()], FakeAgent(fail=True), llm, llm, repeat=1)
    assert res["cases"][0]["pass_all"] is False


def test_egress_refusals_before_any_request():
    with pytest.raises(ValueError):
        S.make_llm("anthropic", env={"ANTHROPIC_API_KEY": "k"})
    with pytest.raises(ValueError):
        S.make_llm("openai", base_url="https://example.com/v1", env={})
    with pytest.raises(S.SimError):
        S.make_agent("vapi:abc")
    with pytest.raises(ValueError):
        S.make_agent("https://example.com/chat")
    assert S.make_llm("ollama", env={}).provider == "ollama"
    assert S.make_llm("openai", base_url="http://localhost:8000/v1", env={}).provider == "openai"
    assert isinstance(S.make_llm("gemini", egress_opt_in=True, env={}), S.GeminiLLM)


def test_provider_request_shapes(monkeypatch):
    seen = []

    def fake_post(url, payload, headers, subject):
        seen.append((url, payload, headers))
        if "anthropic" in subject:
            return {"content": [{"type": "text", "text": "A"}]}
        if "gemini" in subject:
            return {"candidates": [{"content": {"parts": [{"text": "G"}]}}]}
        return {"choices": [{"message": {"content": "O"}}]}

    monkeypatch.setattr(S, "_post_json", fake_post)
    msgs = [{"role": "user", "content": "hi"}]
    assert S.make_llm("anthropic", egress_opt_in=True, env={"ANTHROPIC_API_KEY": "k"}).chat("s", msgs) == "A"
    assert S.make_llm("gemini", egress_opt_in=True, env={"GEMINI_API_KEY": "k"}).chat("s", msgs) == "G"
    assert S.make_llm("openai", egress_opt_in=True, env={"OPENAI_API_KEY": "k"}).chat("s", msgs) == "O"
    assert seen[0][0].endswith("/v1/messages") and seen[0][2]["x-api-key"] == "k"
    assert ":generateContent" in seen[1][0] and seen[1][2]["x-goog-api-key"] == "k"
    assert seen[2][0].endswith("/chat/completions")


def test_vapi_and_retell_targets(monkeypatch):
    calls = []

    def fake_post(url, payload, headers, subject):
        calls.append((url, payload, headers))
        if url.endswith("/chat"):
            return {"id": "c1", "output": [{"role": "assistant", "content": "vapi says hi"}]}
        if url.endswith("/create-chat"):
            return {"chat_id": "r1"}
        return {"messages": [
            {"role": "tool_call_invocation", "name": "lookup", "arguments": "{\"id\": 1}"},
            {"role": "agent", "content": "retell says hi"}]}

    monkeypatch.setattr(S, "_post_json", fake_post)
    monkeypatch.setenv("VAPI_API_KEY", "vk")
    monkeypatch.setenv("RETELL_API_KEY", "rk")
    v = S.make_agent("vapi:asst", egress_opt_in=True)
    v.start({})
    assert v.send("a").text == "vapi says hi"
    v.send("b")
    assert calls[0][1] == {"assistantId": "asst", "input": "a"}
    assert calls[1][1]["previousChatId"] == "c1" and calls[0][2]["Authorization"] == "Bearer vk"
    r = S.make_agent("retell:ag", egress_opt_in=True)
    r.start({"variables": {"k": "v"}})
    rep = r.send("x")
    assert rep.text == "retell says hi" and rep.tool_calls == [{"name": "lookup", "arguments": {"id": 1}}]
    assert calls[2][1] == {"agent_id": "ag", "retell_llm_dynamic_variables": {"k": "v"}}
    assert calls[3][1] == {"chat_id": "r1", "content": "x"}


def test_load_cases_dir_and_routing(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(CASE))
    (tmp_path / "b.json").write_text(json.dumps({**CASE, "name": "Refund"}))
    cases = S.load_cases(str(tmp_path))
    assert [c["case_id"] for c in cases] == ["refund", "refund-2"]
    assert S.looks_like_case_path(str(tmp_path))
    ct = tmp_path / "ct.json"
    ct.write_text(json.dumps({"kind": "hotato.conversation-test", "persona": "x"}))
    assert not S.looks_like_case_path(str(ct))
    with pytest.raises(S.SimError):
        S.load_cases(str(tmp_path / "nope"))


def test_from_call_and_dump(tmp_path):
    segs = [{"role": "caller", "text": "I want to cancel my plan", "start": 0},
            {"role": "agent", "text": "Please hold", "start": 2},
            {"role": "caller", "text": "Hello?", "start": 5}]
    c = S.case_from_transcript({"segments": segs, "findings": [{"summary": "agent went silent"}]}, "x")
    assert "cancel my plan" in c["persona"] and "agent went silent" in c["success_criteria"][0]
    c2 = S.case_from_transcript(segs, "x", at=3.0)
    assert "Please hold" in c2["success_criteria"][0]
    S.parse_case(c, "x")
    for as_yaml in (True, False):
        text = S.dump_case(S.starter_case("n"), as_yaml)
        if not as_yaml:
            S.parse_case(json.loads(text), "n")
    with pytest.raises(S.SimError):
        S.case_from_transcript([{"role": "agent", "text": "hi"}], "x")


def _patch(monkeypatch, verdicts):
    llm = FakeLLM(verdicts=verdicts)
    monkeypatch.setattr(S, "make_llm", lambda *a, **k: llm)
    monkeypatch.setattr(S, "make_agent", lambda *a, **k: FakeAgent())


def test_cli_run_exit_codes_and_artifacts(tmp_path, monkeypatch, capsys):
    f = tmp_path / "case.json"
    f.write_text(json.dumps(CASE))
    out = tmp_path / "out"
    _patch(monkeypatch, _good())
    rc = cli.main(["test", "run", str(f), "--agent", "chat:http://127.0.0.1:1/x",
                   "--format", "json", "--out", str(out)])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0 and doc["schema"] == "hotato.testrun.v1"
    assert json.loads((out / "testrun.json").read_text())["run_id"] == doc["run_id"]
    assert (out / "report.html").exists()
    _patch(monkeypatch, [{"id": 0, "verdict": "fail", "confidence": 1, "evidence": "", "explanation": "no"}])
    assert cli.main(["test", "run", str(f), "--agent", "x:y", "--repeat", "1"]) == 1
    capsys.readouterr()


def test_cli_refuses_with_exit_2(tmp_path, monkeypatch, capsys):
    f = tmp_path / "case.json"
    f.write_text(json.dumps(CASE))
    monkeypatch.delenv("HOTATO_JUDGE_PROVIDER", raising=False)
    # bad agent spec -> 2, no network
    assert cli.main(["test", "run", str(f), "--agent", "nonsense"]) == 2
    # off-box judge without opt-in -> 2
    assert cli.main(["test", "run", str(f), "--agent", "chat:http://127.0.0.1:1/x",
                     "--judge-provider", "anthropic"]) == 2
    capsys.readouterr()


def test_cli_new_and_from_call(tmp_path, capsys):
    assert cli.main(["test", "new", "My Case", "--out", str(tmp_path)]) == 0
    written = [p for p in tmp_path.iterdir()]
    assert len(written) == 1
    assert cli.main(["test", "new", "My Case", "--out", str(tmp_path)]) == 2  # no overwrite
    t = tmp_path / "t.json"
    t.write_text(json.dumps([{"role": "user", "text": "hello"}, {"role": "assistant", "text": "yo"}]))
    assert cli.main(["test", "from-call", str(t), "--out", str(tmp_path), "--name", "fc"]) == 0
    capsys.readouterr()
    assert S.load_cases(str(tmp_path / [p.name for p in tmp_path.iterdir() if p.name.startswith("fc")][0]))


def test_unusable_llm_on_first_run_is_a_refusal():
    class Down(FakeLLM):
        def chat(self, system, messages):
            raise S.SimError("unreachable")

    with pytest.raises(S.SimError):
        S.run_cases([_case()], FakeAgent(), Down(), Down())
