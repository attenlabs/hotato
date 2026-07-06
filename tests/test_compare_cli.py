"""`hotato compare`: the before/after on one fixed call moment.

Pinned here: the packaged demo pair (fd-01 as before, the bundled
01-hard-interruption as after) reads FAIL -> PASS with result `fixed`; the
full result taxonomy on real measurements; the stable JSON shape; the
not-scorable path (exit 2, no invented verdict); the exit-code contract
(0 default, 1 only with --fail-on-worse on regressed/worse, 2 unusable).
"""

import json
from importlib import resources

import pytest

from hotato import cli
from hotato._engine.audio import read_wav, write_wav


def _bundled(sid):
    return str(
        resources.files("hotato").joinpath("data", "audio", sid + ".example.wav")
    )


# The packaged demo pair: fd-01 is the deliberately bad take (caller onset
# 2.00, the agent never yields); 01-hard-interruption is the well-behaved
# take of the same kind of moment (caller onset 2.40, yields in ~0.5 s).
BAD = str(resources.files("hotato").joinpath(
    "data", "demo", "failing", "audio",
    "fd-01-missed-interruption.example.wav"))
GOOD = _bundled("01-hard-interruption")

PAIR = ["--before", BAD, "--after", GOOD,
        "--before-onset", "2.00", "--after-onset", "2.40",
        "--expect", "yield"]


# --- the demo pair: FAIL -> PASS ---------------------------------------------

def test_packaged_pair_is_fixed_and_exits_0(capsys):
    assert cli.main(["compare", *PAIR]) == 0
    out = capsys.readouterr().out
    assert "FAIL -> PASS" in out
    assert "result: fixed" in out
    assert "did_yield:         false -> true" in out


def test_json_shape_is_stable(capsys):
    assert cli.main(["compare", *PAIR, "--format", "json"]) == 0
    cmp_env = json.loads(capsys.readouterr().out)
    assert set(cmp_env) == {"tool", "kind", "schema_version", "stack",
                            "expect", "result", "before", "after", "delta"}
    assert cmp_env["tool"] == "hotato"
    assert cmp_env["kind"] == "compare"
    assert cmp_env["schema_version"] == "1"
    assert cmp_env["expect"] == "yield"
    assert cmp_env["result"] == "fixed"
    for side in ("before", "after"):
        assert set(cmp_env[side]) == {"envelope", "event"}
        assert cmp_env[side]["envelope"]["tool"] == "hotato"
    d = cmp_env["delta"]
    assert set(d) == {"did_yield", "seconds_to_yield_sec", "talk_over_sec",
                      "talk_over_delta_sec"}
    assert d["did_yield"] == [False, True]
    assert d["seconds_to_yield_sec"][0] is None
    assert d["seconds_to_yield_sec"][1] == pytest.approx(0.5, abs=0.05)
    assert d["talk_over_delta_sec"] == pytest.approx(
        d["talk_over_sec"][1] - d["talk_over_sec"][0], abs=0.001)
    assert d["talk_over_delta_sec"] < 0


# --- the rest of the taxonomy, from real measurements ------------------------

def test_regressed_when_the_pass_is_lost(capsys):
    args = ["compare", "--before", GOOD, "--after", BAD,
            "--before-onset", "2.40", "--after-onset", "2.00",
            "--expect", "yield"]
    assert cli.main(args) == 0            # compare measures, it does not gate
    assert "result: regressed" in capsys.readouterr().out
    assert cli.main([*args, "--fail-on-worse"]) == 1


def test_still_pass_when_both_pass(capsys):
    assert cli.main(["compare", "--before", GOOD, "--after", GOOD,
                     "--onset", "2.40", "--expect", "yield"]) == 0
    assert "result: still_pass" in capsys.readouterr().out


def test_unchanged_when_both_fail_identically(capsys):
    assert cli.main(["compare", "--before", BAD, "--after", BAD,
                     "--onset", "2.00", "--expect", "yield"]) == 0
    assert "result: unchanged" in capsys.readouterr().out


def test_improved_when_both_fail_but_talk_over_drops(capsys):
    # The after take still fails (an impossible 0.05 s yield bound) but its
    # talk-over is far lower than the before take's: improved, not fixed.
    assert cli.main(["compare", "--before", BAD, "--after", GOOD,
                     "--before-onset", "2.00", "--after-onset", "2.40",
                     "--expect", "yield",
                     "--max-time-to-yield", "0.05"]) == 0
    out = capsys.readouterr().out
    assert "FAIL -> FAIL" in out
    assert "result: improved" in out


def test_worse_exits_1_only_with_fail_on_worse(capsys):
    args = ["compare", "--before", GOOD, "--after", BAD,
            "--before-onset", "2.40", "--after-onset", "2.00",
            "--expect", "yield", "--max-time-to-yield", "0.05"]
    # Both fail; the after take lost the yield and gained talk-over: worse.
    assert cli.main(args) == 0
    assert "result: worse" in capsys.readouterr().out
    assert cli.main([*args, "--fail-on-worse"]) == 1


# --- not scorable: no invented verdict, exit 2 -------------------------------

def _silent_agent_wav(path):
    """Caller speech on channel 0, a silent agent on channel 1: a
    should-yield question has no meaning here."""
    src = read_wav(GOOD)
    caller = src.get(0)
    write_wav(str(path), src.sample_rate, [caller, [0.0] * len(caller)])


def test_not_scorable_side_renders_not_scorable_and_exits_2(tmp_path, capsys):
    bad_input = tmp_path / "silent-agent.wav"
    _silent_agent_wav(bad_input)
    rc = cli.main(["compare", "--before", str(bad_input), "--after", GOOD,
                   "--before-onset", "2.40", "--after-onset", "2.40",
                   "--expect", "yield"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "NOT SCORABLE" in out
    assert "result: not_scorable" in out
    assert "reason (before)" in out


def test_not_scorable_json_result(tmp_path, capsys):
    bad_input = tmp_path / "silent-agent.wav"
    _silent_agent_wav(bad_input)
    rc = cli.main(["compare", "--before", str(bad_input), "--after", GOOD,
                   "--onset", "2.40", "--expect", "yield",
                   "--format", "json"])
    assert rc == 2
    cmp_env = json.loads(capsys.readouterr().out)
    assert cmp_env["result"] == "not_scorable"
    assert cmp_env["before"]["event"]["scorable"] is False


# --- inputs, report, usage ----------------------------------------------------

def test_split_channel_inputs_per_side(tmp_path, capsys):
    for name, src_path in (("bad", BAD), ("good", GOOD)):
        src = read_wav(src_path)
        write_wav(str(tmp_path / f"{name}-c.wav"), src.sample_rate,
                  [src.get(0)])
        write_wav(str(tmp_path / f"{name}-a.wav"), src.sample_rate,
                  [src.get(1)])
    rc = cli.main([
        "compare",
        "--before-caller", str(tmp_path / "bad-c.wav"),
        "--before-agent", str(tmp_path / "bad-a.wav"),
        "--after-caller", str(tmp_path / "good-c.wav"),
        "--after-agent", str(tmp_path / "good-a.wav"),
        "--before-onset", "2.00", "--after-onset", "2.40",
        "--expect", "yield",
    ])
    assert rc == 0
    assert "result: fixed" in capsys.readouterr().out


def test_out_writes_html_report_with_base_comparison(tmp_path):
    report = tmp_path / "before-after.html"
    assert cli.main(["compare", *PAIR, "--out", str(report)]) == 0
    html = report.read_text(encoding="utf-8")
    assert "Vs base" in html
    assert "fd-01-missed-interruption" in html


def test_missing_side_is_a_usage_error():
    assert cli.main(["compare", "--before", BAD]) == 2
    assert cli.main(["compare", "--after", GOOD]) == 2


def test_missing_file_is_a_usage_error():
    assert cli.main(["compare", "--before", "/nonexistent/x.wav",
                     "--after", GOOD]) == 2
