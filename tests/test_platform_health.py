"""``hotato vapi health`` / ``hotato retell health`` / ``hotato bland health``:
the platform keystone -- pull -> folder check -> Voice Stability Score, one
command, fully offline here.

Every test mocks ``urllib.request.urlopen`` (the only HTTP surface in
``hotato.capture``) exactly as the pull suite does, or stubs ``cap.pull`` /
``cap.list_calls`` at the same seams pull's own tests use; the recordings the
fake platform serves are the bundled deterministic rendered examples. Pinned
here:

  * per-stack end to end: the REAL pull machinery (list + per-call fetch +
    validated download) feeds the REAL folder aggregate, and the output
    leads with the Voice Stability Score, the measured share line directly
    beneath it as the formula, with the eligible sample size and the
    analysis-policy sha beside the score and a small-sample label under 20
    dual-channel calls;
  * score arithmetic is the dual-channel share, times 100: all-clean =
    100/100, all-critical = 0/100, a MONO call NEVER enters the
    denominator (a mixed directory's score counts only the dual-channel
    calls; mono calls report into the best-effort mono observations block
    with their own counts), zero dual-channel calls render NO score and
    state why, and zero analyzed calls refuse with the reason;
  * the evidence-coverage block lists per-lane measured counts from what
    the run actually had, and a lane whose evidence was absent from the
    run never renders as assessed;
  * a missing API key is one actionable line (export the env var or hotato
    connect); retell without --call-id refuses (no fabricated list
    endpoint); an empty window refuses; an all-fetch-failed pull refuses;
  * bland / synthflow / millis (mono/mixed by spec) run autopsy's
    measured-confidence mono path -- the health check works, the scope
    line is stated, and the report carries the observations block without
    a stability score; synthflow and millis are the same thin alias over
    the one implementation (PULL_STACKS members, routed mono);
  * recurrence lines print only when an incident kind in THIS run also
    appears in a stored prior run of the same directory (present + absent
    cases), with the prior dates read from stored recorded_at, and each
    line carries its measured state: observed (1-2 in the stored window),
    RECURRING (3+), RECURRING, LOW SAMPLE (3+ but under 20 eligible
    dual-channel calls), ELEVATED (20+ eligible in both compared runs,
    same policy + coverage, Wilson 95% intervals non-overlapping -- the
    repository's own Wilson helper);
  * the shipped flags reach the pull layer (--last window cutoff, --limit,
    --dir) and --output writes the report copy;
  * every user-facing string this wave added follows the language rule
    (say check / report; never the reserved platform words).
"""

import json
import os
import re
import time
import urllib.request
from importlib import resources

import pytest

from hotato import capture as cap
from hotato import cli, healthcmd
from hotato import scanfolder as scanfolder_mod

EXAMPLES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples", "autopsy", "audio",
)


def _example_bytes(name: str) -> bytes:
    with open(os.path.join(EXAMPLES, name + ".example.wav"), "rb") as fh:
        return fh.read()


def _bundled_bytes(sid: str) -> bytes:
    return (resources.files("hotato")
            .joinpath("data", "audio", sid + ".example.wav").read_bytes())


# --- offline HTTP plumbing (the pull suite's own pattern) --------------------

class _Resp:
    def __init__(self, data):
        self._data = data

    def read(self, size=-1):
        return self._data if size is None or size < 0 else self._data[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install(monkeypatch, routes, seen=None):
    """Route by URL substring -> bytes (or an Exception to raise). Longest
    key wins, so '/call/v1' matches before '/call'."""
    keys = sorted(routes, key=len, reverse=True)

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        if seen is not None:
            seen.append(req)
        for key in keys:
            if key in url:
                payload = routes[key]
                if isinstance(payload, Exception):
                    raise payload
                return _Resp(payload)
        raise AssertionError(f"unexpected URL fetched offline: {url}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOTATO_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HOTATO_ALLOW_MONO", raising=False)
    for var in ("VAPI_API_KEY", "RETELL_API_KEY", "BLAND_API_KEY",
                "SYNTHFLOW_API_KEY", "SYNTHFLOW_MODEL_ID",
                "MILLIS_API_KEY", "MILLIS_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)


def _vapi_routes(files):
    """A fake Vapi: the list endpoint plus per-call objects and media, each
    call served one of the bundled rendered example recordings."""
    arr = [{"id": i} for i in files]
    routes = {"api.vapi.ai/call?": json.dumps(arr).encode()}
    for ident, data in files.items():
        routes[f"api.vapi.ai/call/{ident}"] = json.dumps(
            {"artifact": {"recording": {
                "stereoUrl": f"https://media.test/{ident}.rec.wav"}}}
        ).encode()
        routes[f"{ident}.rec.wav"] = data
    return routes


def _mixed_fleet():
    # one clean yielding call + three rendered incident calls -> 1 of 4
    # clean, score 25/100
    return {
        "v1": _example_bytes("autopsy-01-barge-in-say-do"),
        "v2": _example_bytes("autopsy-02-latency-dead-air"),
        "v3": _example_bytes("autopsy-03-talk-over"),
        "v4": _bundled_bytes("01-hard-interruption"),
    }


# =========================================================================
# 1. Per-stack end to end: pull -> folder check -> score composition
# =========================================================================

def test_vapi_health_pull_scan_score_composition(monkeypatch, capsys):
    monkeypatch.setenv("VAPI_API_KEY", "k")
    seen = []
    _install(monkeypatch, _vapi_routes(_mixed_fleet()), seen)
    assert cli.main(["vapi", "health"]) == 0
    out = capsys.readouterr().out
    assert "hotato vapi health: pulled 4 of 4 listed calls" in out
    # the score headline: sample size + policy sha beside it, the
    # small-sample label (4 < 20 eligible calls), then the measured share
    # line as the formula
    lines = out.splitlines()
    idx = next(n for n, ln in enumerate(lines)
               if "Voice Stability Score" in ln)
    assert re.match(
        r"Voice Stability Score: 25/100  "
        r"\(4 dual-channel calls; policy [0-9a-f]{12}\)$",
        lines[idx].strip())
    assert lines[idx + 1].strip() == (
        "SMALL SAMPLE: 4 dual-channel calls, under the 20-call bar")
    assert ("1 of 4 dual-channel calls had no critical incidents (25%)"
            in lines[idx + 2])
    # the default window and cap reached the real list request
    list_url = next(r.full_url for r in seen if "api.vapi.ai/call?" in r.full_url)
    assert "limit=100" in list_url and "createdAtGt=" in list_url
    # the recordings landed in the default download dir
    pulled = sorted(os.listdir(os.path.join("hotato-output", "vapi-calls")))
    assert pulled == ["vapi__v1.wav", "vapi__v2.wav",
                      "vapi__v3.wav", "vapi__v4.wav"]
    # the report carries the score, the derivation note, the share line,
    # and the evidence coverage block
    m = re.search(r"report:\s+(\S+\.html)", out)
    assert m and os.path.isfile(m.group(1))
    html = open(m.group(1), encoding="utf-8").read()
    assert "Voice Stability Score: 25/100" in html
    assert re.search(r"policy [0-9a-f]{12}", html)
    assert "How this is calculated" in html
    assert "1 of 4 dual-channel calls had no critical incidents (25%)" in html
    assert "Evidence coverage" in html
    assert "dual-channel timing" in html


def test_retell_health_with_call_ids_composes(monkeypatch, capsys):
    monkeypatch.setenv("RETELL_API_KEY", "k")
    stereo = _bundled_bytes("01-hard-interruption")
    routes = {
        "api.retellai.com/v2/get-call/c1": json.dumps(
            {"scrubbed_recording_multi_channel_url":
             "https://media.test/c1.rec.wav"}).encode(),
        "c1.rec.wav": stereo,
    }
    _install(monkeypatch, routes)
    assert cli.main(["retell", "health", "--call-id", "c1"]) == 0
    out = capsys.readouterr().out
    assert "hotato retell health: pulled 1 of 1 listed call" in out
    assert "Voice Stability Score: 100/100" in out
    assert os.path.isfile(os.path.join(
        "hotato-output", "retell-calls", "retell__c1.wav"))


def test_bland_health_mono_best_effort(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("BLAND_API_KEY", "k")
    # a one-channel export: the first channel of a bundled example stands in
    # for Bland's mixed recording
    from hotato._engine.audio import read_wav, write_wav

    src = str(resources.files("hotato").joinpath(
        "data", "audio", "01-hard-interruption.example.wav"))
    sig = read_wav(src)
    mono_path = tmp_path / "mono-src.wav"
    write_wav(str(mono_path), sig.sample_rate, [sig.get(0)])
    routes = {
        "api.bland.ai/v1/calls/b1": json.dumps(
            {"recording_url": "https://media.test/b1.rec.wav"}).encode(),
        "api.bland.ai/v1/calls": json.dumps(
            {"calls": [{"call_id": "b1"}]}).encode(),
        "b1.rec.wav": mono_path.read_bytes(),
    }
    _install(monkeypatch, routes)
    assert cli.main(["bland", "health", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pull"] == {"stack": "bland", "window": "7d", "listed": 1,
                               "pulled": 1, "skipped": 0,
                               "dir": os.path.join("hotato-output",
                                                   "bland-calls")}
    assert payload["counts"]["analyzed"] == 1
    assert payload["calls"][0]["mode"] == "mono"
    # a mono call NEVER enters the Voice Stability denominator: zero
    # dual-channel calls -> no score, and the payload states why; the call
    # reports into the best-effort mono observations block instead
    assert payload["health"]["calls_analyzed"] == 0
    assert payload["health"]["share"] is None
    assert payload["health"]["critical_free_call_rate"] is None
    assert "0 dual-channel calls" in payload["health"]["no_score_reason"]
    assert payload["mono"]["label"] == "Best-effort mono observations"
    assert payload["mono"]["calls_analyzed"] == 1
    assert [c["lane"] for c in payload["coverage"]] == ["mono best-effort"]


def test_bland_health_text_states_the_mono_scope_once(monkeypatch, tmp_path,
                                                      capsys):
    monkeypatch.setenv("BLAND_API_KEY", "k")
    from hotato._engine.audio import read_wav, write_wav

    sig = read_wav(str(resources.files("hotato").joinpath(
        "data", "audio", "01-hard-interruption.example.wav")))
    mono_path = tmp_path / "mono-src.wav"
    write_wav(str(mono_path), sig.sample_rate, [sig.get(0)])
    routes = {
        "api.bland.ai/v1/calls/b1": json.dumps(
            {"recording_url": "https://media.test/b1.rec.wav"}).encode(),
        "api.bland.ai/v1/calls": json.dumps(
            {"calls": [{"call_id": "b1"}]}).encode(),
        "b1.rec.wav": mono_path.read_bytes(),
    }
    _install(monkeypatch, routes)
    assert cli.main(["bland", "health"]) == 0
    out = capsys.readouterr().out
    # the functional scope of a mixed channel, stated once per run
    assert "Mono scope:" in out
    # mono observations, no stability score -- and the report states why
    assert "Voice Stability Score" not in out
    assert "no score: 0 dual-channel calls analyzed" in out
    assert "Best-effort mono observations: 1 call" in out


# =========================================================================
# 2. Score arithmetic: the dual-channel share, times 100 -- nothing else
# =========================================================================

def _scan_dir(tmp_path, files):
    d = tmp_path / "calls"
    d.mkdir()
    for name, data in files:
        (d / name).write_bytes(data)
    return d


def _mono_bytes(tmp_path) -> bytes:
    """A one-channel WAV: the first channel of a bundled example stands in
    for a platform's mixed export."""
    from hotato._engine.audio import read_wav, write_wav

    sig = read_wav(str(resources.files("hotato").joinpath(
        "data", "audio", "01-hard-interruption.example.wav")))
    p = tmp_path / "mono-src.wav"
    write_wav(str(p), sig.sample_rate, [sig.get(0)])
    return p.read_bytes()


def test_score_all_clean_is_100(tmp_path, capsys):
    clean = _bundled_bytes("01-hard-interruption")
    d = _scan_dir(tmp_path, [("a.wav", clean), ("b.wav", clean)])
    assert cli.main(["scan", str(d)]) == 0
    out = capsys.readouterr().out
    assert "Voice Stability Score: 100/100" in out
    assert "2 of 2 dual-channel calls had no critical incidents (100%)" in out


def test_score_all_critical_is_0(tmp_path, capsys):
    d = _scan_dir(tmp_path, [
        ("a.wav", _example_bytes("autopsy-01-barge-in-say-do"))])
    assert cli.main(["scan", str(d)]) == 0
    out = capsys.readouterr().out
    assert "Voice Stability Score: 0/100" in out
    assert "0 of 1 dual-channel calls had no critical incidents (0%)" in out


def test_mixed_dir_score_counts_only_dual_channel(tmp_path, capsys):
    # one clean dual-channel call + one mono call: the score is 100/100
    # over a 1-call dual-channel denominator -- the mono call NEVER enters
    # the rate; it reports into the observations block with its own counts
    d = _scan_dir(tmp_path, [
        ("dual.wav", _bundled_bytes("01-hard-interruption")),
        ("mono.wav", _mono_bytes(tmp_path)),
    ])
    assert cli.main(["scan", str(d), "--format", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["counts"]["analyzed"] == 2
    assert result["health"]["calls_analyzed"] == 1
    assert result["health"]["calls_no_critical"] == 1
    assert result["health"]["critical_free_call_rate"] == 100
    assert result["health"]["share"] == 1.0
    assert result["mono"]["calls_analyzed"] == 1
    # the CLI text keeps the two populations apart
    assert cli.main(["scan", str(d)]) == 0
    out = capsys.readouterr().out
    assert "Voice Stability Score: 100/100  (1 dual-channel call;" in out
    assert "1 of 1 dual-channel calls had no critical incidents (100%)" in out
    assert "Best-effort mono observations: 1 call" in out


def test_coverage_block_reports_only_lanes_the_run_had(tmp_path, capsys):
    # a dual-only directory: the coverage block lists the dual-channel
    # lane and nothing else -- no mono lane, no transcript lane, no
    # tool-trace lane (that evidence was absent from the run, so nothing
    # renders as assessed on it)
    d = _scan_dir(tmp_path, [("a.wav", _bundled_bytes("01-hard-interruption"))])
    assert cli.main(["scan", str(d), "--format", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert [c["lane"] for c in result["coverage"]] == ["dual-channel timing"]
    assert result["mono"] is None
    assert cli.main(["scan", str(d)]) == 0
    out = capsys.readouterr().out
    assert "evidence coverage (measured from this run):" in out
    assert "dual-channel timing: 1 call" in out
    assert "mono best-effort" not in out
    assert "transcript" not in out.lower()
    assert "tool-trace" not in out.lower()


def test_coverage_block_counts_mixed_and_refused_lanes(tmp_path, capsys):
    d = _scan_dir(tmp_path, [
        ("dual.wav", _bundled_bytes("01-hard-interruption")),
        ("mono.wav", _mono_bytes(tmp_path)),
        ("broken.wav", b"this is not a wav"),
    ])
    assert cli.main(["scan", str(d)]) == 0
    out = capsys.readouterr().out
    assert "evidence coverage (measured from this run):" in out
    assert "dual-channel timing: 1 call" in out
    assert "mono best-effort: 1 call" in out
    # refused stays visible, with the reasons listed further down
    assert "refused: 1 file" in out
    assert "broken.wav" in out and "not a readable PCM WAV" in out
    report = re.search(r"report:\s+(\S+\.html)", out).group(1)
    html = open(report, encoding="utf-8").read()
    assert "Evidence coverage" in html
    assert "mono best-effort" in html


def test_zero_analyzed_calls_render_no_score(tmp_path, capsys):
    # every file refused -> no 0/0 theater: the scan renders NO score line
    d = _scan_dir(tmp_path, [("broken.wav", b"this is not a wav")])
    assert cli.main(["scan", str(d)]) == 0
    out = capsys.readouterr().out
    assert "Voice Stability Score" not in out
    assert "0 calls analyzed" in out
    html = open(re.search(r"report:\s+(\S+\.html)", out).group(1),
                encoding="utf-8").read()
    assert "Voice Stability Score" not in html


def test_health_refuses_when_nothing_analyzable(monkeypatch, tmp_path,
                                                capsys):
    # the pull succeeds but the download is not readable call audio -> the
    # health command refuses with the reason instead of scoring 0/0
    monkeypatch.setenv("VAPI_API_KEY", "k")

    def fake_pull(stack, creds, *, out_dir, ids=None, since=None, limit=50,
                  allow_mono=False, log=None):
        os.makedirs(out_dir, exist_ok=True)
        p = os.path.join(out_dir, "vapi__x.wav")
        with open(p, "wb") as fh:
            fh.write(b"not audio at all")
        return {"stack": stack, "out_dir": out_dir, "listed": 1,
                "pulled": [{"id": "x", "path": p}], "skipped": []}

    monkeypatch.setattr(cap, "pull", fake_pull)
    assert cli.main(["vapi", "health"]) == 2
    err = capsys.readouterr().err
    assert "No score is reported over zero analyzed calls" in err
    assert "refused" in err


# =========================================================================
# 3. Refusals: missing key, retell ids, empty window, all-fetch-failed
# =========================================================================

def test_missing_key_is_one_actionable_line(monkeypatch, capsys):
    for stack, env in (("vapi", "VAPI_API_KEY"),
                       ("retell", "RETELL_API_KEY"),
                       ("bland", "BLAND_API_KEY")):
        assert cli.main([stack, "health"]) == 2
        err = capsys.readouterr().err
        assert f"export {env}=YOUR_KEY" in err
        assert f"hotato connect {stack}" in err


def test_retell_health_without_call_id_refuses(monkeypatch, capsys):
    monkeypatch.setenv("RETELL_API_KEY", "k")
    # no urlopen installed: a fabricated list request would raise loudly
    assert cli.main(["retell", "health"]) == 2
    err = capsys.readouterr().err
    assert "--call-id" in err
    assert "no verified list-recent-calls endpoint" in err


def test_empty_window_refuses_with_reason(monkeypatch, capsys):
    monkeypatch.setenv("VAPI_API_KEY", "k")
    _install(monkeypatch, {"api.vapi.ai/call?": b"[]"})
    assert cli.main(["vapi", "health"]) == 2
    err = capsys.readouterr().err
    assert "no vapi calls found in the last 7d" in err
    assert "--last 30d" in err
    assert "Voice Stability Score" not in capsys.readouterr().out


def test_malformed_last_window_is_a_usage_error(monkeypatch, capsys):
    monkeypatch.setenv("VAPI_API_KEY", "k")
    assert cli.main(["vapi", "health", "--last", "banana"]) == 2
    assert "--last 'banana' is not a duration" in capsys.readouterr().err


def test_every_fetch_failed_refuses(monkeypatch, capsys):
    monkeypatch.setenv("VAPI_API_KEY", "k")
    import io
    import urllib.error

    routes = {
        "api.vapi.ai/call?": json.dumps([{"id": "v1"}]).encode(),
        "api.vapi.ai/call/v1": urllib.error.HTTPError(
            "https://api.vapi.ai/call/v1", 404, "err", None,
            io.BytesIO(b"nope")),
    }
    _install(monkeypatch, routes)
    assert cli.main(["vapi", "health"]) == 2
    err = capsys.readouterr().err
    assert "every listed vapi recording failed to fetch" in err
    assert "1 listed, 0 pulled" in err


# =========================================================================
# 4. Recurrence states (stored envelopes only)
# =========================================================================

def _seed_prior(envelope_path, *, categories=None, recorded_at):
    """Store a prior-run summary envelope for the same directory: the
    current envelope with a different content id, the given categories, and
    its own recorded_at provenance."""
    current = json.load(open(envelope_path, encoding="utf-8"))
    prior = dict(current)
    prior["id"] = "scn-aaaaaaaaaaaa"
    prior["recorded_at"] = recorded_at
    if categories is not None:
        prior["categories"] = categories
    out = os.path.join(os.path.dirname(envelope_path),
                       "scan-scn-aaaaaaaaaaaa.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(prior, fh, indent=2)


def test_recurrence_line_prints_for_a_recurring_kind(tmp_path, capsys):
    d = _scan_dir(tmp_path, [
        ("a.wav", _example_bytes("autopsy-01-barge-in-say-do")),
        ("b.wav", _bundled_bytes("01-hard-interruption")),
    ])
    assert cli.main(["scan", str(d)]) == 0
    out1 = capsys.readouterr().out
    assert "RECURRING" not in out1  # first run: no prior envelopes
    assert "observed:" not in out1
    envelope = re.search(r"envelope:\s+(\S+\.json)", out1).group(1)
    report = re.search(r"report:\s+(\S+\.html)", out1).group(1)
    _seed_prior(envelope, recorded_at="2026-07-20T09:00:00Z")

    assert cli.main(["scan", str(d)]) == 0
    out2 = capsys.readouterr().out
    # BARGE-IN is in both calls this run (one critical, one warning) and in
    # both stored prior-run calls: 4 occurrences in the stored window -> a
    # 3+ recurrence, and the 2-call eligible sample is under 20, so the
    # state is RECURRING, LOW SAMPLE. Dates come from the stored
    # recorded_at.
    expected = ("RECURRING, LOW SAMPLE: BARGE-IN in 2 of 2 calls this run "
                "(4 in the stored window). Also present in 1 prior run(s): "
                "2026-07-20T09:00:00Z. Eligible sample: 2 dual-channel "
                "calls, under 20.")
    assert expected in out2
    html = open(report, encoding="utf-8").read()
    assert "Recurrence" in html
    assert expected in html
    # deterministic given the same store
    assert cli.main(["scan", str(d)]) == 0
    assert capsys.readouterr().out == out2
    assert open(report, encoding="utf-8").read() == html


def test_no_fleet_alert_for_a_kind_absent_from_prior_runs(tmp_path, capsys):
    d = _scan_dir(tmp_path, [
        ("a.wav", _example_bytes("autopsy-01-barge-in-say-do")),
    ])
    assert cli.main(["scan", str(d)]) == 0
    out1 = capsys.readouterr().out
    envelope = re.search(r"envelope:\s+(\S+\.json)", out1).group(1)
    # the prior run measured ONLY a kind this run does not have
    _seed_prior(envelope, recorded_at="2026-07-19T09:00:00Z", categories=[
        {"kind_key": "echo-suspected", "kind": "ECHO SUSPECTED",
         "count": 3, "critical": 0, "worst": None},
    ])
    assert cli.main(["scan", str(d)]) == 0
    out = capsys.readouterr().out
    assert "RECURRING" not in out
    assert "observed:" not in out
    assert "ELEVATED" not in out


def test_recurrence_line_in_the_health_command_output(monkeypatch, tmp_path,
                                                      capsys):
    # The first health run stores its summary envelope; a later run over
    # changed fleet content reads it back as a prior run and prints the
    # recurrence line -- no seeding, the store the command itself wrote.
    monkeypatch.setenv("VAPI_API_KEY", "k")
    fleet = {"v1": _example_bytes("autopsy-01-barge-in-say-do")}
    _install(monkeypatch, _vapi_routes(fleet))
    assert cli.main(["vapi", "health"]) == 0
    out1 = capsys.readouterr().out
    assert "RECURRING" not in out1
    # a second run over CHANGED content (one more call) of the same dir:
    # 2 calls with BARGE-IN this run + 1 in the stored prior run = 3 in
    # the window, and 2 eligible dual-channel calls is under 20
    fleet["v2"] = _bundled_bytes("01-hard-interruption")
    _install(monkeypatch, _vapi_routes(fleet))
    assert cli.main(["vapi", "health"]) == 0
    out2 = capsys.readouterr().out
    assert ("RECURRING, LOW SAMPLE: BARGE-IN in 2 of 2 calls this run "
            "(3 in the stored window). Also present in 1 prior run(s): "
            ) in out2


# --- every recurrence state, over stored-shape facts ----------------------

def _state_result(*, dual_with_kind, dual_clean, mono_with_kind=0,
                  kind="dead-air"):
    """A scan-folder result skeleton with the exact fields fleet_alerts
    reads: per-call kinds/modes, the dual-channel eligible sample, and the
    policy sha (the same helper the real run stamps)."""
    calls = ([{"mode": "stereo", "kinds": [kind]}] * dual_with_kind
             + [{"mode": "stereo", "kinds": []}] * dual_clean
             + [{"mode": "mono", "kinds": [kind]}] * mono_with_kind)
    return {
        "counts": {"analyzed": len(calls)},
        "health": {"calls_analyzed": dual_with_kind + dual_clean},
        "policy_sha": scanfolder_mod.policy_sha(2.0),
        "calls": calls,
        "categories": [{"kind_key": kind, "kind": "DEAD AIR",
                        "count": dual_with_kind + mono_with_kind,
                        "critical": dual_with_kind, "worst": None}],
    }


def _state_prior(*, kind_calls, dual_kind_calls, eligible,
                 recorded_at="2026-07-20T09:00:00Z", kind="dead-air",
                 policy=None, lanes=("dual",)):
    return {
        "id": "scn-aaaaaaaaaaaa",
        "recorded_at": recorded_at,
        "kind_keys": [kind] if kind_calls else [],
        "kind_call_counts": {kind: kind_calls} if kind_calls else {},
        "dual_kind_call_counts": ({kind: dual_kind_calls}
                                  if dual_kind_calls else {}),
        "eligible": eligible,
        "lanes": sorted(lanes),
        "policy_sha": policy or scanfolder_mod.policy_sha(2.0),
    }


def test_state_observed_at_one_or_two_in_window():
    result = _state_result(dual_with_kind=1, dual_clean=3)
    prior = _state_prior(kind_calls=1, dual_kind_calls=1, eligible=4)
    (alert,) = scanfolder_mod.fleet_alerts(result, [prior])
    assert alert["state"] == "observed"
    assert alert["occurrences"] == 2
    text = scanfolder_mod.fleet_alert_text(alert)
    assert text.startswith(
        "observed: DEAD AIR in 1 of 4 calls this run (2 in the stored "
        "window). Also present in 1 prior run(s): 2026-07-20T09:00:00Z.")


def test_state_recurring_at_three_plus_with_a_full_sample():
    result = _state_result(dual_with_kind=2, dual_clean=23)
    # the prior run is NOT comparable for ELEVATED (eligible under 20), so
    # a 3+ recurrence over a 20+ eligible sample reads plain RECURRING
    prior = _state_prior(kind_calls=1, dual_kind_calls=1, eligible=5)
    (alert,) = scanfolder_mod.fleet_alerts(result, [prior])
    assert alert["state"] == "RECURRING"
    assert alert["occurrences"] == 3
    text = scanfolder_mod.fleet_alert_text(alert)
    assert text.startswith("RECURRING: DEAD AIR in 2 of 25 calls this run")
    assert "Wilson" not in text


def test_state_recurring_low_sample_under_twenty_eligible():
    result = _state_result(dual_with_kind=2, dual_clean=2)
    prior = _state_prior(kind_calls=2, dual_kind_calls=2, eligible=4)
    (alert,) = scanfolder_mod.fleet_alerts(result, [prior])
    assert alert["state"] == "RECURRING, LOW SAMPLE"
    assert alert["occurrences"] == 4
    text = scanfolder_mod.fleet_alert_text(alert)
    assert "Eligible sample: 4 dual-channel calls, under 20." in text


def test_state_elevated_needs_nonoverlapping_wilson_intervals():
    # this run: 12 of 25 eligible dual-channel calls carry the kind; the
    # comparable prior run (same policy, same lanes, 30 eligible) had 2 of
    # 30 -- Wilson 95% intervals do not overlap, so the state is ELEVATED
    result = _state_result(dual_with_kind=12, dual_clean=13)
    prior = _state_prior(kind_calls=2, dual_kind_calls=2, eligible=30)
    (alert,) = scanfolder_mod.fleet_alerts(result, [prior])
    assert alert["state"] == "ELEVATED"
    assert alert["elevated_vs"] == "2026-07-20T09:00:00Z"
    text = scanfolder_mod.fleet_alert_text(alert)
    assert text.startswith("ELEVATED: DEAD AIR in 12 of 25 calls this run")
    assert ("Wilson 95% intervals do not overlap with the run recorded "
            "2026-07-20T09:00:00Z") in text


def test_state_elevated_requires_same_policy_and_coverage():
    result = _state_result(dual_with_kind=12, dual_clean=13)
    # same measured facts, different policy sha -> never compared: RECURRING
    other_policy = _state_prior(kind_calls=2, dual_kind_calls=2, eligible=30,
                                policy="ffffffffffff")
    (alert,) = scanfolder_mod.fleet_alerts(result, [other_policy])
    assert alert["state"] == "RECURRING"
    # same policy, different evidence lanes -> never compared: RECURRING
    other_lanes = _state_prior(kind_calls=2, dual_kind_calls=2, eligible=30,
                               lanes=("dual", "mono"))
    (alert,) = scanfolder_mod.fleet_alerts(result, [other_lanes])
    assert alert["state"] == "RECURRING"
    # overlapping intervals (prior 10 of 30) -> RECURRING, not ELEVATED
    overlapping = _state_prior(kind_calls=10, dual_kind_calls=10, eligible=30)
    (alert,) = scanfolder_mod.fleet_alerts(result, [overlapping])
    assert alert["state"] == "RECURRING"


# =========================================================================
# 5. synthflow / millis: the same thin alias, routed mono
# =========================================================================

def test_health_stacks_membership_and_routing():
    # every health entry rides the existing pull machinery; the mono
    # stacks route through autopsy's best-effort mono path like bland
    for stack in healthcmd.HEALTH_STACKS:
        assert stack in cap.PULL_STACKS
    assert (set(healthcmd.HEALTH_STACKS) & set(cap.DUAL_PULL_STACKS)
            == {"vapi", "retell"})
    assert (set(healthcmd.HEALTH_STACKS) & set(cap.MONO_STACKS)
            == {"bland", "synthflow", "millis"})


def test_synthflow_and_millis_health_are_thin_mono_aliases(
        monkeypatch, tmp_path, capsys):
    mono = _mono_bytes(tmp_path)
    for stack in ("synthflow", "millis"):
        env = cap.CONNECT_SPECS[stack]["env"]["api_key"]
        monkeypatch.setenv(env, "k")
        seen = {}

        def fake_pull(s, creds, *, out_dir, ids=None, since=None, limit=50,
                      allow_mono=False, log=None):
            seen.update(stack=s, allow_mono=allow_mono)
            os.makedirs(out_dir, exist_ok=True)
            p = os.path.join(out_dir, f"{s}__m1.wav")
            with open(p, "wb") as fh:
                fh.write(mono)
            return {"stack": s, "out_dir": out_dir, "listed": 1,
                    "pulled": [{"id": "m1", "path": p}], "skipped": []}

        monkeypatch.setattr(cap, "pull", fake_pull)
        assert cli.main([stack, "health"]) == 0
        out = capsys.readouterr().out
        # the pull layer got the stack and the mono routing, like bland
        assert seen == {"stack": stack, "allow_mono": True}
        assert f"hotato {stack} health: pulled 1 of 1 listed call" in out
        assert "Mono scope:" in out
        assert "Best-effort mono observations: 1 call" in out
        assert "Voice Stability Score" not in out
        assert "no score: 0 dual-channel calls analyzed" in out


def test_synthflow_and_millis_missing_key_is_one_actionable_line(capsys):
    for stack, env in (("synthflow", "SYNTHFLOW_API_KEY"),
                       ("millis", "MILLIS_API_KEY")):
        assert cli.main([stack, "health"]) == 2
        err = capsys.readouterr().err
        assert f"export {env}=YOUR_KEY" in err
        assert f"hotato connect {stack}" in err


# =========================================================================
# 6. Flags reach the pull layer; --output writes the report copy
# =========================================================================

def test_flags_reach_pull_and_output_writes_report(monkeypatch, tmp_path,
                                                   capsys):
    monkeypatch.setenv("VAPI_API_KEY", "k")
    seen = {}

    def fake_pull(stack, creds, *, out_dir, ids=None, since=None, limit=50,
                  allow_mono=False, log=None):
        seen.update(stack=stack, out_dir=out_dir, since=since, limit=limit,
                    ids=ids, allow_mono=allow_mono, creds=dict(creds))
        os.makedirs(out_dir, exist_ok=True)
        p = os.path.join(out_dir, "vapi__a.wav")
        with open(p, "wb") as fh:
            fh.write(_bundled_bytes("01-hard-interruption"))
        return {"stack": stack, "out_dir": out_dir, "listed": 1,
                "pulled": [{"id": "a", "path": p}], "skipped": []}

    monkeypatch.setattr(cap, "pull", fake_pull)
    out_html = tmp_path / "health.html"
    dl = tmp_path / "downloads"
    assert cli.main(["vapi", "health", "--last", "30d", "--limit", "7",
                     "--dir", str(dl), "--output", str(out_html)]) == 0
    assert seen["stack"] == "vapi"
    assert seen["limit"] == 7
    assert seen["out_dir"] == str(dl)
    assert seen["ids"] is None
    assert seen["allow_mono"] is False  # vapi pulls the two-channel export
    assert seen["creds"] == {"api_key": "k"}  # env var honored, as pull does
    # --last parsed into the epoch cutoff the pull layer filters on
    assert abs((time.time() - 30 * 86400) - seen["since"]) < 120
    out = capsys.readouterr().out
    assert f"report also written to {out_html}" in out
    html = out_html.read_text(encoding="utf-8")
    assert "Voice Stability Score: 100/100" in html


def test_call_id_flag_skips_the_list_step_for_vapi(monkeypatch, capsys):
    monkeypatch.setenv("VAPI_API_KEY", "k")
    stereo = _bundled_bytes("01-hard-interruption")
    # no list route installed: --call-id must not hit the list endpoint
    routes = {
        "api.vapi.ai/call/v9": json.dumps(
            {"artifact": {"recording": {
                "stereoUrl": "https://media.test/v9.rec.wav"}}}).encode(),
        "v9.rec.wav": stereo,
    }
    _install(monkeypatch, routes)
    assert cli.main(["vapi", "health", "--call-id", "v9"]) == 0
    assert "Voice Stability Score: 100/100" in capsys.readouterr().out


# =========================================================================
# 7. The new user-facing copy follows the language rule
# =========================================================================

def _health_help_texts():
    parser = cli.build_parser()
    texts = []
    import argparse as _argparse

    for action in parser._actions:
        if not isinstance(action, _argparse._SubParsersAction):
            continue
        for name, sub in action.choices.items():
            if name not in healthcmd.HEALTH_STACKS:
                continue
            texts.append(sub.format_help())
            for sub_action in sub._actions:
                if isinstance(sub_action, _argparse._SubParsersAction):
                    for leaf in sub_action.choices.values():
                        texts.append(leaf.format_help())
    assert len(texts) >= 10
    return texts


def test_new_copy_follows_the_language_rule():
    # The reserved words never appear in the strings this wave added: the
    # health help texts, the health error lines, the score derivation note,
    # the mono observations label + note, the coverage details, and one
    # line per recurrence state.
    reserved = ("contract", "prove", "local-first", "observability",
                "console")
    state_lines = [
        scanfolder_mod.fleet_alert_text({
            "kind": "DEAD AIR", "state": state, "calls_this_run": 2,
            "calls_analyzed": 5, "occurrences": occurrences, "eligible": 5,
            "prior_runs": 1, "prior_dates": ["2026-07-20T09:00:00Z"],
            "elevated_vs": ("2026-07-20T09:00:00Z"
                            if state == "ELEVATED" else None),
        })
        for state, occurrences in (("observed", 2), ("RECURRING", 3),
                                   ("RECURRING, LOW SAMPLE", 3),
                                   ("ELEVATED", 6))
    ]
    surfaces = _health_help_texts() + [
        healthcmd.missing_key_message(s) for s in healthcmd.HEALTH_STACKS
    ] + [
        healthcmd.retell_ids_message(),
        scanfolder_mod.SCORE_HOW_NOTE,
        scanfolder_mod.MONO_OBSERVATIONS_LABEL,
        scanfolder_mod.MONO_OBSERVATIONS_NOTE,
    ] + state_lines
    for text in surfaces:
        low = text.lower()
        for word in reserved:
            assert word not in low, f"reserved word {word!r} in: {text[:120]}"
