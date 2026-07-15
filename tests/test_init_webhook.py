"""`hotato init webhook --stack STACK --target fastapi --out DIR`: the generated
webhook worker scaffold, and its four honesty invariants.

Pinned here: the eight files land, the generated ``app.py`` parses (syntax + AST),
``hotato.yaml`` matches the worker schema, and the four invariants hold under an
AST scan of the generated ``app.py``:

  1. it never calls a voice-platform config-mutation endpoint;
  2. it never labels intent or emits a verdict -- discovery only;
  3. it verifies the webhook secret BEFORE any parse, fetch, or scan;
  4. the recording fetch is read-only.

Plus the runtime behaviour of every stack's worker (vapi, retell, twilio),
driven end-to-end with FastAPI's TestClient and a faked ``hotato ingest``
subprocess, each signing its own scheme (Vapi shared secret, Retell's
``v=<timestamp>,d=<digest>`` header = HMAC-SHA256 over raw body + timestamp
inside a 5-minute window, Twilio HMAC-SHA1 over url + sorted params): a bad secret is
rejected 401 before anything runs, a non-terminal event is ignored 200, and a
call-ended event invokes ONLY ``hotato ingest --stack STACK``.
"""

import ast
import base64
import hashlib
import hmac
import importlib.util
import json
import time
import types
from urllib.parse import parse_qs, urlencode

import pytest

from hotato import cli, initcmd

EXPECTED_FILES = {
    "README.md",
    "hotato.yaml",
    "app.py",
    "requirements.txt",
    "Dockerfile",
    ".env.example",
    ".github/workflows/deploy.yml",
    "tests/test_webhook_contract.py",
}

VENDOR_API_HOSTS = ("api.vapi.ai", "api.retellai.com", "api.twilio.com")
FORBIDDEN_HTTP_METHODS = ("PUT", "PATCH", "DELETE")
FORBIDDEN_SUBCOMMANDS = (
    "run", "verify", "compare", "plan", "patch", "apply", "fixture",
    "promote", "create", "connect", "diagnose",
)


def _scaffold(tmp_path, stack="vapi", *extra, target="fastapi"):
    return cli.main([
        "init", "webhook", "--stack", stack, "--target", target,
        "--out", str(tmp_path), *extra,
    ])


def _app_tree(tmp_path):
    src = (tmp_path / "app.py").read_text(encoding="utf-8")
    compile(src, "app.py", "exec")  # syntax
    return ast.parse(src)


# --- AST helpers (mirror the shipped contract test) ------------------------

def _string_constants(node):
    return [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _find_function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _first_call_line(fn, name):
    lines = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Name) and f.id == name) or \
               (isinstance(f, ast.Attribute) and f.attr == name):
                lines.append(node.lineno)
    return min(lines) if lines else None


def _calls_attr(fn, attr):
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == attr
        for n in ast.walk(fn)
    )


def _raises_httpexception(fn):
    for node in ast.walk(fn):
        if isinstance(node, ast.Raise) and node.exc is not None:
            exc = node.exc
            f = exc.func if isinstance(exc, ast.Call) else exc
            if (getattr(f, "id", None) or getattr(f, "attr", None)) == "HTTPException":
                return True
    return False


def _hotato_calls(tree):
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if (isinstance(f, ast.Attribute) and f.attr in ("run", "Popen", "call")
                and isinstance(f.value, ast.Name) and f.value.id == "subprocess"
                and node.args and isinstance(node.args[0], (ast.List, ast.Tuple))):
            argv = [e.value for e in node.args[0].elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if argv and argv[0] == "hotato":
                out.append(argv)
    return out


# --- scaffolding -----------------------------------------------------------

@pytest.mark.parametrize("stack", initcmd.WEBHOOK_STACKS)
def test_scaffolds_all_eight_files(tmp_path, stack):
    assert _scaffold(tmp_path, stack) == 0
    found = {
        str(p.relative_to(tmp_path)).replace("\\", "/")
        for p in tmp_path.rglob("*") if p.is_file()
    }
    assert found == EXPECTED_FILES


@pytest.mark.parametrize("stack", initcmd.WEBHOOK_STACKS)
def test_generated_app_parses(tmp_path, stack):
    assert _scaffold(tmp_path, stack) == 0
    assert isinstance(_app_tree(tmp_path), ast.Module)


@pytest.mark.parametrize("stack", initcmd.WEBHOOK_STACKS)
def test_hotato_yaml_matches_schema(tmp_path, stack):
    yaml = pytest.importorskip("yaml")
    jsonschema = pytest.importorskip("jsonschema")
    assert _scaffold(tmp_path, stack) == 0
    cfg = yaml.safe_load((tmp_path / "hotato.yaml").read_text(encoding="utf-8"))
    schema = {
        "type": "object",
        "required": ["version", "stack", "target", "webhook", "recording",
                     "report", "scan", "notify"],
        "properties": {
            "version": {"const": 1},
            "stack": {"enum": list(initcmd.WEBHOOK_STACKS)},
            "target": {"const": "fastapi"},
            "webhook": {
                "type": "object", "required": ["event", "verify"],
                "properties": {"verify": {
                    "type": "object",
                    "required": ["method", "header", "secret_env"]}},
            },
            "recording": {
                "type": "object", "required": ["access", "channels"],
                "properties": {"access": {"const": "read-only"},
                               "channels": {"const": "dual"}},
            },
            "report": {"type": "object", "required": ["dir", "formats"]},
            "scan": {"type": "object", "required": ["min_gap_sec", "top"]},
            "notify": {
                "type": "object", "required": ["slack", "github"],
                "properties": {"github": {
                    "type": "object", "required": ["enabled", "create_issues"],
                    "properties": {"enabled": {"const": False},
                                   "create_issues": {"const": False}}}},
            },
        },
    }
    jsonschema.validate(cfg, schema)
    assert cfg["stack"] == stack
    assert set(cfg["report"]["formats"]) == {"json", "html"}


# --- the four honesty invariants (AST scan) --------------------------------

@pytest.mark.parametrize("stack", initcmd.WEBHOOK_STACKS)
def test_invariant_1_no_platform_config_mutation(tmp_path, stack):
    assert _scaffold(tmp_path, stack) == 0
    tree = _app_tree(tmp_path)
    consts = _string_constants(tree)
    for host in VENDOR_API_HOSTS:
        assert all(host not in c for c in consts), f"app.py references {host}"
    for method in FORBIDDEN_HTTP_METHODS:
        assert method not in consts
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in ("put", "patch", "delete")


@pytest.mark.parametrize("stack", initcmd.WEBHOOK_STACKS)
def test_invariant_2_discovery_only_no_verdict_no_intent(tmp_path, stack):
    assert _scaffold(tmp_path, stack) == 0
    tree = _app_tree(tmp_path)
    calls = _hotato_calls(tree)
    assert calls, "the worker must shell out to hotato ingest"
    for argv in calls:
        assert "ingest" in argv
        for bad in FORBIDDEN_SUBCOMMANDS:
            assert bad not in argv, f"worker calls hotato {bad}"
        assert "--expect" not in argv
    assert "--expect" not in _string_constants(tree)


@pytest.mark.parametrize("stack", initcmd.WEBHOOK_STACKS)
def test_invariant_3_secret_verified_before_processing(tmp_path, stack):
    assert _scaffold(tmp_path, stack) == 0
    tree = _app_tree(tmp_path)
    handler = _find_function(tree, "handle_webhook")
    assert handler is not None
    verify_line = _first_call_line(handler, "verify_webhook")
    assert verify_line is not None
    for later in ("parse_payload", "is_target_event", "run_hotato_ingest"):
        line = _first_call_line(handler, later)
        if line is not None:
            assert verify_line < line, f"verify must precede {later}"
    verify = _find_function(tree, "verify_webhook")
    assert verify is not None
    assert _calls_attr(verify, "compare_digest")
    assert _raises_httpexception(verify)


@pytest.mark.parametrize("stack", initcmd.WEBHOOK_STACKS)
def test_invariant_4_recording_fetch_is_read_only(tmp_path, stack):
    assert _scaffold(tmp_path, stack) == 0
    tree = _app_tree(tmp_path)
    assert _hotato_calls(tree), "fetch must be delegated to hotato ingest"
    consts = _string_constants(tree)
    for host in VENDOR_API_HOSTS:
        assert all(host not in c for c in consts)


# --- usage errors ----------------------------------------------------------

def test_unknown_stack_is_exit_2(tmp_path):
    # argparse enforces --stack choices: an unknown stack is a usage error (2).
    with pytest.raises(SystemExit) as excinfo:
        cli.main([
            "init", "webhook", "--stack", "nope", "--target", "fastapi",
            "--out", str(tmp_path / "w"),
        ])
    assert excinfo.value.code == 2
    # The library-level guard refuses too (for the MCP / direct-call path).
    with pytest.raises(initcmd.InitError):
        initcmd.scaffold_webhook("nope", "fastapi", str(tmp_path / "w2"))
    assert not (tmp_path / "w2").exists()


def test_overwrite_needs_force(tmp_path):
    assert _scaffold(tmp_path, "vapi") == 0
    # A second scaffold into the same dir refuses without --force ...
    assert _scaffold(tmp_path, "vapi") == 2
    # ... and succeeds with it.
    assert _scaffold(tmp_path, "vapi", "--force") == 0


def test_json_output_shape(tmp_path, capsys):
    assert _scaffold(tmp_path, "vapi", "--format", "json") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["kind"] == "init-webhook"
    assert out["stack"] == "vapi"
    assert out["target"] == "fastapi"
    assert set(out["files"]) == EXPECTED_FILES


# --- runtime: drive each stack's worker end-to-end -------------------------
#
# Each stack verifies its webhook differently, so the request is built per stack
# but every case asserts the SAME contract. A case is (env, target, nontarget,
# bad) where each request is a (content_bytes, headers) pair carrying the EXACT
# signature for that content: `target` is a valid call-ended event, `nontarget`
# a valid but non-terminal event, `bad` a call-ended event with a wrong secret.

_SECRET = "s3cret"
_TWILIO_URL = "https://worker.example.test/webhook"


def _twilio_signature(body: bytes) -> str:
    # Mirror the shipped verify_twilio fragment: base64(HMAC-SHA1(token, url +
    # sorted param concatenation)) over the exact posted bytes.
    params = {k: v[-1] for k, v in
              parse_qs(body.decode("utf-8"), keep_blank_values=True).items()}
    signed = _TWILIO_URL + "".join(k + params[k] for k in sorted(params))
    digest = hmac.new(_SECRET.encode("utf-8"), signed.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def _retell_signature(body: bytes, secret: str = _SECRET, timestamp: int | None = None) -> str:
    # Retell's documented ``X-Retell-Signature`` = ``v=<unix_ts>,d=<hex_digest>``
    # where the digest is HMAC-SHA256(api_key, raw_body + timestamp), accepted
    # only inside a 5-minute freshness window.
    ts = str(int(time.time()) if timestamp is None else timestamp)
    digest = hmac.new(secret.encode("utf-8"), body + ts.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"v={ts},d={digest}"


def _runtime_case(stack):
    if stack == "vapi":
        env = {"VAPI_WEBHOOK_SECRET": _SECRET}
        ct = "application/json"
        target = json.dumps(
            {"message": {"type": "end-of-call-report", "call": {"id": "abc"}}}
        ).encode("utf-8")
        nontarget = json.dumps({"message": {"type": "status-update"}}).encode("utf-8")
        good = {"x-vapi-secret": _SECRET, "content-type": ct}
        bad = {"x-vapi-secret": "WRONG", "content-type": ct}
        return env, (target, good), (nontarget, good), (target, bad)
    if stack == "retell":
        env = {"RETELL_API_KEY": _SECRET}
        ct = "application/json"
        target = json.dumps(
            {"event": "call_ended", "call": {"call_id": "abc"}}
        ).encode("utf-8")
        nontarget = json.dumps(
            {"event": "call_started", "call": {"call_id": "abc"}}
        ).encode("utf-8")

        return (
            env,
            (target, {"x-retell-signature": _retell_signature(target), "content-type": ct}),
            (nontarget, {"x-retell-signature": _retell_signature(nontarget), "content-type": ct}),
            # A valid-format header signed with the WRONG key: 401, no processing.
            (target, {"x-retell-signature": _retell_signature(target, secret="WRONG"),
                      "content-type": ct}),
        )
    if stack == "twilio":
        env = {"TWILIO_AUTH_TOKEN": _SECRET, "TWILIO_WEBHOOK_URL": _TWILIO_URL}
        ct = "application/x-www-form-urlencoded"
        target = urlencode(
            {"CallSid": "CA1", "RecordingSid": "RE1", "RecordingStatus": "completed"}
        ).encode("utf-8")
        nontarget = urlencode(
            {"CallSid": "CA1", "RecordingStatus": "in-progress"}
        ).encode("utf-8")
        return (
            env,
            (target, {"x-twilio-signature": _twilio_signature(target), "content-type": ct}),
            (nontarget, {"x-twilio-signature": _twilio_signature(nontarget), "content-type": ct}),
            (target, {"x-twilio-signature": "AAAAAAAAAAAAAAAAAAAAAAAAAAA=", "content-type": ct}),
        )
    raise AssertionError("no runtime case for stack " + stack)


def _load_app(tmp_path, monkeypatch, recorder, env):
    """Import the generated app.py as a module with a faked hotato ingest and the
    stack's secret env in place."""
    monkeypatch.setenv("HOTATO_REPORT_DIR", str(tmp_path / "reports"))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location(
        "hotato_generated_worker_" + tmp_path.name, str(tmp_path / "app.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def fake_run(argv, *a, **k):
        recorder.append(argv)
        return types.SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "total_candidates": 2, "candidates": [], "source": "call.wav",
            }),
            stderr="",
        )

    monkeypatch.setattr(mod, "subprocess", types.SimpleNamespace(run=fake_run))
    return mod


@pytest.mark.parametrize("stack", initcmd.WEBHOOK_STACKS)
def test_runtime_bad_secret_rejected_before_processing(tmp_path, monkeypatch, stack):
    pytest.importorskip("fastapi")
    TestClient = pytest.importorskip("fastapi.testclient").TestClient
    assert _scaffold(tmp_path, stack) == 0
    env, _target, _nontarget, bad = _runtime_case(stack)
    calls = []
    mod = _load_app(tmp_path, monkeypatch, calls, env)
    client = TestClient(mod.app, raise_server_exceptions=False)
    content, headers = bad
    resp = client.post("/webhook", content=content, headers=headers)
    assert resp.status_code == 401
    assert calls == [], "ingest must not run when the secret is wrong"


@pytest.mark.parametrize("stack", initcmd.WEBHOOK_STACKS)
def test_runtime_non_target_event_ignored(tmp_path, monkeypatch, stack):
    pytest.importorskip("fastapi")
    TestClient = pytest.importorskip("fastapi.testclient").TestClient
    assert _scaffold(tmp_path, stack) == 0
    env, _target, nontarget, _bad = _runtime_case(stack)
    calls = []
    mod = _load_app(tmp_path, monkeypatch, calls, env)
    client = TestClient(mod.app, raise_server_exceptions=False)
    content, headers = nontarget
    resp = client.post("/webhook", content=content, headers=headers)
    assert resp.status_code == 200
    assert calls == [], "a non-terminal event must not trigger a fetch"


@pytest.mark.parametrize("stack", initcmd.WEBHOOK_STACKS)
def test_runtime_call_ended_invokes_ingest_only(tmp_path, monkeypatch, stack):
    pytest.importorskip("fastapi")
    TestClient = pytest.importorskip("fastapi.testclient").TestClient
    assert _scaffold(tmp_path, stack) == 0
    env, target, _nontarget, _bad = _runtime_case(stack)
    calls = []
    mod = _load_app(tmp_path, monkeypatch, calls, env)
    client = TestClient(mod.app, raise_server_exceptions=False)
    content, headers = target
    resp = client.post("/webhook", content=content, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["candidates"] == 2
    assert len(calls) == 1
    argv = calls[0]
    assert argv[:4] == ["hotato", "ingest", "--stack", stack]
    for bad in FORBIDDEN_SUBCOMMANDS:
        assert bad not in argv
    assert "--expect" not in argv


# --- Retell vendor-format signature: adversarial regression ----------------
#
# Retell's documented header is ``v=<unix_ts>,d=<hex_digest>`` with the digest
# taken over ``raw_body + timestamp`` and a 5-minute freshness window. The old
# scaffold expected a BARE hex HMAC of the raw body only, so a genuine Retell
# delivery was rejected 401 while a bare-hex forgery in hotato's own scheme was
# accepted. These cases pin the vendor contract: a fresh valid ``v=,d=`` is
# accepted, and every malformed / mutated / stale / future / duplicate / bare
# variant is rejected before ingest runs.

_RETELL_TARGET = json.dumps(
    {"event": "call_ended", "call": {"call_id": "abc"}}
).encode("utf-8")


def _post_retell(tmp_path, monkeypatch, header_value, body=_RETELL_TARGET):
    TestClient = pytest.importorskip("fastapi.testclient").TestClient
    assert _scaffold(tmp_path, "retell") == 0
    calls = []
    mod = _load_app(tmp_path, monkeypatch, calls, {"RETELL_API_KEY": _SECRET})
    client = TestClient(mod.app, raise_server_exceptions=False)
    headers = {"content-type": "application/json"}
    if header_value is not None:
        headers["x-retell-signature"] = header_value
    resp = client.post("/webhook", content=body, headers=headers)
    return resp, calls


def test_retell_accepts_fresh_vendor_signature(tmp_path, monkeypatch):
    # A genuine documented-format delivery must verify and be processed.
    # (Fails on the old bare-hex scaffold, which 401s the vendor header.)
    pytest.importorskip("fastapi")
    resp, calls = _post_retell(tmp_path, monkeypatch, _retell_signature(_RETELL_TARGET))
    assert resp.status_code == 200
    assert len(calls) == 1


def test_retell_rejects_hotato_bare_hex_scheme(tmp_path, monkeypatch):
    # hotato's old incompatible scheme -- a bare hex HMAC of the raw body with
    # no ``v=/d=`` envelope -- must NOT verify. (Passes 200 on the old code.)
    pytest.importorskip("fastapi")
    bare = hmac.new(_SECRET.encode("utf-8"), _RETELL_TARGET, hashlib.sha256).hexdigest()
    resp, calls = _post_retell(tmp_path, monkeypatch, bare)
    assert resp.status_code == 401
    assert calls == [], "a bare-hex forgery must never reach ingest"


def _retell_bad_headers():
    now = int(time.time())

    def digest(ts, secret=_SECRET, body=_RETELL_TARGET):
        return hmac.new(secret.encode("utf-8"), body + str(ts).encode("utf-8"),
                        hashlib.sha256).hexdigest()

    good = digest(now)
    flipped = ("0" if good[-1] != "0" else "1")
    return {
        # right timestamp, wrong (mutated) digest
        "mutated": f"v={now},d={good[:-1] + flipped}",
        # no key=value envelope at all (bare hex forgery already covered)
        "malformed": f"{now}:{good}",
        # valid digest but the delivery is older than the 5-minute window
        "expired": f"v={now - 600},d={digest(now - 600)}",
        # valid digest but dated in the future beyond clock skew
        "future": f"v={now + 3600},d={digest(now + 3600)}",
        # duplicate ``v`` field
        "duplicate": f"v={now},v={now}",
        # missing the digest field
        "missing_digest": f"v={now}",
        # unknown extra key instead of ``d``
        "unknown_key": f"v={now},x={good}",
        # non-hex digest value
        "non_hex": f"v={now},d=zzzznothex",
        # non-numeric timestamp
        "bad_timestamp": f"v=notanumber,d={digest(now)}",
        # non-ASCII "digit" timestamp: str.isdigit() accepts superscripts but
        # int() rejects them -- must reject 401, never surface an uncaught 500.
        "unicode_digit": f"v=²³⁴¹⁰,d={digest(now)}",
        # three fields -> not the strict two-field form
        "extra_field": f"v={now},d={good},extra=1",
        # empty header
        "empty": "",
    }


@pytest.mark.parametrize("variant", sorted(_retell_bad_headers()))
def test_retell_rejects_bad_vendor_signatures(tmp_path, monkeypatch, variant):
    pytest.importorskip("fastapi")
    header = _retell_bad_headers()[variant]
    resp, calls = _post_retell(tmp_path, monkeypatch, header)
    assert resp.status_code == 401, f"variant {variant!r} was not rejected"
    assert calls == [], f"variant {variant!r} reached ingest"


def test_retell_missing_header_rejected(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    resp, calls = _post_retell(tmp_path, monkeypatch, None)
    assert resp.status_code == 401
    assert calls == []
