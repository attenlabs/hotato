"""Regression tests for FIX round 1.

Each test pins one confirmed blocker/major defect so it cannot silently return:

  * onset NaN / +-Inf / past-the-end are refused as a clean usage error (exit 2),
    never a fabricated verdict, an OverflowError traceback, or a silent clamp;
  * the OSError family (a directory / an unreadable / an already-existing file
    input) is the shared handled-error contract -> clean exit 2, not a raw crash;
  * caller_onset_sec is null (never a fabricated -1.0) when no onset was
    detected, in the envelope AND in the export CSV;
  * `hotato patch` structurally validates a plan (no raw KeyError);
  * `hotato verify` / compare degrade to not_scorable on a verdict-less side,
    never KeyError('verdict');
  * a MEASURED self-echo routes to the audio-routing (config) fix and never to
    the engagement-control pointer, and never fires the both-axes funnel;
  * `hotato ingest` refuses non-http(s) recording_url (SSRF / local-file read)
    and confines recording_path to HOTATO_INGEST_DIR;
  * `hotato patch` shell-quotes its paste-ready curl and refuses a non-id target;
  * capture never sends the vendor API key to an off-domain recording_url;
  * the MCP report_path never overwrites a non-hotato file / escapes its dir.
"""

import json
import math
import os
import struct
import sys
import urllib.request
import wave

import pytest

from hotato import capture as cap
from hotato import cli
from hotato import compare as _compare
from hotato import ingest as ing
from hotato import mcp_server
from hotato import verify as _verify
from hotato.core import run_single
from hotato.export import run_export
from hotato.fixmap import classify_event, systemic_pointer
from hotato.patch import build_patch


SR = 16000


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
    """Agent talks throughout; caller says something 1.5-2.5s -> scorable."""
    n = int(dur * SR)
    agent = _tone(n, (0.0, dur), 330.0)
    caller = _tone(n, (1.5, 2.5), 220.0)
    return _write_stereo(path, caller, agent)


def _silent_stereo(path, dur=2.0):
    n = int(dur * SR)
    zeros = [0.0] * n
    return _write_stereo(path, zeros, zeros)


def _bleed_stereo(path, dur=4.0, agent_seg=(0.2, 2.0), delay=0.12, gain=0.35):
    """Caller channel is ONLY a delayed, attenuated copy of the agent -> pure
    self-echo; the agent stops while the echo tail plays, so the scorer sees a
    yield. echo_suspected is measured true."""
    n = int(dur * SR)
    agent = _tone(n, agent_seg, 330.0)
    d = int(delay * SR)
    caller = [0.0] * n
    for i in range(n):
        j = i - d
        if 0 <= j < n:
            caller[i] = gain * agent[j]
    return _write_stereo(path, caller, agent)


# --- defect: onset NaN / Inf / out-of-range ---------------------------------

def test_onset_nan_is_clean_usage_error(tmp_path):
    wav = _talking_stereo(tmp_path / "c.wav")
    with pytest.raises(ValueError, match="finite"):
        run_single(stereo=wav, onset_sec=float("nan"), expect="yield")


def test_onset_inf_is_clean_usage_error_no_overflow(tmp_path):
    wav = _talking_stereo(tmp_path / "c.wav")
    with pytest.raises(ValueError, match="finite"):
        run_single(stereo=wav, onset_sec=float("inf"), expect="yield")


def test_onset_past_end_is_refused(tmp_path):
    wav = _talking_stereo(tmp_path / "c.wav", dur=4.0)
    with pytest.raises(ValueError, match="beyond the end"):
        run_single(stereo=wav, onset_sec=999999.0, expect="yield")


def test_cli_onset_nan_exits_2_with_json_error(tmp_path, capsys):
    wav = _talking_stereo(tmp_path / "c.wav")
    code = cli.main(["run", "--stereo", wav, "--onset", "nan",
                     "--format", "json", "--expect", "yield"])
    assert code == 2
    obj = json.loads(capsys.readouterr().out)
    assert obj["ok"] is False
    assert obj["exit_code"] == 2


def test_cli_onset_inf_exits_2_no_traceback(tmp_path, capsys):
    wav = _talking_stereo(tmp_path / "c.wav")
    code = cli.main(["run", "--stereo", wav, "--onset", "inf",
                     "--format", "json", "--expect", "yield"])
    assert code == 2
    # a structured error, not a Python traceback on stderr
    assert "Traceback" not in capsys.readouterr().err


# --- defect: OSError family (dir / unreadable / existing --out) -------------

def test_cli_run_on_directory_exits_2(tmp_path, capsys):
    d = tmp_path / "adir"
    d.mkdir()
    code = cli.main(["run", "--stereo", str(d), "--format", "json"])
    assert code == 2
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="chmod(0) does not block reads under Windows' ACL-based permission "
           "model the way it does on POSIX; this pins POSIX unreadable-file "
           "handling specifically",
)
@pytest.mark.skipif(sys.platform != "win32" and os.geteuid() == 0,
                     reason="root bypasses file permissions")
def test_cli_run_on_unreadable_file_exits_2(tmp_path, capsys):
    wav = _talking_stereo(tmp_path / "noperm.wav")
    os.chmod(wav, 0o000)
    try:
        code = cli.main(["run", "--stereo", wav, "--format", "json"])
    finally:
        os.chmod(wav, 0o644)
    assert code == 2
    assert "Traceback" not in capsys.readouterr().err


def test_cli_patch_on_directory_exits_2(tmp_path, capsys):
    d = tmp_path / "adir"
    d.mkdir()
    code = cli.main(["patch", str(d), "--format", "json"])
    assert code == 2
    assert "Traceback" not in capsys.readouterr().err


# --- defect: caller_onset_sec null, never fabricated -1.0 -------------------

def test_no_onset_reports_null_not_minus_one(tmp_path):
    wav = _silent_stereo(tmp_path / "silent.wav")
    ev = run_single(stereo=wav, expect="yield")["events"][0]
    assert ev.get("scorable") is False
    assert ev["measurements"]["caller_onset_sec"] is None


def test_export_csv_has_blank_onset_not_minus_one(tmp_path):
    wav = _silent_stereo(tmp_path / "silent.wav")
    out = tmp_path / "export"
    manifest = run_export(out_dir=str(out), stereo=wav, expect="yield")
    events_csv = (out / "events.csv").read_text(encoding="utf-8")
    assert "-1.0" not in events_csv
    # the not-scorable event's onset measurement is null in the envelope too
    assert manifest["env"]["events"][0]["measurements"]["caller_onset_sec"] is None


# --- defect: patch structural validation (no raw KeyError) ------------------

def test_patch_missing_target_is_clean_error():
    with pytest.raises(ValueError, match="target"):
        build_patch({"schema": "hotato.fixplan.v1"})


def test_patch_propose_with_empty_change_is_clean_error():
    plan = {
        "schema": "hotato.fixplan.v1",
        "target": {"stack": "vapi"},
        "decision": "propose_one_step",
        "changes": [{}],
    }
    with pytest.raises(ValueError, match="field"):
        build_patch(plan)


# --- defect: verify/compare on a verdict-less side -> not_scorable ----------

def _envelope(events):
    return {
        "tool": "hotato",
        "schema_version": "1",
        "mode": "suite",
        "stack": "generic",
        "events": events,
        "summary": {"events": len(events), "passed": 0, "failed": 0,
                    "regression": False},
    }


def test_compare_classify_pair_no_verdict_is_not_scorable():
    before = {"event_id": "e1", "scenario_id": "s1", "expected_yield": True,
              "scorable": True}  # NOTE: no verdict key
    after = {"event_id": "e1", "scenario_id": "s1", "expected_yield": True,
             "scorable": True, "verdict": {"passed": True, "did_yield": True,
                                           "talk_over_sec": 0.0,
                                           "seconds_to_yield": 0.3}}
    assert _compare.classify_pair(True, before, after) == "not_scorable"


def test_verify_sides_no_verdict_does_not_crash(tmp_path):
    before_dir = tmp_path / "before"
    after_dir = tmp_path / "after"
    before_dir.mkdir()
    after_dir.mkdir()
    (before_dir / "b.json").write_text(json.dumps(_envelope([
        {"event_id": "e1", "scenario_id": "s1", "expected_yield": True,
         "scorable": True}])), encoding="utf-8")
    (after_dir / "a.json").write_text(json.dumps(_envelope([
        {"event_id": "e1", "scenario_id": "s1", "expected_yield": True,
         "scorable": True,
         "verdict": {"passed": True, "did_yield": True, "talk_over_sec": 0.0,
                     "seconds_to_yield": 0.3}}])), encoding="utf-8")
    v = _verify.verify_sides(str(before_dir), str(after_dir), min_n=1)
    assert v["results"]["not_scorable"] == 1


# --- defect: measured self-echo -> config fix, never engagement-control -----

def test_classify_event_echo_suspected_routes_to_config():
    fix = classify_event(
        expected_yield=False, did_yield=True,
        reasons=["expected the agent to keep the floor but it yielded"],
        stack="vapi", echo_suspected=True,  # no scenario_id, no tags
    )
    assert fix["fix_class"] == "config"
    assert fix["pointer"] is None
    assert "self-interruption" in fix["title"].lower()
    # the fabricated caller-intent wording must be gone
    assert "mhm" not in fix["detail"].lower()
    assert "listening" not in fix["detail"].lower()


def test_real_self_echo_call_routes_to_config_not_saa(tmp_path):
    wav = _bleed_stereo(tmp_path / "bleed.wav")
    ev = run_single(stereo=wav, onset_sec=2.0, expect="hold", stack="vapi")["events"][0]
    assert ev["signals"]["echo"]["echo_suspected"] is True
    assert ev["verdict"]["did_yield"] is True
    assert ev["verdict"]["passed"] is False
    assert ev["fix"]["fix_class"] == "config"
    assert ev["fix"]["pointer"] is None
    assert "listening" not in ev["fix"]["detail"].lower()


def test_funnel_excludes_measured_echo_false_barge():
    echo_signals = {"echo": {"coherence": 1.0, "lag_sec": 0.12,
                             "echo_suspected": True}}
    missed = {"scenario_id": "x-missed", "expected_yield": True,
              "verdict": {"passed": False, "did_yield": False}}
    echo_false_barge = {"scenario_id": "some-backchannel", "expected_yield": False,
                        "verdict": {"passed": False, "did_yield": True},
                        "signals": echo_signals}
    # a measured self-echo is NOT the discrimination case -> no funnel
    assert systemic_pointer([missed, echo_false_barge]) is None
    # a genuine (non-echo) backchannel false-barge DOES fire the funnel
    real_backchannel = {"scenario_id": "some-backchannel", "expected_yield": False,
                        "verdict": {"passed": False, "did_yield": True},
                        "signals": {"echo": {"coherence": 0.0, "lag_sec": 0.0,
                                             "echo_suspected": False}}}
    assert systemic_pointer([missed, real_backchannel]) is not None


# --- defect: ingest SSRF / local-file read ----------------------------------

def _write_event(tmp_path, name, obj):
    p = tmp_path / name
    p.write_text(json.dumps(obj), encoding="utf-8")
    return str(p)


def test_ingest_rejects_file_scheme_recording_url(tmp_path):
    ev = _write_event(tmp_path, "lk.json", {"recording_url": "file:///etc/hostname"})
    with pytest.raises(ing.IngestError, match="scheme"):
        ing.run_ingest("pipecat", event=ev)


def test_ingest_rejects_data_scheme_recording_url(tmp_path):
    ev = _write_event(tmp_path, "lk.json",
                      {"recording_url": "data:text/plain;base64,AAAA"})
    with pytest.raises(ing.IngestError, match="scheme"):
        ing.run_ingest("pipecat", event=ev)


def test_validate_recording_url_blocks_file_and_allows_https():
    with pytest.raises(ing.IngestError):
        ing._validate_recording_url("file:///etc/passwd", "pipecat")
    assert ing._validate_recording_url(
        "https://storage.test/x.wav", "pipecat") == "https://storage.test/x.wav"


# --- defect: SSRF -- private/link-local/loopback IP block (default posture) --

import pytest as _pytest


@_pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
    "http://127.0.0.1:6379/",
    "http://10.0.0.5/rec.wav",
    "http://192.168.1.10/rec.wav",
    "http://[::1]/rec.wav",
])
def test_capture_download_url_blocks_internal_ips_by_default(url, monkeypatch):
    """SSRF default-deny: a vendor-response download URL that resolves to a
    loopback/private/link-local (cloud-metadata) address is refused BEFORE any
    fetch, with no HOTATO_INGEST_ALLOWED_HOSTS configured. IP literals resolve to
    themselves, so no DNS is needed."""
    monkeypatch.delenv("HOTATO_ALLOW_PRIVATE_URLS", raising=False)
    monkeypatch.delenv("HOTATO_INGEST_ALLOWED_HOSTS", raising=False)
    with pytest.raises(ValueError, match="non-public|SSRF|private|metadata"):
        cap._validate_download_url(url)


@_pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",
    "http://127.0.0.1:6379/",
    "http://10.1.2.3/x.wav",
])
def test_ingest_recording_url_blocks_internal_ips_by_default(url, monkeypatch):
    """Same default-deny SSRF block on the untrusted webhook recording_url path
    (the livekit/pipecat blind-SSRF primitive)."""
    monkeypatch.delenv("HOTATO_ALLOW_PRIVATE_URLS", raising=False)
    monkeypatch.delenv("HOTATO_INGEST_ALLOWED_HOSTS", raising=False)
    with pytest.raises(ing.IngestError, match="non-public|SSRF|private|metadata"):
        ing._validate_recording_url(url, "livekit")


def test_ssrf_block_catches_hostname_resolving_to_private_ip(monkeypatch):
    """DNS-based SSRF: a public-looking hostname that RESOLVES to an internal IP
    is refused (the check inspects resolved addresses, not just the literal)."""
    monkeypatch.delenv("HOTATO_ALLOW_PRIVATE_URLS", raising=False)
    monkeypatch.setattr(cap, "_resolve_host_addresses",
                        lambda h: ["169.254.169.254"])
    with pytest.raises(ValueError, match="non-public"):
        cap._validate_download_url("https://totally-legit-cdn.example/rec.wav")


def test_ssrf_block_allows_public_ip_and_is_opt_outable(monkeypatch):
    """A public address is accepted; the operator can opt into private hosts."""
    monkeypatch.delenv("HOTATO_ALLOW_PRIVATE_URLS", raising=False)
    assert cap._validate_download_url("http://1.1.1.1/rec.wav") == \
        "http://1.1.1.1/rec.wav"
    # explicit opt-out lever restores internal-host access (local test server)
    monkeypatch.setenv("HOTATO_ALLOW_PRIVATE_URLS", "1")
    assert cap._validate_download_url("http://127.0.0.1:8080/rec.wav") == \
        "http://127.0.0.1:8080/rec.wav"


def test_ingest_confines_recording_path_to_configured_dir(tmp_path, monkeypatch):
    base = tmp_path / "egress"
    base.mkdir()
    monkeypatch.setenv("HOTATO_INGEST_DIR", str(base))
    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"x")
    ev = _write_event(tmp_path, "pc.json", {"recording_path": str(outside)})
    with pytest.raises(ing.IngestError, match="outside"):
        ing.run_ingest("pipecat", event=ev)


# --- defect: patch curl shell-injection -------------------------------------

def _propose_plan(stack, assistant_id, to):
    return {
        "schema": "hotato.fixplan.v1",
        "target": {"stack": stack, "assistant_id": assistant_id},
        "decision": "propose_one_step",
        "finding": "false_stop_on_backchannel",
        "changes": [{
            "field": "stopSpeakingPlan.numWords",
            "direction": "raise",
            "from": 1,
            "to": to,
            "bounds": "1..10",
            "risk": "low",
        }],
    }


def test_patch_refuses_injected_assistant_id():
    plan = _propose_plan("vapi", "abc; touch /tmp/pwned #", 3)
    with pytest.raises(ValueError, match="valid platform id"):
        build_patch(plan)


def test_patch_curl_shell_quotes_the_body():
    import shlex
    from hotato.patch import _nest
    malicious = "3'; touch /tmp/pwned; echo 'done"
    plan = _propose_plan("vapi", "assistant-123", malicious)
    out = build_patch(plan)
    curl = out["artifact"]["curl"]
    assert curl is not None
    body = _nest("stopSpeakingPlan.numWords".split("."), malicious)
    expected = shlex.quote(json.dumps(body, sort_keys=True))
    assert expected in curl  # the body is a single, safely-quoted shell token


# --- defect: capture never sends the API key off-domain ---------------------

class _Resp:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _mono_bytes():
    frames = bytearray()
    for i in range(1600):
        frames += struct.pack("<h", 0)
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def _install(monkeypatch, routes, captured):
    def fake_urlopen(req, timeout=None):
        url = req.full_url
        captured.append(req)
        for key, payload in routes.items():
            if key in url:
                return _Resp(payload)
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


def test_capture_bland_drops_key_on_offdomain_recording_url(tmp_path, monkeypatch):
    mono = _mono_bytes()
    routes = {
        "/v1/calls/b1": json.dumps({"recording_url": "https://evil.test/steal"}).encode(),
        "evil.test/steal": mono,
    }
    captured = []
    _install(monkeypatch, routes, captured)
    cap.capture_bland(call_id="b1", api_key="SECRET", out_path=str(tmp_path / "o.wav"))
    dl = [r for r in captured if "evil.test/steal" in r.full_url][0]
    assert dl.get_header("Authorization") is None  # key NOT leaked off-domain


def test_capture_bland_sends_key_on_same_host(tmp_path, monkeypatch):
    mono = _mono_bytes()
    routes = {
        "/v1/calls/b1": json.dumps(
            {"recording_url": "https://api.bland.ai/rec/b1.wav"}).encode(),
        "api.bland.ai/rec/b1.wav": mono,
    }
    captured = []
    _install(monkeypatch, routes, captured)
    cap.capture_bland(call_id="b1", api_key="SECRET", out_path=str(tmp_path / "o.wav"))
    dl = [r for r in captured if "/rec/b1.wav" in r.full_url][0]
    assert dl.get_header("Authorization") == "SECRET"  # same host -> auth kept


def test_capture_millis_drops_key_on_offdomain_recording_url(tmp_path, monkeypatch):
    mono = _mono_bytes()
    routes = {
        "/call-logs/m1": json.dumps(
            {"recording": {"recording_url": "https://evil.test/steal"}}).encode(),
        "evil.test/steal": mono,
    }
    captured = []
    _install(monkeypatch, routes, captured)
    cap.capture_millis(session_id="m1", api_key="SECRET",
                       out_path=str(tmp_path / "o.wav"))
    dl = [r for r in captured if "evil.test/steal" in r.full_url][0]
    assert dl.get_header("Authorization") is None


# --- defect: MCP report_path arbitrary overwrite ----------------------------

def test_mcp_report_path_refuses_non_hotato_overwrite(tmp_path):
    wav = _talking_stereo(tmp_path / "c.wav")
    victim = tmp_path / "secret.txt"
    victim.write_text("ssh-rsa AAAA... important", encoding="utf-8")
    out = mcp_server._run_tool(stereo=wav, stack="generic", expect="yield",
                               report_path=str(victim))
    assert out["ok"] is False
    # the victim file was NOT overwritten
    assert victim.read_text(encoding="utf-8") == "ssh-rsa AAAA... important"


def test_mcp_report_path_confined_to_report_dir(tmp_path, monkeypatch):
    wav = _talking_stereo(tmp_path / "c.wav")
    reports = tmp_path / "reports"
    reports.mkdir()
    monkeypatch.setenv("HOTATO_MCP_REPORT_DIR", str(reports))
    escape = tmp_path / "escape.html"
    out = mcp_server._run_tool(stereo=wav, stack="generic", expect="yield",
                               report_path=str(escape))
    assert out["ok"] is False
    assert not escape.exists()
    # inside the configured dir it works
    ok = mcp_server._run_tool(stereo=wav, stack="generic", expect="yield",
                              report_path=str(reports / "r.html"))
    assert ok.get("ok") is not False
    assert (reports / "r.html").exists()


# --- defect: MCP input paths (stereo/caller/agent) unsandboxed ---------------

def test_mcp_input_path_refuses_arbitrary_absolute_path(monkeypatch):
    """Regression: stereo/caller/agent had NO sandbox, unlike report_path. An
    LLM tool-caller (or untrusted content steering it) could point the tool at
    any readable file on the host and get a spoken-timeline disclosure. The input
    path must now be confined, returning the shared structured error, not a read."""
    monkeypatch.delenv("HOTATO_MCP_INPUT_DIR", raising=False)
    out = mcp_server._run_tool(stereo="/etc/hostname", expect="yield")
    assert out["ok"] is False
    assert "sandbox" in out["message"].lower() or "refusing to read" in out["message"].lower()


def test_mcp_input_path_confined_to_input_dir(tmp_path, monkeypatch):
    """When HOTATO_MCP_INPUT_DIR is set, only paths inside it are scorable."""
    allowed = tmp_path / "inbox"
    allowed.mkdir()
    wav = _talking_stereo(allowed / "c.wav")
    monkeypatch.setenv("HOTATO_MCP_INPUT_DIR", str(allowed))
    # a real WAV outside the configured dir is refused
    outside = _talking_stereo(tmp_path / "outside.wav")
    out = mcp_server._run_tool(stereo=outside, expect="yield")
    assert out["ok"] is False
    # inside the configured dir it scores normally
    ok = mcp_server._run_tool(stereo=wav, expect="yield")
    assert ok.get("ok") is not False


def test_mcp_guard_input_path_rejects_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv("HOTATO_MCP_INPUT_DIR", str(tmp_path / "inbox"))
    (tmp_path / "inbox").mkdir()
    with pytest.raises(ValueError):
        mcp_server._guard_input_path(
            str(tmp_path / "inbox" / ".." / "escape.wav"), "stereo")
