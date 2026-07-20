"""The curated, seeded persona/scenario pack (``hotato.simulate_pack``).

Pins the properties that make the pack a table-stake rather than a demo:

* the manifest and the packaged ``*.scenario.json`` files agree exactly (no
  orphan file, no orphan manifest entry);
* every pack entry is a valid ``hotato.scenario.v1`` (Python validator, and the
  JSON Schema when ``jsonschema`` is installed);
* every ``(pack scenario, pinned seed)`` renders faithfully (``sim=ok``, never
  ``SIMULATOR_INVALID``) into a labelled ``origin=simulated`` conversation;
* every ``(pack scenario, pinned seed)`` render is BYTE-IDENTICAL across two
  runs -- the wedge -- both in-memory (content_hash + transcript + trace) and on
  disk (the written ``transcript.json`` / ``trace.jsonl`` bytes);
* the pack carries no ``overall_score`` and the common test cases the pack is
  meant to cover are all present;
* the CLI lists the pack (``--list``) and runs an entry BY NAME
  (``hotato simulate <name>``), with a local file always winning over a name.
"""

import json
from importlib import resources

import pytest

from hotato import cli
from hotato import simulate as SIM
from hotato import simulate_pack as PACK

# The common voice-agent test cases the pack is curated to cover.
_EXPECTED_NAMES = {
    "barge-in-missed",
    "backchannel-not-floor-take",
    "dead-air-silence",
    "over-eager-early-response",
    "caller-talk-over",
    "fast-interrupter",
    "slow-speaker",
}


def _pack_dir():
    return resources.files("hotato").joinpath(*PACK.PACK_DIR)


def _files_on_disk():
    return sorted(
        p.name for p in _pack_dir().iterdir()
        if p.name.endswith(".scenario.json")
    )


# --------------------------------------------------------------------------
# manifest <-> files agree; the curated set is present
# --------------------------------------------------------------------------

def test_manifest_loads_and_lists_the_curated_set():
    entries = PACK.list_entries()
    assert {e["name"] for e in entries} == _EXPECTED_NAMES
    # list_entries is sorted (byte-stable) regardless of manifest order
    assert [e["name"] for e in entries] == sorted(e["name"] for e in entries)


def test_manifest_and_scenario_files_agree_exactly():
    manifest_files = sorted(e["file"] for e in PACK.list_entries())
    assert manifest_files == _files_on_disk()
    # every entry's file actually resolves to a readable packaged resource
    for e in PACK.list_entries():
        assert _pack_dir().joinpath(e["file"]).is_file()


# --------------------------------------------------------------------------
# every entry is a valid scenario.v1 (Python validator + JSON Schema)
# --------------------------------------------------------------------------

def test_every_pack_scenario_validates_scenario_v1():
    for name in PACK.names():
        doc = PACK.load_scenario(name)
        assert doc["kind"] == "hotato.scenario" and doc["version"] == 1
        # each entry pins a fixed seed (the reproducibility contract)
        assert isinstance(doc["seed"], int)


def test_every_pack_scenario_validates_against_json_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        resources.files("hotato").joinpath("schema", "scenario.v1.json")
        .read_text(encoding="utf-8")
    )
    for name in PACK.names():
        # validate the RAW file bytes, not the normalized doc
        text = _pack_dir().joinpath(f"{name}.scenario.json").read_text(encoding="utf-8")
        jsonschema.validate(instance=json.loads(text), schema=schema)


def test_pinned_seed_matches_the_scenario_file():
    for e in PACK.list_entries():
        doc = PACK.load_scenario(e["name"])
        assert doc["seed"] == e["seed"]


# --------------------------------------------------------------------------
# every (scenario, pinned seed) renders faithfully -> origin=simulated
# --------------------------------------------------------------------------

def test_every_pack_scenario_renders_sim_ok_and_simulated():
    for e in PACK.list_entries():
        doc = PACK.load_scenario(e["name"])
        rendered = SIM.render(doc, e["seed"])
        assert rendered["origin"]["kind"] == "simulated"
        verdict = SIM.validate_simulation(doc, rendered)
        assert verdict["ok"], (e["name"], verdict.get("reason"))
        # the caller only ever speaks as the caller (never solves it for the agent)
        assert all(
            s["role"] == "caller"
            for s in rendered["transcript"]["segments"]
        )


def test_no_overall_score_anywhere_in_the_pack():
    for name in PACK.names():
        text = _pack_dir().joinpath(f"{name}.scenario.json").read_text(encoding="utf-8")
        assert "overall_score" not in text


# --------------------------------------------------------------------------
# the wedge: a fixed (scenario, seed) is BYTE-IDENTICAL across two runs
# --------------------------------------------------------------------------

def test_every_pack_render_is_byte_identical_across_two_runs():
    for e in PACK.list_entries():
        doc = PACK.load_scenario(e["name"])
        a = SIM.render(doc, e["seed"])
        b = SIM.render(doc, e["seed"])
        assert a["content_hash"] == b["content_hash"], e["name"]
        assert a["transcript"] == b["transcript"], e["name"]
        assert a["trace"] == b["trace"], e["name"]


def test_every_pack_write_is_byte_identical_across_two_runs(tmp_path):
    for e in PACK.list_entries():
        doc = PACK.load_scenario(e["name"])
        SIM.run_scripted(doc, e["seed"], out_dir=str(tmp_path / (e["name"] + "-1")),
                         created_at="2026-07-20T00:00:00Z")
        SIM.run_scripted(doc, e["seed"], out_dir=str(tmp_path / (e["name"] + "-2")),
                         created_at="2026-07-20T00:00:00Z")
        for fname in ("transcript.json", "trace.jsonl", "conversation.json"):
            one = (tmp_path / (e["name"] + "-1") / fname).read_bytes()
            two = (tmp_path / (e["name"] + "-2") / fname).read_bytes()
            assert one == two, (e["name"], fname)


def test_pack_scenarios_are_seed_invariant_by_construction():
    # every pack entry uses backchannel probability 0.0 or 1.0, so it is not only
    # byte-identical at its pinned seed but seed-invariant -- robust to a --seed
    # override. This is the pack's design, verified here.
    for e in PACK.list_entries():
        doc = PACK.load_scenario(e["name"])
        pinned = SIM.render(doc, e["seed"])["content_hash"]
        other = SIM.render(doc, e["seed"] + 987654)["content_hash"]
        assert pinned == other, e["name"]


# --------------------------------------------------------------------------
# the pack covers its intended perturbations (not just that files parse)
# --------------------------------------------------------------------------

def test_pack_covers_interruptions_and_backchannels():
    # at least one entry declares a barge-in interruption ...
    interrupters = []
    backchannelers = []
    for name in PACK.names():
        doc = PACK.load_scenario(name)
        beh = doc["caller"].get("behavior") or {}
        if beh.get("interruptions"):
            interrupters.append(name)
        if float((beh.get("backchannels") or {}).get("probability", 0.0)) > 0:
            backchannelers.append(name)
    assert "barge-in-missed" in interrupters
    assert "caller-talk-over" in interrupters
    # ... and at least one exercises backchannels rendered as backchannel spans
    assert "backchannel-not-floor-take" in backchannelers
    doc = PACK.load_scenario("backchannel-not-floor-take")
    spans = SIM.render(doc, 202)["trace"]["spans"]
    assert any(s["type"] == "backchannel" for s in spans)


def test_slow_speaker_keeps_its_declared_fact():
    doc = PACK.load_scenario("slow-speaker")
    rendered = SIM.render(doc, doc["seed"])
    spoken = " ".join(
        s["text"] for s in rendered["transcript"]["segments"]
    ).lower()
    assert "m-7788" in spoken


# --------------------------------------------------------------------------
# CLI: --list, run by name, local file wins, unknown name
# --------------------------------------------------------------------------

def test_cli_simulate_list_text_lists_every_entry(capsys):
    code = cli.main(["simulate", "--list"])
    assert code == 0
    out = capsys.readouterr().out
    assert PACK.PACK_NAME in out
    for name in _EXPECTED_NAMES:
        assert name in out


def test_cli_simulate_list_json(capsys):
    code = cli.main(["simulate", "--list", "--format", "json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "simulate-pack"
    assert payload["count"] == len(_EXPECTED_NAMES)
    assert {e["name"] for e in payload["scenarios"]} == _EXPECTED_NAMES
    assert "overall_score" not in json.dumps(payload)


def test_cli_simulate_by_name_produces_simulated_artifact(tmp_path, capsys):
    out = tmp_path / "sim"
    code = cli.main(["simulate", "barge-in-missed", "--out", str(out),
                     "--created-at", "2026-07-20T00:00:00Z"])
    assert code == 0
    capsys.readouterr()
    manifest = json.loads((out / "conversation.json").read_text(encoding="utf-8"))
    assert manifest["origin"]["kind"] == "simulated"
    # and `hotato conversation verify` accepts the produced artifact
    assert cli.main(["conversation", "verify", str(out)]) == 0
    assert "VERIFIED" in capsys.readouterr().out


def test_cli_simulate_by_name_is_byte_reproducible(tmp_path, capsys):
    # The DOCUMENTED command (`hotato simulate <name> --out DIR`, no --created-at)
    # writes a byte-identical bundle every run: the manifest created_at defaults
    # to a reproducible instant, never the wall clock, so conversation.json,
    # transcript.json, AND trace.jsonl match across two runs on any machine.
    one = tmp_path / "one"
    two = tmp_path / "two"
    for out in (one, two):
        code = cli.main(["simulate", "slow-speaker", "--out", str(out)])
        assert code == 0
        capsys.readouterr()
    names_one = sorted(p.name for p in one.iterdir())
    names_two = sorted(p.name for p in two.iterdir())
    assert names_one == names_two
    assert "conversation.json" in names_one and "trace.jsonl" in names_one
    for name in names_one:
        assert (one / name).read_bytes() == (two / name).read_bytes(), name


def test_cli_simulate_by_name_created_at_defaults_to_reproducible_instant(
        tmp_path, capsys):
    # The default manifest created_at is a fixed reproducible instant (epoch 0),
    # not the wall clock -- this is WHY the bare command is byte-identical, and
    # --created-at still pins an explicit timestamp when a real one is wanted.
    out = tmp_path / "sim"
    assert cli.main(["simulate", "slow-speaker", "--out", str(out)]) == 0
    capsys.readouterr()
    manifest = json.loads((out / "conversation.json").read_text(encoding="utf-8"))
    assert manifest["created_at"] == "1970-01-01T00:00:00Z"

    pinned = tmp_path / "pinned"
    assert cli.main(["simulate", "slow-speaker", "--out", str(pinned),
                     "--created-at", "2026-07-20T00:00:00Z"]) == 0
    capsys.readouterr()
    pinned_manifest = json.loads(
        (pinned / "conversation.json").read_text(encoding="utf-8"))
    assert pinned_manifest["created_at"] == "2026-07-20T00:00:00Z"


def test_local_file_wins_over_pack_name(tmp_path, capsys, monkeypatch):
    # a file whose name collides with a pack name is used AS THE FILE, never the
    # packaged pack entry (the pack is only consulted when the path is absent).
    doc = {
        "kind": "hotato.scenario", "version": 1, "id": "local-wins",
        "goal": {"type": "x", "target": "y"},
        "caller": {"script": [{"say": "this is the local file, not the pack"}]},
        "seed": 0,
    }
    p = tmp_path / "barge-in-missed"
    p.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    code = cli.main(["simulate", "barge-in-missed", "--format", "json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scenario_id"] == "local-wins"


def test_unknown_name_is_not_a_pack_name_and_errors():
    assert PACK.is_pack_name("no-such-scenario") is False
    with pytest.raises(ValueError):
        PACK.load_scenario("no-such-scenario")
    # the CLI treats an unknown, non-file reference as an unusable input (exit 2)
    assert cli.main(["simulate", "no-such-scenario"]) == 2
