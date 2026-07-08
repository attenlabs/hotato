"""Regression tests for FIX round 2.

Each test pins one confirmed blocker/major defect so it cannot silently return:

  * a WAV whose header declares sample_rate=0 is a clean exit-2 error across
    run / scan / analyze / compare, never a raw ZeroDivisionError traceback;
  * `hotato patch` structurally validates a schema-tagged but partial / wrong
    typed plan (missing from/bounds, non-string stack/field) -> clean ValueError,
    never a raw KeyError / AttributeError;
  * the numpy-vectorized WAV decode in core produces byte-identical values to the
    engine's stdlib list decode (the memory-scaling fix must not move a number);
  * a truncated / cut-off recording is SKIPPED with an honest reason by analyze,
    matching the rejection `run` already does, never scored as a short call;
  * the single-item capture / inspect adapters raise ValueError (not
    AttributeError) on a non-dict JSON body;
  * pull() skips ANY per-item adapter exception and finishes the batch;
  * compare / verify degrade to not_scorable on a verdict dict missing `passed`,
    never KeyError('passed');
  * loop.load_state rejects a corrupt non-int `run` field as a clean error;
  * every cli --out write is atomic (a mid-write failure never truncates a
    previously-good file, and leaves no temp turd);
  * `hotato team` output is a pure function of the envelope FILES, independent of
    filesystem mtimes;
  * a not-scorable event never deflates the headline pass RATE (denominator is
    the scorable population);
  * the capture download adapters refuse a file:// / non-http(s) recording URL
    (local-file read / SSRF), like ingest already does;
  * the MCP report_path is sandboxed BY DEFAULT (no HOTATO_MCP_REPORT_DIR) to the
    OS temp dir, so an agent cannot write an HTML file to an arbitrary path.
"""

import json
import math
import os
import struct
import threading
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from hotato import aggregate as _aggregate
from hotato import capture as cap
from hotato import cli
from hotato import compare as _compare
from hotato import inspectcfg as _inspect
from hotato import loop as _loop
from hotato import mcp_server
from hotato import scan as _scan
from hotato import verify as _verify
from hotato.patch import build_patch


SR = 16000


# --- WAV helpers ------------------------------------------------------------

def _write_stereo(path, caller, agent, sr=SR):
    n = min(len(caller), len(agent))
    frames = bytearray()
    for i in range(n):
        c = int(max(-1.0, min(1.0, caller[i])) * 32767)
        a = int(max(-1.0, min(1.0, agent[i])) * 32767)
        frames += struct.pack("<hh", c, a)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))
    return str(path)


def _tone(n, seg, freq, sr=SR, amp=0.5):
    out = [0.0] * n
    a, b = int(seg[0] * sr), int(seg[1] * sr)
    for i in range(a, min(b, n)):
        out[i] = amp * math.sin(2 * math.pi * freq * i / sr)
    return out


def _talking_stereo(path, dur=4.0):
    n = int(dur * SR)
    agent = _tone(n, (0.0, dur), 330.0)
    caller = _tone(n, (1.5, 2.5), 220.0)
    return _write_stereo(path, caller, agent)


def _write_raw_wav(path, *, n_channels, sampwidth, sample_rate, n_frames):
    """Write a structurally valid RIFF/fmt/data WAV with a chosen (possibly
    invalid, e.g. 0) sample rate. Python's ``wave`` refuses to *write* a 0 rate
    but reads one back happily, so the bytes are laid down by hand."""
    block_align = n_channels * sampwidth
    data = b"\x00" * (block_align * n_frames)
    byte_rate = sample_rate * block_align
    fmt = struct.pack(
        "<HHIIHH", 1, n_channels, sample_rate, byte_rate, block_align, sampwidth * 8
    )
    body = (
        b"WAVE"
        + b"fmt " + struct.pack("<I", len(fmt)) + fmt
        + b"data" + struct.pack("<I", len(data)) + data
    )
    riff = b"RIFF" + struct.pack("<I", len(body)) + body
    with open(str(path), "wb") as fh:
        fh.write(riff)
    return str(path)


# --- 1. sample_rate = 0 is a clean exit-2 error everywhere -------------------

def test_zero_sample_rate_wav_reads_back_as_zero(tmp_path):
    """Guards the premise: ``wave`` really does read a 0 rate back."""
    p = _write_raw_wav(tmp_path / "fr0.wav", n_channels=2, sampwidth=2,
                       sample_rate=0, n_frames=100)
    with wave.open(p, "rb") as wf:
        assert wf.getframerate() == 0


def test_zero_sample_rate_run_scan_compare_exit_2(tmp_path):
    p = _write_raw_wav(tmp_path / "fr0.wav", n_channels=2, sampwidth=2,
                       sample_rate=0, n_frames=16000)
    # run
    assert cli.main(["run", "--stereo", p]) == 2
    # scan
    assert cli.main(["scan", "--stereo", p]) == 2
    # compare (goes through run_single)
    assert cli.main(["compare", "--before", p, "--after", p, "--onset", "1.0"]) == 2


def test_zero_sample_rate_scan_recording_raises_valueerror(tmp_path):
    p = _write_raw_wav(tmp_path / "fr0.wav", n_channels=2, sampwidth=2,
                       sample_rate=0, n_frames=16000)
    with pytest.raises(ValueError) as ei:
        _scan.scan_recording(p)
    assert "sample rate" in str(ei.value).lower()


def test_zero_sample_rate_analyze_skips_it(tmp_path):
    folder = tmp_path / "calls"
    folder.mkdir()
    _write_raw_wav(folder / "fr0.wav", n_channels=2, sampwidth=2,
                   sample_rate=0, n_frames=16000)
    from hotato import analyze as _analyze
    aggregate, _per = _analyze.analyze_folder(str(folder))
    assert aggregate["calls_skipped"] == 1
    assert any("sample rate" in s["reason"].lower() for s in aggregate["skipped"])


# --- 2 + 7. patch validates partial / wrong-typed plans ---------------------

def _plan(**over):
    base = {
        "schema": "hotato.fixplan.v1",
        "decision": "propose_one_step",
        "target": {"stack": "vapi", "assistant_id": "abc"},
        "changes": [{
            "field": "startSpeakingPlan.waitSeconds",
            "direction": "down", "from": 0.4, "to": 0.2, "bounds": [0, 5],
        }],
    }
    base.update(over)
    return base


def test_patch_missing_from_is_valueerror_not_keyerror():
    plan = _plan(changes=[{
        "field": "startSpeakingPlan.waitSeconds", "direction": "down", "to": 0.2,
        "bounds": [0, 5],  # no 'from'
    }])
    with pytest.raises(ValueError):
        build_patch(plan)


def test_patch_missing_bounds_is_valueerror_not_keyerror():
    plan = _plan(changes=[{
        "field": "startSpeakingPlan.waitSeconds", "direction": "down",
        "from": 0.4, "to": None,  # no 'bounds', to is None
    }])
    with pytest.raises(ValueError):
        build_patch(plan)


def test_patch_non_string_stack_is_valueerror_not_attributeerror():
    plan = _plan(target={"stack": 123}, decision="no_change", changes=[])
    with pytest.raises(ValueError):
        build_patch(plan)


def test_patch_non_string_field_is_valueerror_not_attributeerror():
    plan = _plan(changes=[{
        "field": 123, "direction": "up", "to": 5, "from": 1, "bounds": [0, 10],
    }])
    with pytest.raises(ValueError):
        build_patch(plan)


def test_patch_valid_plan_still_builds():
    """The stricter validation must not break a well-formed plan."""
    out = build_patch(_plan())
    assert out["config_patchable"] is True
    assert out["change"]["field"] == "startSpeakingPlan.waitSeconds"


# --- 3. the numpy-vectorized decode matches the engine's stdlib decode -------

def test_vectorized_decode_matches_stdlib_values(tmp_path):
    p = _talking_stereo(tmp_path / "call.wav")
    from hotato import core
    from hotato._engine import audio as A

    ref = A.read_wav(p)  # the pure stdlib list-based decode
    got = core._load_signal(p)  # numpy-vectorized when numpy is present
    assert got.sample_rate == ref.sample_rate
    assert got.num_channels == ref.num_channels
    for ch in range(ref.num_channels):
        a = list(ref.get(ch))
        b = [float(x) for x in got.get(ch)]
        assert a == b  # byte-identical float values, not merely close


def test_load_signal_falls_back_when_numpy_absent(tmp_path):
    p = _talking_stereo(tmp_path / "call.wav")
    from hotato import core
    from hotato._engine import audio as A

    saved = A._np
    A._np = None
    try:
        sig = core._load_signal(p)
    finally:
        A._np = saved
    assert sig.num_channels == 2 and sig.sample_rate == SR


# --- 4. a truncated recording is skipped by analyze, rejected by run --------

def _truncated_wav(path, frames=16000):
    _talking_stereo(path, dur=frames / SR)
    full = os.path.getsize(str(path))
    # Chop the data chunk in half on a frame boundary; the header still declares
    # the full frame count.
    with open(str(path), "r+b") as fh:
        fh.truncate(full - (full - 44) // 4 * 2)  # drop ~half the samples cleanly
    return str(path)


def test_truncated_wav_rejected_by_run(tmp_path):
    p = _truncated_wav(tmp_path / "cut.wav")
    assert cli.main(["run", "--stereo", p]) == 2


def test_truncated_wav_skipped_by_analyze_with_reason(tmp_path):
    folder = tmp_path / "calls"
    folder.mkdir()
    _truncated_wav(folder / "cut.wav")
    from hotato import analyze as _analyze
    aggregate, _per = _analyze.analyze_folder(str(folder))
    assert aggregate["calls_skipped"] == 1
    reason = aggregate["skipped"][0]["reason"].lower()
    assert "truncated" in reason or "declares" in reason
    # it is NOT reported as a normal scanned call
    assert aggregate["calls_scanned"] == 0


# --- 5. non-dict vendor JSON -> ValueError, not AttributeError ---------------

class _JSONHandler(BaseHTTPRequestHandler):
    payload = b"[1, 2, 3]"

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, *a):
        pass


def _serve(payload):
    handler = type("H", (_JSONHandler,), {"payload": payload})
    srv = HTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def test_capture_vapi_non_dict_body_valueerror():
    srv, base = _serve(b"[1,2,3]")
    try:
        with pytest.raises(ValueError):
            cap.capture_vapi(call_id="abc", api_key="x", base_url=base)
    finally:
        srv.shutdown()


def test_capture_retell_non_dict_body_valueerror():
    srv, base = _serve(b'"a string body"')
    try:
        with pytest.raises(ValueError):
            cap.capture_retell(call_id="abc", api_key="x", base_url=base)
    finally:
        srv.shutdown()


def test_inspect_vapi_non_dict_body_valueerror():
    srv, base = _serve(b"[1,2,3]")
    try:
        with pytest.raises(ValueError):
            _inspect.inspect_vapi(assistant_id="abc", api_key="x", base_url=base)
    finally:
        srv.shutdown()


def test_inspect_retell_non_dict_body_valueerror():
    srv, base = _serve(b"null")
    try:
        with pytest.raises(ValueError):
            _inspect.inspect_retell(agent_id="abc", api_key="x", base_url=base)
    finally:
        srv.shutdown()


# --- 6. one bad adapter never aborts the whole pull -------------------------

def test_pull_skips_arbitrary_adapter_exception(tmp_path, monkeypatch):
    def boom(stack, ident, creds, out_path=None, *, allow_mono=False):
        raise AttributeError("'list' object has no attribute 'get'")

    monkeypatch.setattr(cap, "fetch_one", boom)
    out = cap.pull("vapi", {"api_key": "x"}, out_dir=str(tmp_path / "pulled"),
                   ids=["call-1", "call-2"])
    assert len(out["skipped"]) == 2
    assert out["pulled"] == []
    assert all("AttributeError" in s["reason"] for s in out["skipped"])


# --- 8. verdict dict missing 'passed' -> not_scorable, not KeyError ---------

def test_classify_pair_verdict_without_passed_is_not_scorable():
    before = {"event_id": "e1", "expected_yield": True, "verdict": {}}
    after = {"event_id": "e1", "expected_yield": True,
             "verdict": {"passed": True}}
    assert _compare.classify_pair(True, before, after) == "not_scorable"


def test_verify_verdict_without_passed_does_not_crash(tmp_path):
    before = {
        "tool": "hotato", "schema_version": "1", "mode": "single",
        "summary": {"events": 1, "passed": 0, "failed": 0},
        "events": [{"event_id": "e1", "expected_yield": True, "verdict": {}}],
    }
    after = {
        "tool": "hotato", "schema_version": "1", "mode": "single",
        "summary": {"events": 1, "passed": 1, "failed": 0},
        "events": [{"event_id": "e1", "expected_yield": True,
                    "verdict": {"passed": True, "talk_over_sec": 0.0,
                                "seconds_to_yield": 0.1}}],
    }
    bp = tmp_path / "before.json"
    ap = tmp_path / "after.json"
    bp.write_text(json.dumps(before))
    ap.write_text(json.dumps(after))
    result = _verify.verify_sides(str(bp), str(ap))  # must not raise KeyError
    assert result["results"]["not_scorable"] == 1


# --- 9. corrupt non-int loop 'run' -> clean error ---------------------------

def test_loop_state_non_int_run_is_valueerror(tmp_path):
    state = {
        "schema": "hotato.loop-state.v1", "root": None, "fixtures_dir": None,
        "stage": "awaiting_label", "created_at": "x", "updated_at": None,
        "run": "3", "discovery": None, "planning": None, "history": [],
    }
    p = tmp_path / "loop-state.json"
    p.write_text(json.dumps(state))
    with pytest.raises(ValueError):
        _loop.load_state(str(p))


def test_loop_cli_non_int_run_exit_2(tmp_path):
    d = tmp_path / ".hotato"
    d.mkdir()
    state = {
        "schema": "hotato.loop-state.v1", "root": None, "fixtures_dir": None,
        "stage": "awaiting_label", "created_at": "x", "updated_at": None,
        "run": "3", "discovery": None, "planning": None, "history": [],
    }
    (d / "loop-state.json").write_text(json.dumps(state))
    assert cli.main(["loop", str(tmp_path), "--state",
                     str(d / "loop-state.json")]) == 2


# --- 10. --out writes are atomic --------------------------------------------

def test_atomic_write_preserves_original_on_failure(tmp_path):
    target = tmp_path / "result.json"
    target.write_text('{"good": true}')

    class _Unserializable:
        pass

    with pytest.raises(TypeError):
        cli._atomic_write_json(str(target), {"x": _Unserializable()})
    # the previously-good file is untouched, and no temp turd is left behind
    assert json.loads(target.read_text()) == {"good": True}
    leftovers = [n for n in os.listdir(tmp_path) if n.startswith(".hotato-tmp-")]
    assert leftovers == []


def test_atomic_write_replaces_content(tmp_path):
    target = tmp_path / "out.json"
    cli._atomic_write_json(str(target), {"a": 1})
    cli._atomic_write_json(str(target), {"a": 2})
    assert json.loads(target.read_text()) == {"a": 2}
    assert not any(n.startswith(".hotato-tmp-") for n in os.listdir(tmp_path))


# --- 11. team output is independent of filesystem mtimes --------------------

def _run_envelope(passed, total, fc="early_yield"):
    return {
        "tool": "hotato", "kind": "run", "schema_version": "1",
        "summary": {"events": total, "passed": passed, "failed": total - passed},
        "events": [
            {
                "scenario_id": f"s{i}",
                "verdict": {"passed": i < passed, "talk_over_sec": 0.1 * i,
                            "seconds_to_yield": 0.2 * i},
                **({"fix": {"fix_class": fc}} if i >= passed else {}),
            }
            for i in range(total)
        ],
    }


def test_team_aggregate_ignores_mtime(tmp_path):
    d = tmp_path / "team"
    d.mkdir()
    (d / "run_A.json").write_text(json.dumps(_run_envelope(8, 10)))
    (d / "run_B.json").write_text(json.dumps(_run_envelope(3, 10, "late_yield")))

    def _agg():
        loaded = _aggregate.load_run_dir(str(d))
        return json.dumps(
            _aggregate.aggregate_runs(loaded["runs"], order=loaded["order"],
                                      skipped=loaded["skipped"]),
            sort_keys=True,
        )

    first = _agg()
    os.utime(d / "run_A.json", (0, 0))
    os.utime(d / "run_B.json", (10 ** 9, 10 ** 9))
    assert _agg() == first  # same bytes in, same aggregate out


# --- 12. a not-scorable event never deflates the pass RATE ------------------

def test_pass_rate_excludes_not_scorable_cli(capsys):
    env = {
        "mode": "single", "stack": "generic", "offline": True,
        "summary": {"events": 2, "passed": 1, "failed": 0, "not_scorable": 1},
        "events": [
            {"event_id": "ok", "verdict": {"passed": True, "did_yield": True,
                                           "talk_over_sec": 0.0,
                                           "seconds_to_yield": 0.1}},
            {"event_id": "bad", "scorable": False, "verdict": {},
             "not_scorable_reason": "no caller speech"},
        ],
        "exit_code": 0,
    }
    cli._emit(env, "text")
    out = capsys.readouterr().out
    # N passing + M not_scorable reports N/N (100%), never N/(N+M)
    assert "1/1 events pass" in out
    assert "1/2 events pass" not in out


def test_pass_rate_excludes_not_scorable_aggregate(tmp_path):
    d = tmp_path / "runs"
    d.mkdir()
    env = {
        "tool": "hotato", "kind": "run", "schema_version": "1",
        "summary": {"events": 2, "passed": 1, "failed": 0, "not_scorable": 1},
        "events": [
            {"scenario_id": "ok", "verdict": {"passed": True}},
            {"scenario_id": "bad", "scorable": False,
             "verdict": {}, "not_scorable_reason": "no caller speech"},
        ],
    }
    (d / "001.json").write_text(json.dumps(env))
    (d / "002.json").write_text(json.dumps(env))
    loaded = _aggregate.load_run_dir(str(d))
    agg = _aggregate.aggregate_runs(loaded["runs"], order=loaded["order"])
    point = agg["pass_rate_over_time"][0]
    assert point["passed"] == 1
    assert point["failed"] == 0  # the not-scorable event is NOT a failure
    assert point["pass_rate"] == 1.0  # 1/1 scorable, not 1/2


# --- 13. capture download refuses file:// / non-http(s) URLs ----------------

@pytest.mark.parametrize("bad", [
    "file:///etc/passwd",
    "file:///home/user/.hotato/connections.json",
    "data:text/plain;base64,AAAA",
    "ftp://host/recording.wav",
])
def test_download_refuses_non_http_urls(tmp_path, bad):
    with pytest.raises(ValueError):
        cap._download(bad, str(tmp_path / "out.wav"))


def test_capture_vapi_refuses_file_url_from_response(tmp_path):
    secret = tmp_path / "secret.json"
    secret.write_text('{"api_key": "super-secret"}')
    payload = json.dumps(
        {"artifact": {"recording": {"stereoUrl": f"file://{secret}"}}}
    ).encode()
    srv, base = _serve(payload)
    try:
        with pytest.raises(ValueError):
            cap.capture_vapi(call_id="evil", api_key="x", base_url=base,
                             out_path=str(tmp_path / "pulled.wav"))
    finally:
        srv.shutdown()
    # the local secret was never exfiltrated into a "recording"
    assert not (tmp_path / "pulled.wav").exists()


# --- 14. MCP report_path is sandboxed by default ----------------------------

def test_mcp_report_path_default_sandbox_refuses_outside_tempdir(monkeypatch):
    monkeypatch.delenv("HOTATO_MCP_REPORT_DIR", raising=False)
    import tempfile
    tmpdir = os.path.realpath(tempfile.gettempdir())
    cwd = os.path.realpath(os.getcwd())
    if os.path.commonpath([cwd, tmpdir]) == tmpdir:
        pytest.skip("working directory is itself under the OS temp dir")
    # A path outside the OS temp dir (the repo working directory) must be refused
    # when no explicit HOTATO_MCP_REPORT_DIR is configured.
    outside = os.path.join(cwd, "poc-arbitrary-write.html")
    with pytest.raises(ValueError):
        mcp_server._guard_report_path(outside)
    assert not os.path.exists(outside)


def test_mcp_report_path_default_sandbox_allows_tempdir(monkeypatch):
    monkeypatch.delenv("HOTATO_MCP_REPORT_DIR", raising=False)
    import tempfile
    inside = os.path.join(tempfile.gettempdir(), "hotato-ok-report.html")
    # a fresh (nonexistent) temp path is accepted
    if os.path.exists(inside):
        os.unlink(inside)
    assert mcp_server._guard_report_path(inside) == inside
