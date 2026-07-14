"""``hotato fleet trend``: the self-contained turn-taking trend dashboard built
from the fleet SQLite registry.

Seeds the registry directly (mirroring tests/test_fleet_registry_entities.py's
pattern of exercising Registry rows straight, plus one real ingest+discover
loop via tests/_trial_audio.py for an end-to-end check) rather than driving
real audio through every scenario: the trend math only needs known
``measured_json`` / ``created_at`` values, not real WAVs.
"""
from __future__ import annotations

import json

from hotato import cli
from hotato._stats import dist_summary, percentile
from hotato.fleet import trend as _trend
from hotato.fleet.registry import Registry
from tests import _trial_audio as ta

DAY = 86400.0


def _seed_candidate(reg, ws, agent_id, cid, *, day_epoch, overlap_sec=None,
                    after_sec=None, went_silent=None, kind="overlap_while_agent_talking"):
    """Insert one candidate row with a hand-built measured_json and an explicit
    (backdated) created_at, mirroring exactly what FleetAPI.discover would have
    written for an ``overlap_while_agent_talking`` scan hit."""
    measured = {"kind": kind}
    if kind == "overlap_while_agent_talking":
        measured["durations"] = {"overlap_sec": overlap_sec}
        measured["agent_reaction"] = {
            "went_silent_within_search": bool(went_silent) if went_silent is not None
                                          else after_sec is not None,
            "after_sec": after_sec,
        }
    reg.add_candidate(ws, cid, recording_id="rec1", agent_id=agent_id,
                      onset_sec=1.0, measured_json=json.dumps(measured),
                      severity=overlap_sec or 0.0, cluster=kind)
    reg.conn.execute(
        "UPDATE candidates SET created_at=? WHERE workspace_id=? AND candidate_id=?",
        (day_epoch, ws, cid))
    reg.conn.commit()


def _seed_trial(reg, ws, agent_id, trial_id, *, verdict, day_epoch):
    reg.add_trial(ws, trial_id, agent_id=agent_id, manifest_hash="m", verdict=verdict,
                  evidence_tier=1)
    reg.conn.execute(
        "UPDATE trials SET created_at=? WHERE workspace_id=? AND trial_id=?",
        (day_epoch, ws, trial_id))
    reg.conn.commit()


# --- collect(): pure data layer ---------------------------------------------

def test_collect_empty_home_has_no_agents(tmp_path):
    reg = Registry(home=str(tmp_path))
    data = _trend.collect(reg, "ws1")
    assert data["workspace_id"] == "ws1"
    assert data["agents"] == []
    reg.close()


def test_collect_never_interpolates_missing_days(tmp_path):
    reg = Registry(home=str(tmp_path))
    reg.add_agent("ws1", "bot-a", stack="vapi")
    base = 1_700_000_000.0
    _seed_candidate(reg, "ws1", "bot-a", "c1", day_epoch=base, overlap_sec=0.3, after_sec=0.4)
    _seed_candidate(reg, "ws1", "bot-a", "c2", day_epoch=base + 5 * DAY,
                    overlap_sec=0.5, after_sec=0.6)
    data = _trend.collect(reg, "ws1")
    agent = data["agents"][0]
    days = [r["day"] for r in agent["talk_over_sec"]]
    # exactly the two seeded days -- the 4 days between them are never
    # backfilled with a fabricated zero or an interpolated value.
    assert len(days) == 2
    assert len(set(days)) == 2
    counted_days = [d for d, _ in agent["candidates_per_day"]]
    assert len(counted_days) == 2
    reg.close()


def test_collect_p95_math_matches_stats_percentile_on_a_known_series(tmp_path):
    reg = Registry(home=str(tmp_path))
    reg.add_agent("ws1", "bot-a", stack="vapi")
    base = 1_700_000_000.0
    overlaps = [0.10, 0.20, 0.30, 0.40, 0.90]
    for i, v in enumerate(overlaps):
        _seed_candidate(reg, "ws1", "bot-a", f"c{i}", day_epoch=base, overlap_sec=v,
                        after_sec=None, went_silent=False)
    # a second day so the series has >= 2 points and actually renders as a line
    _seed_candidate(reg, "ws1", "bot-a", "c-other-day", day_epoch=base + DAY,
                    overlap_sec=0.5, after_sec=None, went_silent=False)
    data = _trend.collect(reg, "ws1")
    agent = data["agents"][0]
    row = agent["talk_over_sec"][0]
    expected = dist_summary(overlaps)
    assert row["n"] == 5
    assert row["p50"] == expected["median"]
    assert row["p95"] == expected["p95"]
    # cross-check against the raw percentile formula directly, independent of
    # dist_summary, so a bug shared by both would not hide here.
    assert row["p95"] == round(percentile(sorted(overlaps), 0.95), 3)
    reg.close()


def test_collect_time_to_yield_excludes_candidates_that_never_yielded(tmp_path):
    reg = Registry(home=str(tmp_path))
    reg.add_agent("ws1", "bot-a", stack="vapi")
    base = 1_700_000_000.0
    _seed_candidate(reg, "ws1", "bot-a", "c1", day_epoch=base, overlap_sec=0.3,
                    after_sec=0.5, went_silent=True)
    # went_silent False: contributes to talk_over_sec but NOT time_to_yield_sec
    _seed_candidate(reg, "ws1", "bot-a", "c2", day_epoch=base, overlap_sec=0.7,
                    after_sec=None, went_silent=False)
    data = _trend.collect(reg, "ws1")
    agent = data["agents"][0]
    tov_day = next(r for r in agent["talk_over_sec"] if r["day"])
    assert tov_day["n"] == 2
    # time_to_yield has only ONE real sample on that day -> not enough history
    # (a lone day never renders as a line either; asserted at the data layer
    # via the day count staying honest, not padded to match talk_over).
    assert agent["time_to_yield_sec"] == [] or agent["time_to_yield_sec"][0]["n"] == 1
    reg.close()


def test_collect_candidate_kinds_other_than_overlap_never_contribute(tmp_path):
    reg = Registry(home=str(tmp_path))
    reg.add_agent("ws1", "bot-a", stack="vapi")
    base = 1_700_000_000.0
    _seed_candidate(reg, "ws1", "bot-a", "c1", day_epoch=base, kind="long_response_gap")
    data = _trend.collect(reg, "ws1")
    agent = data["agents"][0]
    assert agent["candidates_total"] == 1          # still counted as a candidate moment
    assert agent["talk_over_sec"] == []             # but never re-derived as talk-over
    assert agent["time_to_yield_sec"] == []
    reg.close()


def test_collect_outcomes_buckets_known_verdicts_and_others_separately(tmp_path):
    reg = Registry(home=str(tmp_path))
    reg.add_agent("ws1", "bot-a", stack="vapi")
    base = 1_700_000_000.0
    _seed_trial(reg, "ws1", "bot-a", "t1", verdict="improved", day_epoch=base)
    _seed_trial(reg, "ws1", "bot-a", "t2", verdict="inconclusive", day_epoch=base)
    _seed_trial(reg, "ws1", "bot-a", "t3", verdict="refused", day_epoch=base)
    _seed_trial(reg, "ws1", "bot-a", "t4", verdict="created", day_epoch=base)  # precommitted, unrun
    data = _trend.collect(reg, "ws1")
    outcomes = data["agents"][0]["outcomes"]
    assert outcomes == {"improved": 1, "inconclusive": 1, "refused": 1, "other": 1}
    reg.close()


# --- build_trend_html(): rendering ------------------------------------------

def test_html_honest_empty_state_for_workspace_with_no_agents():
    html = _trend.build_trend_html({"workspace_id": "ws1", "generated_at": 0, "agents": []})
    assert "no agents registered" in html.lower()
    assert "<!doctype html>" in html.lower()


def test_html_not_enough_history_for_a_single_day_series(tmp_path):
    reg = Registry(home=str(tmp_path))
    reg.add_agent("ws1", "bot-a", stack="vapi", name="Bot A")
    _seed_candidate(reg, "ws1", "bot-a", "c1", day_epoch=1_700_000_000.0,
                    overlap_sec=0.44, after_sec=0.61, went_silent=True)
    data = _trend.collect(reg, "ws1")
    html = _trend.build_trend_html(data)
    assert html.count("not enough history to trend") >= 3  # talk-over, tty, candidates/day
    assert "<polyline" not in html  # a single point is never stretched into a line
    reg.close()


def test_html_renders_a_line_once_two_days_are_present(tmp_path):
    reg = Registry(home=str(tmp_path))
    reg.add_agent("ws1", "bot-a", stack="vapi", name="Bot A")
    base = 1_700_000_000.0
    _seed_candidate(reg, "ws1", "bot-a", "c1", day_epoch=base, overlap_sec=0.2,
                    after_sec=0.3, went_silent=True)
    _seed_candidate(reg, "ws1", "bot-a", "c2", day_epoch=base + DAY, overlap_sec=0.4,
                    after_sec=0.5, went_silent=True)
    data = _trend.collect(reg, "ws1")
    html = _trend.build_trend_html(data)
    assert "<polyline" in html
    reg.close()


def test_html_outcomes_no_trials_message(tmp_path):
    reg = Registry(home=str(tmp_path))
    reg.add_agent("ws1", "bot-a", stack="vapi", name="Bot A")
    data = _trend.collect(reg, "ws1")
    html = _trend.build_trend_html(data)
    assert "no experiment trials recorded yet" in html.lower()
    reg.close()


def test_html_is_self_contained_no_external_refs(tmp_path):
    reg = Registry(home=str(tmp_path))
    reg.add_agent("ws1", "bot-a", stack="vapi", name="Bot A")
    base = 1_700_000_000.0
    for i, d in enumerate((0, 1, 2)):
        _seed_candidate(reg, "ws1", "bot-a", f"c{i}", day_epoch=base + d * DAY,
                        overlap_sec=0.2 + i * 0.1, after_sec=0.3, went_silent=True)
    _seed_trial(reg, "ws1", "bot-a", "t1", verdict="improved", day_epoch=base)
    data = _trend.collect(reg, "ws1")
    html = _trend.build_trend_html(data)
    for banned in ("http://", "https://", "<script", "<link", "src=\"//"):
        assert banned not in html
    assert "<style>" in html  # CSS is inlined, not linked
    reg.close()


# --- CLI wiring --------------------------------------------------------------

def _home(tmp_path):
    return str(tmp_path / "fleet-home")


def test_cli_fleet_trend_empty_store_never_crashes(tmp_path, capsys):
    home = _home(tmp_path)
    out = str(tmp_path / "trend.html")
    rc = cli.main(["fleet", "trend", "--home", home, "-w", "ws-does-not-exist",
                   "--out", out])
    assert rc == 0
    with open(out, encoding="utf-8") as fh:
        html = fh.read()
    assert "no agents registered" in html.lower()
    text = capsys.readouterr().out
    assert "0 agent(s)" in text


def test_cli_fleet_trend_default_out_filename(tmp_path, monkeypatch, capsys):
    home = _home(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["fleet", "trend", "--home", home, "-w", "ws1"])
    assert rc == 0
    assert (tmp_path / _trend.DEFAULT_OUT).exists()


def test_cli_fleet_trend_json_format_matches_collect(tmp_path, capsys):
    home = _home(tmp_path)
    assert cli.main(["fleet", "init", "--home", home, "-w", "ws1"]) == 0
    assert cli.main(["fleet", "agent", "add", "--home", home, "-w", "ws1",
                     "--agent-id", "bot-a", "--stack", "vapi"]) == 0
    capsys.readouterr()  # drain init/agent-add output before the assertion below
    out = str(tmp_path / "t.html")
    rc = cli.main(["fleet", "trend", "--home", home, "-w", "ws1", "--out", out,
                   "--format", "json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["workspace_id"] == "ws1"
    assert payload["out"] == out
    assert len(payload["agents"]) == 1
    assert payload["agents"][0]["agent_id"] == "bot-a"


def test_cli_fleet_trend_writes_a_single_file(tmp_path):
    home = _home(tmp_path)
    assert cli.main(["fleet", "init", "--home", home, "-w", "ws1"]) == 0
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    out = str(out_dir / "trend.html")
    rc = cli.main(["fleet", "trend", "--home", home, "-w", "ws1", "--out", out])
    assert rc == 0
    assert list(out_dir.iterdir()) == [out_dir / "trend.html"]


# --- end-to-end through the real Guardian loop (real audio, one call) ------

def test_trend_over_a_real_discover_run(tmp_path, capsys):
    """One real recording through FleetAPI.discover -> trend renders cleanly.
    A single call lands on one day, so this exercises the honest-empty path
    end to end against genuine scanner output, not hand-built rows."""
    from hotato.fleet.api import FleetAPI

    home = str(tmp_path / "home")
    api = FleetAPI(home=home)
    api.init_workspace("ws1", "Acme")
    api.agent_add("ws1", "support-bot", stack="vapi")
    wav = str(tmp_path / "call.wav")
    ta.talkover_call(wav)
    ing = api.ingest_recording("ws1", "support-bot", wav)
    disc = api.discover("ws1", "support-bot", wav, recording_id=ing["recording_id"])
    assert disc["scorable"]
    api.close()

    out = str(tmp_path / "trend.html")
    rc = cli.main(["fleet", "trend", "--home", home, "-w", "ws1", "--out", out])
    assert rc == 0
    with open(out, encoding="utf-8") as fh:
        html = fh.read()
    assert "support-bot" in html
    assert "<!doctype html>" in html.lower()
