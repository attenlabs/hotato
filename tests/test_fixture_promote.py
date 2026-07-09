"""`hotato fixture promote`: one sweep/analyze candidate -> a permanent
regression fixture, by ref instead of by hand.

Pinned here: the two ref forms (FILE#N rank refs and FILE#CALL:N call-scoped
refs, both 1-based in the file's ranked candidate order -- the same #N rank
the report shows), the end-to-end promote from a real `hotato sweep --demo
--format json` result and a real `hotato analyze --format json` result,
source-recording resolution (the folder_path recorded in the result file,
plus the --folder override), the promote provenance, the created-fixture
output block, and the actionable exit-2 refusals: a malformed ref, an
out-of-range number, an unknown call, a missing or foreign JSON, an
unresolvable recording, and a not-scorable candidate (partial outputs
removed).
"""

import json
import os
import shlex
import shutil
from importlib import resources

import pytest

from hotato import cli
from hotato import fixture as fixture_mod
from hotato._engine.audio import read_wav


def _bundled(sid):
    return str(
        resources.files("hotato").joinpath("data", "audio", sid + ".example.wav")
    )


def _scenario(fx_root, fid):
    with open(fx_root / "scenarios" / (fid + ".json"), encoding="utf-8") as fh:
        return json.load(fh)


# --- ref parsing: FILE#N and FILE#CALL:N -------------------------------------

def test_rank_ref_parses():
    assert fixture_mod.parse_candidate_ref("hotato-sweep.json#3") == (
        "hotato-sweep.json", None, 3)


def test_call_ref_parses():
    assert fixture_mod.parse_candidate_ref("analyze.json#call_abc123:2") == (
        "analyze.json", "call_abc123", 2)


def test_call_ref_keeps_colons_inside_the_call_name():
    # Only the LAST colon splits the number off, so a call id that itself
    # contains colons still round-trips.
    assert fixture_mod.parse_candidate_ref("a.json#sip:host:5060:7") == (
        "a.json", "sip:host:5060", 7)


def test_path_containing_a_hash_still_parses():
    # Only the LAST # splits the ref, so a path with a # in it survives.
    assert fixture_mod.parse_candidate_ref("dir#v2/sweep.json#1") == (
        "dir#v2/sweep.json", None, 1)


@pytest.mark.parametrize("ref", [
    "hotato-sweep.json",       # no #
    "#3",                      # no file
    "hotato-sweep.json#",      # no number
    "hotato-sweep.json#three", # not a number
    "hotato-sweep.json#call:", # call form with no number
    "hotato-sweep.json#:2",    # call form with no call
    "hotato-sweep.json#call:x",
])
def test_malformed_ref_names_both_forms(ref):
    with pytest.raises(ValueError) as exc:
        fixture_mod.parse_candidate_ref(ref)
    msg = str(exc.value)
    assert "FILE#N" in msg and "FILE#CALL:N" in msg


def test_numbers_are_one_based_zero_is_rejected():
    with pytest.raises(ValueError) as exc:
        fixture_mod.parse_candidate_ref("hotato-sweep.json#0")
    assert "start at 1" in str(exc.value)


# --- end to end from a real sweep --demo result -------------------------------

@pytest.fixture()
def sweep_json(tmp_path, capsys, monkeypatch):
    """A real `hotato sweep --demo --format json` result on disk, exactly the
    file a user redirects stdout into."""
    monkeypatch.setenv("HOTATO_HOME", str(tmp_path / "home"))
    assert cli.main(["sweep", "--demo", "--format", "json"]) == 0
    path = tmp_path / "hotato-sweep.json"
    path.write_text(capsys.readouterr().out, encoding="utf-8")
    return path


def _first_overlap_rank(doc):
    """The 1-based rank of the first overlap candidate (the promotable
    archetype: the agent is talking at the onset, so a yield label scores)."""
    for i, c in enumerate(doc["candidates"], 1):
        if c["kind"] == "overlap_while_agent_talking":
            return i, c
    raise AssertionError("the demo sweep surfaced no overlap candidate")


def test_promote_rank_ref_end_to_end(sweep_json, tmp_path, capsys):
    doc = json.loads(sweep_json.read_text(encoding="utf-8"))
    rank, cand = _first_overlap_rank(doc)
    fx = tmp_path / "fx"
    rc = cli.main([
        "fixture", "promote", f"{sweep_json}#{rank}",
        "--expect", "yield", "--id", "sweep-overlap-001", "--out", str(fx),
    ])
    assert rc == 0
    assert (fx / "scenarios" / "sweep-overlap-001.json").exists()
    assert (fx / "audio" / "sweep-overlap-001.example.wav").exists()
    wav = read_wav(str(fx / "audio" / "sweep-overlap-001.example.wav"))
    assert wav.num_channels == 2

    sc = _scenario(fx, "sweep-overlap-001")
    # The candidate's onset and recording, not hand-typed flags.
    assert sc["provenance"]["source_onset_sec"] == pytest.approx(
        cand["t_sec"], abs=0.001)
    assert sc["provenance"]["source"] == os.path.basename(cand["source"])
    assert sc["provenance"]["created_by"] == "hotato fixture promote"
    assert sc["provenance"]["candidate_ref"] == f"{sweep_json}#{rank}"
    assert sc["provenance"]["candidate_kind"] == "overlap_while_agent_talking"

    out = capsys.readouterr().out
    assert f"promoted {sweep_json}#{rank}:" in out
    assert "created Hotato fixture: sweep-overlap-001" in out
    assert "check:    scorable" in out
    assert "next:" in out and "hotato run --scenarios" in out


def test_ref_number_is_the_rank_the_report_shows(sweep_json, tmp_path):
    """#2 is the SECOND candidate in the file's ranked order (1-based), the
    same number the HTML report's rank chip shows."""
    doc = json.loads(sweep_json.read_text(encoding="utf-8"))
    second = doc["candidates"][1]
    rc = cli.main([
        "fixture", "promote", f"{sweep_json}#2",
        "--expect", "yield", "--id", "sweep-rank-002",
        "--out", str(tmp_path / "fx"),
    ])
    assert rc == 0
    sc = _scenario(tmp_path / "fx", "sweep-rank-002")
    assert sc["provenance"]["source_onset_sec"] == pytest.approx(
        second["t_sec"], abs=0.001)
    assert sc["provenance"]["source"] == os.path.basename(second["source"])


def test_call_scoped_ref_counts_within_that_call(sweep_json, tmp_path):
    """FILE#CALL:N takes the Nth candidate FROM THAT CALL in rank order; the
    call answers to its file name with the extensions stripped."""
    doc = json.loads(sweep_json.read_text(encoding="utf-8"))
    call = "fd-01-missed-interruption"
    expected = [c for c in doc["candidates"]
                if os.path.basename(c["source"]).startswith(call)][0]
    rc = cli.main([
        "fixture", "promote", f"{sweep_json}#{call}:1",
        "--expect", "yield", "--id", "sweep-call-001",
        "--out", str(tmp_path / "fx"),
    ])
    assert rc == 0
    sc = _scenario(tmp_path / "fx", "sweep-call-001")
    assert sc["provenance"]["source_onset_sec"] == pytest.approx(
        expected["t_sec"], abs=0.001)
    assert sc["provenance"]["source"] == os.path.basename(expected["source"])


def test_json_output_carries_the_candidate_block_and_next(sweep_json, tmp_path,
                                                          capsys):
    doc = json.loads(sweep_json.read_text(encoding="utf-8"))
    rank, cand = _first_overlap_rank(doc)
    rc = cli.main([
        "fixture", "promote", f"{sweep_json}#{rank}",
        "--expect", "yield", "--id", "sweep-json-001",
        "--out", str(tmp_path / "fx"), "--format", "json",
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["tool"] == "hotato"
    assert out["kind"] == "fixture"
    assert out["candidate"] == {
        "ref": f"{sweep_json}#{rank}",
        "source": cand["source"],
        "t_sec": cand["t_sec"],
        "kind": cand["kind"],
        "salience": cand["salience"],
    }
    assert out["validation"]["events"][0]["event_id"] == "sweep-json-001"
    assert out["next"].startswith("hotato run --scenarios ")


def test_next_command_from_promote_runs_verbatim(sweep_json, tmp_path, capsys):
    doc = json.loads(sweep_json.read_text(encoding="utf-8"))
    rank, _ = _first_overlap_rank(doc)
    rc = cli.main([
        "fixture", "promote", f"{sweep_json}#{rank}",
        "--expect", "yield", "--id", "sweep-next-001",
        "--out", str(tmp_path / "fx"), "--format", "json",
    ])
    assert rc == 0
    next_cmd = json.loads(capsys.readouterr().out)["next"]
    argv = shlex.split(next_cmd)[1:]  # drop the leading "hotato"
    # A promoted fixture is allowed to FAIL its run: promoting a live bug is
    # the point. The contract here is that the emitted command scores the
    # fixture (exit 0 or 1), never a usage error.
    rc = cli.main(argv)
    assert rc in (0, 1)
    assert "sweep-next-001" in capsys.readouterr().out


# --- end to end from a real analyze result ------------------------------------

@pytest.fixture()
def analyze_json(tmp_path):
    """A real `hotato analyze --format json --out` result over a folder with
    one call named the canon way (call_abc123.wav)."""
    calls = tmp_path / "calls"
    calls.mkdir()
    shutil.copy(_bundled("01-hard-interruption"), calls / "call_abc123.wav")
    out = tmp_path / "analyze.json"
    assert cli.main(["analyze", str(calls), "--format", "json",
                     "--out", str(out)]) == 0
    return out


def test_promote_the_canon_analyze_ref(analyze_json, tmp_path, capsys):
    rc = cli.main([
        "fixture", "promote", f"{analyze_json}#call_abc123:1",
        "--expect", "yield", "--id", "refund-cutoff-001",
        "--out", str(tmp_path / "fx"),
    ])
    assert rc == 0
    sc = _scenario(tmp_path / "fx", "refund-cutoff-001")
    assert sc["provenance"]["source"] == "call_abc123.wav"
    assert sc["category"] == "should_yield"
    out = capsys.readouterr().out
    assert "created Hotato fixture: refund-cutoff-001" in out
    assert "check:    scorable" in out


def test_promote_resolves_the_recording_from_anywhere(analyze_json, tmp_path,
                                                      monkeypatch):
    """The result file records the analyzed folder's absolute path
    (folder_path), so a promote works from a different working directory."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    rc = cli.main([
        "fixture", "promote", f"{analyze_json}#1",
        "--expect", "yield", "--id", "moved-cwd-001",
        "--out", str(tmp_path / "fx"),
    ])
    assert rc == 0


def _rewrite_folders(analyze_json, dst, *, folder_path, folder):
    doc = json.loads(analyze_json.read_text(encoding="utf-8"))
    doc["folder_path"] = folder_path
    doc["folder"] = folder
    dst.write_text(json.dumps(doc), encoding="utf-8")
    return dst


def test_folder_override_resolves_a_moved_result(analyze_json, tmp_path):
    stale = _rewrite_folders(analyze_json, tmp_path / "stale.json",
                             folder_path="/nonexistent-hotato-xyz",
                             folder="nonexistent-hotato-xyz")
    rc = cli.main([
        "fixture", "promote", f"{stale}#1",
        "--expect", "yield", "--id", "moved-folder-001",
        "--out", str(tmp_path / "fx"),
        "--folder", str(tmp_path / "calls"),
    ])
    assert rc == 0


def test_folder_override_is_authoritative_when_it_misses(analyze_json,
                                                         tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    rc = cli.main([
        "fixture", "promote", f"{analyze_json}#1",
        "--expect", "yield", "--id", "miss-001",
        "--out", str(tmp_path / "fx"), "--folder", str(empty),
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--folder" in err and "call_abc123.wav" in err


def test_unresolvable_recording_names_tried_paths_and_suggests_folder(
        analyze_json, tmp_path, capsys, monkeypatch):
    stale = _rewrite_folders(analyze_json, tmp_path / "stale.json",
                             folder_path="/nonexistent-hotato-xyz",
                             folder="nonexistent-hotato-xyz")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    rc = cli.main([
        "fixture", "promote", f"{stale}#1",
        "--expect", "yield", "--id", "lost-001", "--out", str(tmp_path / "fx"),
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "was not found" in err
    assert "tried:" in err
    assert "pass --folder DIR" in err


# --- refusals: bad refs, foreign files, not-scorable candidates ---------------

def test_out_of_range_number_states_the_valid_range(analyze_json, tmp_path,
                                                    capsys):
    rc = cli.main([
        "fixture", "promote", f"{analyze_json}#5",
        "--expect", "yield", "--id", "oob-001", "--out", str(tmp_path / "fx"),
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "out of range" in err and "1..1" in err


def test_unknown_call_lists_the_calls_in_the_file(analyze_json, tmp_path,
                                                  capsys):
    rc = cli.main([
        "fixture", "promote", f"{analyze_json}#call_zzz:1",
        "--expect", "yield", "--id", "nocall-001",
        "--out", str(tmp_path / "fx"),
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "call_zzz" in err and "call_abc123" in err


def test_malformed_ref_is_a_clean_usage_error(analyze_json, tmp_path, capsys):
    rc = cli.main([
        "fixture", "promote", str(analyze_json),
        "--expect", "yield", "--id", "noref-001",
        "--out", str(tmp_path / "fx"),
    ])
    assert rc == 2
    assert "FILE#N" in capsys.readouterr().err


def test_missing_result_file_is_the_structured_json_error(tmp_path, capsys):
    rc = cli.main([
        "fixture", "promote", str(tmp_path / "nope.json") + "#1",
        "--expect", "yield", "--id", "miss-json-001",
        "--out", str(tmp_path / "fx"), "--format", "json",
    ])
    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error_code"] == "file_not_found"


def test_non_json_file_is_refused_with_the_reason(tmp_path, capsys):
    bad = tmp_path / "notes.json"
    bad.write_text("not json at all", encoding="utf-8")
    rc = cli.main([
        "fixture", "promote", f"{bad}#1",
        "--expect", "yield", "--id", "notjson-001",
        "--out", str(tmp_path / "fx"),
    ])
    assert rc == 2
    assert "is not JSON" in capsys.readouterr().err


def test_foreign_json_is_refused_with_the_expected_shape(tmp_path, capsys):
    # A hotato file of the wrong kind (a fixture envelope) is still foreign.
    foreign = tmp_path / "fixture.json"
    foreign.write_text(json.dumps({"tool": "hotato", "kind": "fixture"}),
                       encoding="utf-8")
    rc = cli.main([
        "fixture", "promote", f"{foreign}#1",
        "--expect", "yield", "--id", "foreign-001",
        "--out", str(tmp_path / "fx"),
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not a hotato sweep/analyze result" in err


def test_result_with_no_candidates_is_stated_plainly(tmp_path, capsys):
    empty = tmp_path / "clean.json"
    empty.write_text(json.dumps({"kind": "analyze", "candidates": []}),
                     encoding="utf-8")
    rc = cli.main([
        "fixture", "promote", f"{empty}#1",
        "--expect", "yield", "--id", "clean-001",
        "--out", str(tmp_path / "fx"),
    ])
    assert rc == 2
    assert "no candidates" in capsys.readouterr().err


def test_not_scorable_candidate_refused_with_reason_and_cleaned_up(tmp_path,
                                                                   capsys):
    # At 5.5 s in 01-hard-interruption the agent is long silent: a
    # should-yield label there has no meaning, so the promote is refused by
    # the same immediate-scoring validation `fixture create` runs, and the
    # partial outputs are removed.
    calls = tmp_path / "calls"
    calls.mkdir()
    shutil.copy(_bundled("01-hard-interruption"), calls / "call_abc123.wav")
    doc = {
        "tool": "hotato", "kind": "analyze", "schema_version": "1",
        "folder": "calls", "folder_path": str(calls),
        "total_candidates": 1,
        "candidates": [{"source": "call_abc123.wav", "t_sec": 5.5,
                        "kind": "long_response_gap", "salience": 2.0}],
    }
    ref_file = tmp_path / "analyze.json"
    ref_file.write_text(json.dumps(doc), encoding="utf-8")
    rc = cli.main([
        "fixture", "promote", f"{ref_file}#1",
        "--expect", "yield", "--id", "gap-as-yield-001",
        "--out", str(tmp_path / "fx"),
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not scorable" in err
    assert "agent was not talking" in err
    assert not (tmp_path / "fx" / "scenarios" / "gap-as-yield-001.json").exists()
    assert not (tmp_path / "fx" / "audio"
                / "gap-as-yield-001.example.wav").exists()


def test_overwrite_refused_without_force(analyze_json, tmp_path, capsys):
    argv = [
        "fixture", "promote", f"{analyze_json}#1",
        "--expect", "yield", "--id", "twice-001", "--out", str(tmp_path / "fx"),
    ]
    assert cli.main(argv) == 0
    assert cli.main(argv) == 2
    assert "--force" in capsys.readouterr().err
    assert cli.main(argv + ["--force"]) == 0


def test_invalid_slug_is_refused(analyze_json, tmp_path):
    rc = cli.main([
        "fixture", "promote", f"{analyze_json}#1",
        "--expect", "yield", "--id", "Not A Slug!",
        "--out", str(tmp_path / "fx"),
    ])
    assert rc == 2


# --- the promoted fixture round-trips through hotato run ----------------------

def test_promoted_fixture_round_trips_through_run(analyze_json, tmp_path):
    fx = tmp_path / "fx"
    assert cli.main([
        "fixture", "promote", f"{analyze_json}#call_abc123:1",
        "--expect", "yield", "--id", "roundtrip-001", "--out", str(fx),
    ]) == 0
    assert cli.main([
        "run", "--scenarios", str(fx / "scenarios"),
        "--audio", str(fx / "audio"),
    ]) == 0
