"""Round-4 hardening regressions.

Each test reproduces a confirmed defect and pins the fixed contract:

  1. identical caller/agent channels are refused everywhere (never a confident,
     meaningless verdict; never a bogus regression fixture);
  2. ``hotato team --format json`` with <2 runs emits JSON (not a sentence) and
     surfaces WHY each file was rejected;
  3. analyze / loop / sweep validate global scan flags up front (a bad
     --min-gap or channel is exit 2, not a false clean "found nothing");
  4. ``hotato sweep`` writes its dashboard atomically;
  5. a LONE backchannel false-stop routes to a CONFIG fix, not the
     engagement-control pointer (only the both-axes battery keeps the pointer);
  7. a path-traversal scenario id is refused before any file is opened;
  8. mono/unclear-channel stacks always score degraded/indicative, even when the
     download happens to be 2-channel;
 10. ``_http_get_json`` turns a non-JSON 200 body into a clean, named ValueError.
"""

import io
import json
import struct
import threading
import wave
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib import resources

import pytest

from hotato import cli
from hotato.core import run_single, run_suite


def _bundled(sid):
    return str(resources.files("hotato").joinpath("data", "audio", sid + ".example.wav"))


def _write_stereo(path, n_channels=2, sample_rate=16000, n_frames=1600, value=1200):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(n_channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(
            struct.pack("<" + "h" * (n_frames * n_channels),
                        *([value] * (n_frames * n_channels)))
        )
    return str(path)


# --- defect 1: identical caller/agent channels are refused ------------------

def test_run_refuses_identical_channels():
    assert cli.main(["run", "--stereo", _bundled("01-hard-interruption"),
                     "--caller-channel", "0", "--agent-channel", "0"]) == 2


def test_scan_refuses_identical_channels():
    assert cli.main(["scan", "--stereo", _bundled("01-hard-interruption"),
                     "--caller-channel", "1", "--agent-channel", "1"]) == 2


def test_compare_refuses_identical_channels():
    b = _bundled("01-hard-interruption")
    assert cli.main(["compare", "--before", b, "--after", b, "--onset", "2.4",
                     "--expect", "yield",
                     "--caller-channel", "0", "--agent-channel", "0"]) == 2


def test_fixture_create_refuses_identical_channels(tmp_path):
    """The worst case: identical channels must never mint a fixture that
    passes/fails the battery forever by comparing a channel against itself."""
    out = tmp_path / "fx"
    assert cli.main(["fixture", "create", "--stereo",
                     _bundled("01-hard-interruption"), "--id", "samechan",
                     "--onset", "2.4", "--expect", "yield", "--out", str(out),
                     "--caller-channel", "0", "--agent-channel", "0"]) == 2
    # And nothing was written.
    assert not (out / "scenarios" / "samechan.json").exists()


def test_analyze_refuses_identical_channels(tmp_path):
    folder = tmp_path / "calls"
    folder.mkdir()
    _write_stereo(folder / "a.wav")
    assert cli.main(["analyze", str(folder),
                     "--caller-channel", "0", "--agent-channel", "0"]) == 2


# --- defect 2: team --format json with <2 runs ------------------------------

def test_team_json_with_too_few_runs_is_valid_json_with_reasons(tmp_path, capsys):
    (tmp_path / "run1.json").write_text("garbage not json")
    (tmp_path / "run2.json").write_text('{"tool":"hotato"}')
    code = cli.main(["team", str(tmp_path), "--format", "json"])
    assert code == 0
    out = capsys.readouterr().out
    doc = json.loads(out)  # must parse; the bug printed a plain sentence
    assert doc["kind"] == "team"
    assert doc["runs_found"] == 0
    reasons = {s["file"]: s["why"] for s in doc["skipped"]}
    assert "run1.json" in reasons and "unreadable JSON" in reasons["run1.json"]
    assert "run2.json" in reasons and "not a hotato run envelope" in reasons["run2.json"]


def test_team_text_with_too_few_runs_lists_skip_reasons(tmp_path, capsys):
    (tmp_path / "a.json").write_text("nope")
    code = cli.main(["team", str(tmp_path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "a.json" in out and "unreadable JSON" in out


# --- defect 3: analyze / loop validate global scan flags up front -----------

def _folder_of_good_wavs(tmp_path):
    folder = tmp_path / "recordings"
    folder.mkdir()
    for name in ("one.wav", "two.wav"):
        _write_stereo(folder / name)
    return folder


def test_analyze_bad_min_gap_is_exit_2(tmp_path):
    folder = _folder_of_good_wavs(tmp_path)
    assert cli.main(["analyze", str(folder), "--min-gap", "-1",
                     "--format", "json"]) == 2


def test_analyze_out_of_range_channel_is_exit_2(tmp_path):
    """A channel index out of range for every file is a GLOBAL flag error, not a
    per-file skip that degrades into 'found nothing'."""
    folder = _folder_of_good_wavs(tmp_path)
    assert cli.main(["analyze", str(folder), "--caller-channel", "5",
                     "--format", "json"]) == 2


def test_loop_bad_min_gap_is_exit_2(tmp_path):
    folder = _folder_of_good_wavs(tmp_path)
    state = tmp_path / "loop.json"
    assert cli.main(["loop", str(folder), "--min-gap", "-5", "--state",
                     str(state), "--format", "json"]) == 2


def test_loop_bad_min_gap_is_exit_2_even_with_prior_state(tmp_path):
    """The validation must fire regardless of the persisted loop stage."""
    folder = _folder_of_good_wavs(tmp_path)
    state = tmp_path / "loop.json"
    assert cli.main(["loop", str(folder), "--state", str(state),
                     "--format", "json"]) == 0  # a valid first run
    # Now a typo'd flag on a subsequent run must still be exit 2, not a
    # from-memory "nothing to fix".
    assert cli.main(["loop", str(folder), "--min-gap", "-5", "--state",
                     str(state), "--format", "json"]) == 2


def test_analyze_good_channels_still_works(tmp_path):
    folder = _folder_of_good_wavs(tmp_path)
    assert cli.main(["analyze", str(folder), "--format", "json"]) == 0


# --- defect 4: sweep dashboard is written atomically ------------------------

def test_capture_atomic_write_preserves_previous_file_on_failure(tmp_path):
    from hotato import capture

    target = tmp_path / "report.html"
    target.write_text("PREVIOUS GOOD REPORT")

    class Boom:
        def __str__(self):
            raise RuntimeError("kill mid-write")

    # A write that blows up while producing bytes must leave the previous file
    # intact (temp-file + os.replace), never a truncated/corrupt target.
    with pytest.raises(RuntimeError):
        capture._atomic_write_text(str(target), "".join(["ok", str(Boom())]))
    assert target.read_text() == "PREVIOUS GOOD REPORT"
    # No leftover temp files in the directory.
    assert [p.name for p in tmp_path.iterdir()] == ["report.html"]


def test_capture_atomic_write_replaces_completely(tmp_path):
    from hotato import capture

    target = tmp_path / "r.html"
    target.write_text("old")
    capture._atomic_write_text(str(target), "brand new complete contents")
    assert target.read_text() == "brand new complete contents"


# --- defect 5: a lone backchannel false-stop is config, not the pointer -----

def test_lone_backchannel_false_stop_routes_to_config():
    """`hotato run` on a single should-hold recording that yields: the funnel
    cannot fire (one event), so the fix must be CONFIG (raise words-to-interrupt),
    NOT the engagement-control pointer."""
    fd02 = _bundled_funnel("fd-02-backchannel-yielded")
    env = run_single(stereo=fd02, expect="hold", stack="vapi")
    e = env["events"][0]
    assert e["verdict"]["passed"] is False
    assert env["funnel"] is None
    assert e["fix"]["fix_class"] == "config"
    assert e["fix"]["knob"] is not None          # a concrete dial is offered
    assert e["fix"]["pointer"] is None           # no engagement-control pointer
    # and the fix_map mirrors it
    assert env["fix_map"][0]["fix_class"] == "config"


def test_both_axes_battery_still_fires_the_pointer():
    """The correctly-gated case is preserved: a battery failing on BOTH axes
    keeps the engagement-control fix + funnel."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    scen = os.path.join(root, "examples", "funnel-demo", "scenarios")
    aud = os.path.join(root, "examples", "funnel-demo", "audio")
    env = run_suite(suite="barge-in", scenarios_dir=scen, audio_dir=aud,
                    stack="vapi")
    assert env["funnel"] is not None
    classes = {f["fix_class"] for f in env["fix_map"]}
    assert "engagement-control" in classes


def _bundled_funnel(sid):
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "examples", "funnel-demo", "audio", sid + ".example.wav")


def test_downgrade_helper_is_a_noop_on_config_fix():
    from hotato.fixmap import downgrade_lone_engagement_fix

    ev = {"fix": {"fix_class": "config", "knob": {"x": 1}, "pointer": None,
                  "title": "t", "detail": "d"}}
    before = dict(ev["fix"])
    downgrade_lone_engagement_fix(ev, "vapi")
    assert ev["fix"] == before  # unchanged


# --- defect 7: path traversal via scenario id -------------------------------

def test_run_suite_refuses_traversal_scenario_id(tmp_path):
    """A scenarios pack with an id that traverses out of --audio must be refused
    before any file outside --audio is read (or embedded into a report)."""
    outside = tmp_path / "outside"
    outside.mkdir()
    _write_stereo(outside / "leaked.example.wav")
    audio = tmp_path / "audio"
    audio.mkdir()
    _write_stereo(audio / "safe.example.wav")
    scen = tmp_path / "scenarios"
    scen.mkdir()
    (scen / "evil.json").write_text(json.dumps({
        "id": "../outside/leaked", "title": "x", "category": "should_yield",
        "expected": {"yield": True}, "caller_onset_sec": 1.0,
    }))
    with pytest.raises(ValueError):
        run_suite(suite="barge-in", scenarios_dir=str(scen), audio_dir=str(audio))


def test_run_suite_refuses_absolute_scenario_id(tmp_path):
    audio = tmp_path / "audio"
    audio.mkdir()
    scen = tmp_path / "scenarios"
    scen.mkdir()
    (scen / "abs.json").write_text(json.dumps({
        "id": "/etc/passwd", "title": "x", "category": "should_yield",
        "expected": {"yield": True}, "caller_onset_sec": 1.0,
    }))
    with pytest.raises(ValueError):
        run_suite(suite="barge-in", scenarios_dir=str(scen), audio_dir=str(audio))


def test_safe_scenario_id_accepts_normal_slugs():
    from hotato.core import _safe_scenario_id

    for ok in ("bc-01-repeated", "fd_02", "05-telephony-8khz", "call_abc123"):
        assert _safe_scenario_id(ok) == ok
    for bad in ("../x", "a/b", "/abs", "..", ".hidden", "a\\b"):
        with pytest.raises(ValueError):
            _safe_scenario_id(bad)


# --- defect 8: mono/unclear stacks always score degraded --------------------

def test_cartesia_two_channel_download_is_degraded(tmp_path):
    """Cartesia is spec-tagged [unclear]: even a 2-channel download must be
    scored degraded/indicative, never a confident dual-channel verdict."""
    from hotato import capture

    p = _write_stereo(tmp_path / "cartesia.wav")
    err = io.StringIO()
    with redirect_stderr(err):
        capture._score_capture("cartesia", p, onset=None, expect="yield",
                               caller_channel=0, agent_channel=1)
    assert "[cartesia] degraded" in err.getvalue()


def test_confirmed_mono_stack_two_channel_download_is_degraded(tmp_path):
    from hotato import capture

    p = _write_stereo(tmp_path / "bland.wav")
    err = io.StringIO()
    with redirect_stderr(err):
        capture._score_capture("bland", p, onset=None, expect="yield",
                               caller_channel=0, agent_channel=1)
    assert "[bland] degraded" in err.getvalue()


def test_dual_pull_stack_two_channel_download_is_not_degraded(tmp_path):
    """A spec-verified dual stack keeps the confident dual-channel verdict."""
    from hotato import capture

    p = _write_stereo(tmp_path / "vapi.wav")
    err = io.StringIO()
    with redirect_stderr(err):
        capture._score_capture("vapi", p, onset=None, expect="yield",
                               caller_channel=0, agent_channel=1)
    assert "degraded" not in err.getvalue()


# --- defect 10: a non-JSON 200 body is a clean, named ValueError ------------

def test_http_get_json_rejects_non_json_body():
    from hotato import capture

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"<html><body>Service Unavailable</body></html>")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_port
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        with pytest.raises(ValueError) as exc:
            capture._http_get_json(f"http://127.0.0.1:{port}/x")
        msg = str(exc.value)
        assert "non-JSON body" in msg
        assert str(port) in msg  # the URL is named
    finally:
        srv.shutdown()


# NOTE on defect 6 (docs/SAA-FIX-POINTER.md's false 'no threshold value can fix'
# absolute + unqualified CTA): the fix is applied to that doc on disk, but the
# file is deliberately gitignored (.gitignore names it), so it is not tracked and
# there is no committed artifact to assert against here. The CODE behaviour the
# doc described is pinned by test_lone_backchannel_false_stop_routes_to_config and
# test_both_axes_battery_still_fires_the_pointer above, which are the source of
# truth the corrected doc now matches.
