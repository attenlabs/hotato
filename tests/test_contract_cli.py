"""``hotato contract create/verify/inspect/pack/unpack``: the portable
failure-contract bundle.

Pinned here, against the canon in docs/CONTRACTS.md:

  * ``create`` from a real ``--from-candidate`` sweep result and from a raw
    ``--stereo`` WAV, both refused honestly (exit 2, nothing written) for a
    mono recording and for a not-scorable moment, both accepted (--force) for
    an id that already exists;
  * the bundle carries every file the canon lists: contract.json,
    audio/event.wav, evidence/{frames.jsonl,timeline.html,trust.json,
    card.svg}, traces/ (empty), source/{call_metadata.json,
    stack_config_snapshot.json}, policy/verify.yaml,
    reports/{initial.html,after.html}, provenance.json,
    ci/{github-action.yml,junit.xml};
  * contract.json validates against schema/contract.v1.json;
  * redaction by default (a candidate ref / source name is hidden unless
    --include-identifiers);
  * ``verify`` re-scores a directory (or one bundle) of contracts, emits
    text/json/html/junit, and its exit code is CI's pass/fail signal;
  * ``pack``/``unpack`` round-trip a bundle through one deterministic
    ``.hotato`` archive and catch a corrupted archive by sha256;
  * the diarized-mono opt-in path (``--mono --diarize``) never silently
    upgrades an indicative-only verdict, and frame-level evidence is
    honestly reported as unavailable rather than fabricated.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import pathlib
import struct
import tempfile
import wave
import xml.etree.ElementTree as ET
import zipfile
from importlib import resources

import pytest

from hotato import analyze as _analyze
from hotato import cli
from hotato import contract as _contract
from hotato import diarize as _diarize

HARD = str(resources.files("hotato").joinpath(
    "data", "audio", "01-hard-interruption.example.wav"))          # yields at 2.40
BACKCHANNEL = str(resources.files("hotato").joinpath(
    "data", "audio", "02-backchannel-mhm.example.wav"))            # holds at 2.10


def _bundle(tmp_path, cid):
    return tmp_path / (cid + ".hotato")


def _create(tmp_path, *extra, src=HARD, onset="2.40", expect="yield",
           cid="ct-created-001", out=None):
    out_dir = out if out is not None else tmp_path
    return cli.main([
        "contract", "create", "--stereo", src, "--id", cid,
        "--onset", onset, "--expect", expect, "--out", str(out_dir),
        *extra,
    ])


def _contract_json(tmp_path, cid):
    with open(_bundle(tmp_path, cid) / "contract.json", encoding="utf-8") as fh:
        return json.load(fh)


def _write_mono(path, segments, *, duration_sec=6.0, sr=16000):
    n = int(duration_sec * sr)

    def _on(t):
        return any(s <= t < e for s, e in segments)

    frames = bytearray()
    for i in range(n):
        t = i / sr
        v = int(0.35 * 32767 * math.sin(2 * math.pi * 220.0 * i / sr)) if _on(t) else 0
        frames += struct.pack("<h", v)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))
    return str(path)


# --- create: from a raw --stereo WAV ---------------------------------------

def test_create_from_stereo_writes_the_full_bundle(tmp_path):
    assert _create(tmp_path) == 0
    b = _bundle(tmp_path, "ct-created-001")
    for rel in (
        "contract.json", "audio/event.wav",
        "evidence/frames.jsonl", "evidence/timeline.html",
        "evidence/trust.json", "evidence/card.svg",
        "source/call_metadata.json", "source/stack_config_snapshot.json",
        "policy/verify.yaml", "reports/initial.html", "reports/after.html",
        "provenance.json", "ci/github-action.yml", "ci/junit.xml",
        "traces/.gitkeep",
    ):
        assert (b / rel).exists(), rel


def test_audio_event_wav_is_two_channel(tmp_path):
    from hotato._engine.audio import read_wav

    assert _create(tmp_path) == 0
    wav = read_wav(str(_bundle(tmp_path, "ct-created-001") / "audio" / "event.wav"))
    assert wav.num_channels == 2


def test_contract_json_shape(tmp_path):
    assert _create(tmp_path) == 0
    c = _contract_json(tmp_path, "ct-created-001")
    assert c["schema"] == "hotato.contract.v1"
    assert c["kind"] == "voice-turn-taking-contract"
    assert c["id"] == "ct-created-001"
    assert c["label"] == {"expected_behavior": "yield", "label_source": "human",
                          "rationale": None}
    assert c["source"]["recording_type"] == "stereo"
    assert c["source"]["channels"] == 2
    assert len(c["source"]["source_audio_sha256"]) == 64
    assert c["measurement"]["scorable"] is True
    assert c["measurement"]["passed"] is True
    assert c["measurement"]["did_yield"] is True
    assert c["measurement"]["indicative_only"] is False
    assert c["trust"]["scorable"] is True
    assert c["policy"]["pass_conditions"]["yield"] is True
    assert c["bundle"]["paths"]["audio"] == "audio/event.wav"
    assert c["bundle"]["paths"]["evidence"]["frames"] == "evidence/frames.jsonl"


def test_hold_label_gets_null_pass_bounds(tmp_path):
    assert _create(tmp_path, src=BACKCHANNEL, onset="2.10", expect="hold",
                  cid="ct-hold-001") == 0
    c = _contract_json(tmp_path, "ct-hold-001")
    assert c["label"]["expected_behavior"] == "hold"
    assert c["policy"]["pass_conditions"] == {
        "yield": False, "max_talk_over_sec": None, "max_time_to_yield_sec": None,
    }
    assert "require_hold_fixture: true" in (
        (_bundle(tmp_path, "ct-hold-001") / "policy" / "verify.yaml").read_text()
    )


def test_yield_policy_yaml_requires_yield_fixture(tmp_path):
    assert _create(tmp_path) == 0
    text = (_bundle(tmp_path, "ct-created-001") / "policy" / "verify.yaml").read_text()
    assert "require_yield_fixture: true" in text
    assert "target:" in text and "improve:" in text


def test_frames_jsonl_is_valid_jsonl_with_a_meta_header(tmp_path):
    assert _create(tmp_path) == 0
    lines = (_bundle(tmp_path, "ct-created-001") / "evidence" / "frames.jsonl").read_text().splitlines()
    assert lines
    meta = json.loads(lines[0])
    assert meta["_meta"] is True
    assert meta["sample_rate"] == 16000
    for ln in lines[1:]:
        row = json.loads(ln)
        assert "t_sec" in row


def test_ci_junit_reflects_the_created_contract(tmp_path):
    assert _create(tmp_path) == 0
    xml = (_bundle(tmp_path, "ct-created-001") / "ci" / "junit.xml").read_text()
    root = ET.fromstring(xml)
    assert root.attrib["tests"] == "1"
    assert root.attrib["failures"] == "0"
    assert root.find("testcase").attrib["name"] == "ct-created-001"


def test_ci_github_action_scaffold_runs_contract_verify(tmp_path):
    assert _create(tmp_path) == 0
    text = (_bundle(tmp_path, "ct-created-001") / "ci" / "github-action.yml").read_text()
    assert "hotato contract verify" in text


def test_call_metadata_is_redacted_by_default(tmp_path):
    assert _create(tmp_path) == 0
    meta = json.loads(
        (_bundle(tmp_path, "ct-created-001") / "source" / "call_metadata.json").read_text()
    )
    assert "source_name" not in meta
    assert "redacted by default" in meta["note"]


# --- create: from --from-candidate ------------------------------------------

def _demo_sweep_json(tmp_path):
    audio = str(resources.files("hotato").joinpath("data", "demo", "failing", "audio"))
    aggregate, _ = _analyze.analyze_folder(audio)
    p = tmp_path / "hotato-sweep.json"
    p.write_text(json.dumps(aggregate), encoding="utf-8")
    return p


def test_create_from_candidate_ref(tmp_path):
    sweep = _demo_sweep_json(tmp_path)
    rc = cli.main([
        "contract", "create", "--from-candidate", f"{sweep}#1",
        "--expect", "yield", "--id", "ct-cand-001", "--out", str(tmp_path),
    ])
    assert rc == 0
    c = _contract_json(tmp_path, "ct-cand-001")
    assert c["source"]["candidate_kind"] is not None
    # redacted by default: not the literal ref
    assert c["source"]["candidate_ref"] != f"{sweep}#1"
    assert "redacted" in c["source"]["candidate_ref"]


def test_create_from_candidate_ref_include_identifiers_shows_it(tmp_path):
    sweep = _demo_sweep_json(tmp_path)
    rc = cli.main([
        "contract", "create", "--from-candidate", f"{sweep}#1",
        "--expect", "yield", "--id", "ct-cand-002", "--out", str(tmp_path),
        "--include-identifiers",
    ])
    assert rc == 0
    c = _contract_json(tmp_path, "ct-cand-002")
    assert c["source"]["candidate_ref"] == f"{sweep}#1"
    meta = json.loads(
        (_bundle(tmp_path, "ct-cand-002") / "source" / "call_metadata.json").read_text()
    )
    assert meta["candidate_ref"] == f"{sweep}#1"
    assert "source_name" in meta


# --- refusals: mono, not-scorable, exactly-one-input, bad slug -------------

def test_mono_as_stereo_is_refused_by_default(tmp_path, capsys):
    mono = _write_mono(tmp_path / "mono.wav", segments=[(0.2, 5.8)])
    rc = _create(tmp_path, src=mono, cid="ct-mono-001")
    assert rc == 2
    assert "one channel" in capsys.readouterr().err
    assert not _bundle(tmp_path, "ct-mono-001").exists()


def test_not_scorable_moment_is_refused_and_nothing_is_written(tmp_path, capsys):
    # 5.5s: the agent is long silent in 01-hard-interruption, so a should-yield
    # label there is meaningless (mirrors fixture create's own refusal).
    rc = _create(tmp_path, onset="5.5", cid="ct-bad-001")
    assert rc == 2
    err = capsys.readouterr().err
    assert "not scorable" in err
    assert not _bundle(tmp_path, "ct-bad-001").exists()


def test_exactly_one_input_mode_is_required(tmp_path):
    assert cli.main([
        "contract", "create", "--stereo", HARD, "--caller", "x.wav",
        "--agent", "y.wav", "--id", "ct-x", "--onset", "1.0",
        "--expect", "yield", "--out", str(tmp_path),
    ]) == 2
    assert cli.main([
        "contract", "create", "--id", "ct-x", "--expect", "yield",
        "--out", str(tmp_path),
    ]) == 2


def test_invalid_slug_is_refused(tmp_path):
    assert _create(tmp_path, cid="Not_A_Slug!") == 2


def test_missing_onset_with_stereo_is_a_usage_error(tmp_path):
    assert cli.main([
        "contract", "create", "--stereo", HARD, "--id", "ct-noonset",
        "--expect", "yield", "--out", str(tmp_path),
    ]) == 2


# --- overwrite / force -------------------------------------------------------

def test_overwrite_refused_without_force_then_forced(tmp_path, capsys):
    assert _create(tmp_path) == 0
    assert _create(tmp_path) == 2
    assert "--force" in capsys.readouterr().err
    assert _create(tmp_path, "--force") == 0


# --- output formats -----------------------------------------------------

def test_json_output_shape(tmp_path, capsys):
    assert _create(tmp_path, "--format", "json") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["tool"] == "hotato"
    assert out["kind"] == "contract"
    assert out["contract"]["id"] == "ct-created-001"
    assert out["next"].startswith("hotato contract verify")


def test_text_output_states_pass_fail_and_next(tmp_path, capsys):
    assert _create(tmp_path) == 0
    out = capsys.readouterr().out
    assert "created hotato contract: ct-created-001" in out
    assert "passed:   True" in out
    assert "hotato contract verify" in out


# --- schema validation -------------------------------------------------------

def test_contract_json_validates_against_its_schema(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        resources.files("hotato").joinpath("schema", "contract.v1.json")
        .read_text(encoding="utf-8")
    )
    assert _create(tmp_path) == 0
    jsonschema.validate(instance=_contract_json(tmp_path, "ct-created-001"), schema=schema)


def test_schema_rejects_a_machine_label_source(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        resources.files("hotato").joinpath("schema", "contract.v1.json")
        .read_text(encoding="utf-8")
    )
    assert _create(tmp_path) == 0
    bad = _contract_json(tmp_path, "ct-created-001")
    bad["label"]["label_source"] = "model"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=bad, schema=schema)


# --- verify ------------------------------------------------------------------

def test_verify_a_passing_contract_exits_zero(tmp_path, capsys):
    assert _create(tmp_path) == 0
    rc = cli.main(["contract", "verify", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[PASS] ct-created-001" in out


def test_verify_single_bundle_dir_path(tmp_path):
    assert _create(tmp_path) == 0
    rc = cli.main(["contract", "verify", str(_bundle(tmp_path, "ct-created-001"))])
    assert rc == 0


def test_verify_json_shape(tmp_path, capsys):
    assert _create(tmp_path) == 0
    capsys.readouterr()  # drop the `contract create` text output
    rc = cli.main(["contract", "verify", str(tmp_path), "--format", "json"])
    assert rc == 0
    v = json.loads(capsys.readouterr().out)
    assert v["kind"] == "contract-verify"
    assert v["count"] == 1
    assert v["summary"] == {"passed": 1, "failed": 0}
    assert v["exit_code"] == 0
    assert v["results"][0]["id"] == "ct-created-001"


def test_verify_text_shows_stored_evidence_caveat(tmp_path, capsys):
    # contract verify re-scores the SAME bundled audio.wav every time; the
    # report says so outright so a green run is never mistaken for proof the
    # currently deployed agent is fine (that is the fresh-capture lane in
    # docs/RECAPTURE.md, not this one).
    assert _create(tmp_path) == 0
    capsys.readouterr()
    rc = cli.main(["contract", "verify", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert _contract._STORED_EVIDENCE_CAVEAT in out


def test_verify_html_shows_stored_evidence_caveat(tmp_path, capsys):
    assert _create(tmp_path) == 0
    html_path = tmp_path / "verify.html"
    rc = cli.main(["contract", "verify", str(tmp_path), "--html", str(html_path)])
    assert rc == 0
    html = html_path.read_text(encoding="utf-8")
    assert _contract._STORED_EVIDENCE_CAVEAT in html


def test_verify_writes_html_and_junit(tmp_path, capsys):
    assert _create(tmp_path) == 0
    html_path = tmp_path / "verify.html"
    junit_path = tmp_path / "verify-junit.xml"
    rc = cli.main([
        "contract", "verify", str(tmp_path),
        "--html", str(html_path), "--junit", str(junit_path),
    ])
    assert rc == 0
    assert "PASSED" in html_path.read_text()
    root = ET.fromstring(junit_path.read_text())
    assert root.attrib["failures"] == "0"


def test_verify_reports_a_regressed_contract_as_failed(tmp_path, capsys):
    # An impossible bound (0.0s) at creation time: the moment is still
    # SCORABLE (creation only refuses not-scorable input), but its OWN policy
    # never passes -- both at creation and on re-verify.
    assert _create(tmp_path, "--max-time-to-yield", "0.0",
                  cid="ct-regressed-001") == 0
    c = _contract_json(tmp_path, "ct-regressed-001")
    assert c["measurement"]["passed"] is False

    capsys.readouterr()  # drop the `contract create` text output
    rc = cli.main(["contract", "verify", str(tmp_path), "--format", "json"])
    assert rc == 1
    v = json.loads(capsys.readouterr().out)
    assert v["summary"] == {"passed": 0, "failed": 1}
    assert v["results"][0]["passed"] is False


def test_verify_batch_mixed_pass_and_fail(tmp_path, capsys):
    assert _create(tmp_path, cid="ct-good-001") == 0
    assert _create(tmp_path, "--max-time-to-yield", "0.0", cid="ct-bad-001") == 0
    capsys.readouterr()  # drop the two `contract create` text outputs
    rc = cli.main(["contract", "verify", str(tmp_path), "--format", "json"])
    assert rc == 1
    v = json.loads(capsys.readouterr().out)
    assert v["count"] == 2
    assert v["summary"] == {"passed": 1, "failed": 1}


def test_verify_empty_directory_is_a_usage_error(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert cli.main(["contract", "verify", str(empty)]) == 2


def test_verify_missing_directory_is_a_usage_error(tmp_path):
    assert cli.main(["contract", "verify", str(tmp_path / "nope")]) == 2


def test_verify_corrupt_contract_json_is_a_usage_error(tmp_path):
    assert _create(tmp_path) == 0
    (_bundle(tmp_path, "ct-created-001") / "contract.json").write_text("{not json")
    assert cli.main(["contract", "verify", str(tmp_path)]) == 2


@pytest.mark.parametrize("drop_path", [
    ("bundle", "paths", "audio"),
    ("source", "recording_type"),
    ("label", "expected_behavior"),
    ("policy", "pass_conditions"),
    ("event",),
])
def test_verify_contract_missing_required_field_is_a_usage_error(
        tmp_path, capsys, drop_path):
    """A contract.json that is valid JSON with the right schema string, but is
    missing a nested field _verify_one dereferences directly (bundle.paths.audio,
    source.recording_type, label.expected_behavior, policy.pass_conditions,
    event), must be refused as a clean usage error (exit 2) -- not an uncaught
    KeyError breaking verify_contracts's documented "never an exception"
    contract (docs/SUBMITTING.md invites third-party contract submissions)."""
    assert _create(tmp_path) == 0
    cpath = _bundle(tmp_path, "ct-created-001") / "contract.json"
    doc = json.loads(cpath.read_text(encoding="utf-8"))
    node = doc
    for key in drop_path[:-1]:
        node = node[key]
    del node[drop_path[-1]]
    cpath.write_text(json.dumps(doc), encoding="utf-8")
    capsys.readouterr()  # drop the `contract create` text output
    rc = cli.main(["contract", "verify", str(tmp_path), "--format", "json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert out["ok"] is False
    assert out["exit_code"] == 2
    assert ".".join(drop_path) in out["message"]


def test_verify_deeply_nested_contract_json_is_a_usage_error(tmp_path, capsys):
    """A pathologically deeply nested contract.json makes CPython's json
    decoder raise a bare RecursionError, not a json.JSONDecodeError.
    _load_contract must turn that into a clean usage error (exit 2), never
    let a RecursionError propagate as a raw traceback."""
    assert _create(tmp_path) == 0
    (_bundle(tmp_path, "ct-created-001") / "contract.json").write_text(
        "[" * 200000 + "]" * 200000
    )
    capsys.readouterr()  # drop the `contract create` text output
    rc = cli.main(["contract", "verify", str(tmp_path), "--format", "json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert out["ok"] is False
    assert out["exit_code"] == 2


# --- inspect -----------------------------------------------------------

def test_inspect_text(tmp_path, capsys):
    assert _create(tmp_path) == 0
    rc = cli.main(["contract", "inspect", str(_bundle(tmp_path, "ct-created-001"))])
    assert rc == 0
    out = capsys.readouterr().out
    assert "hotato contract: ct-created-001" in out
    assert "expect:    yield" in out


def test_inspect_json_matches_contract_file(tmp_path, capsys):
    assert _create(tmp_path) == 0
    capsys.readouterr()  # drop the `contract create` text output
    rc = cli.main([
        "contract", "inspect", str(_bundle(tmp_path, "ct-created-001")),
        "--format", "json",
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out == _contract_json(tmp_path, "ct-created-001")


def test_inspect_accepts_contract_json_path_directly(tmp_path):
    assert _create(tmp_path) == 0
    cpath = _bundle(tmp_path, "ct-created-001") / "contract.json"
    assert cli.main(["contract", "inspect", str(cpath)]) == 0


# --- card kind dispatch (reuses `hotato card`) ------------------------------

def test_card_renders_from_a_contract_json(tmp_path):
    assert _create(tmp_path) == 0
    out = tmp_path / "card.svg"
    rc = cli.main([
        "card", str(_bundle(tmp_path, "ct-created-001") / "contract.json"),
        "--out", str(out),
    ])
    assert rc == 0
    svg = out.read_text()
    assert svg.startswith("<svg")
    assert "ct-created-001" in svg
    assert "PASSED" in svg


# --- pack / unpack -----------------------------------------------------

def test_pack_then_unpack_round_trips_every_file(tmp_path):
    assert _create(tmp_path) == 0
    bundle = _bundle(tmp_path, "ct-created-001")
    archive = tmp_path / "ct-created-001.hotato.pack"
    rc = cli.main(["contract", "pack", str(bundle), "--out", str(archive)])
    assert rc == 0
    assert archive.is_file()

    dest = tmp_path / "unpacked" / "ct-created-001.hotato"
    rc = cli.main(["contract", "unpack", str(archive), "--out", str(dest)])
    assert rc == 0

    orig_files = sorted(str(p.relative_to(bundle)) for p in bundle.rglob("*") if p.is_file())
    dest_files = sorted(str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file())
    assert orig_files == dest_files
    for rel in orig_files:
        assert (bundle / rel).read_bytes() == (dest / rel).read_bytes()


def test_pack_is_deterministic(tmp_path):
    assert _create(tmp_path) == 0
    bundle = _bundle(tmp_path, "ct-created-001")
    a1 = tmp_path / "a1.hotato"
    a2 = tmp_path / "a2.hotato"
    assert cli.main(["contract", "pack", str(bundle), "--out", str(a1)]) == 0
    assert cli.main(["contract", "pack", str(bundle), "--out", str(a2)]) == 0
    assert a1.read_bytes() == a2.read_bytes()


def test_unpack_detects_a_corrupted_archive(tmp_path):
    assert _create(tmp_path) == 0
    archive = tmp_path / "ct-created-001.hotato.pack"
    assert cli.main(["contract", "pack", str(_bundle(tmp_path, "ct-created-001")),
                     "--out", str(archive)]) == 0
    data = bytearray(archive.read_bytes())
    mid = len(data) // 2
    for i in range(mid, mid + 64):
        data[i] ^= 0xFF
    corrupt = tmp_path / "corrupt.hotato"
    corrupt.write_bytes(bytes(data))

    dest = tmp_path / "corrupt-unpacked"
    rc = cli.main(["contract", "unpack", str(corrupt), "--out", str(dest)])
    assert rc == 2
    assert not dest.exists()


def test_pack_refuses_an_existing_out_without_force(tmp_path):
    assert _create(tmp_path) == 0
    archive = tmp_path / "ct-created-001.hotato.pack"
    assert cli.main(["contract", "pack", str(_bundle(tmp_path, "ct-created-001")),
                     "--out", str(archive)]) == 0
    assert cli.main(["contract", "pack", str(_bundle(tmp_path, "ct-created-001")),
                     "--out", str(archive)]) == 2
    assert cli.main(["contract", "pack", str(_bundle(tmp_path, "ct-created-001")),
                     "--out", str(archive), "--force"]) == 0


def test_unpack_refuses_an_existing_out_without_force(tmp_path):
    assert _create(tmp_path) == 0
    bundle = _bundle(tmp_path, "ct-created-001")
    archive = tmp_path / "ct-created-001.hotato.pack"
    assert cli.main(["contract", "pack", str(bundle), "--out", str(archive)]) == 0
    dest = tmp_path / "dest"
    assert cli.main(["contract", "unpack", str(archive), "--out", str(dest)]) == 0
    assert cli.main(["contract", "unpack", str(archive), "--out", str(dest)]) == 2
    assert cli.main(["contract", "unpack", str(archive), "--out", str(dest),
                     "--force"]) == 0


def test_pack_of_a_non_bundle_directory_is_refused(tmp_path):
    empty = tmp_path / "not-a-bundle"
    empty.mkdir()
    (empty / "hello.txt").write_text("hi")
    assert cli.main(["contract", "pack", str(empty)]) == 2


# --- unpack hardening against hostile archives ------------------------------
#
# A .hotato archive travels between teams (`contract pack` on one machine,
# `contract unpack` on another), so `unpack_contract` treats it as hostile
# input, not just a corruption check. Each fixture below hand-builds a
# minimal, otherwise well-formed archive (never through `contract pack`) so
# exactly one property is hostile. Every case must be refused (exit 2) with
# NOTHING written outside the archive itself: no --out directory, no stray
# extraction temp directory left behind.

def _hostile_zip(path, members, *, manifest_entries=None):
    """Write a raw archive with MANIFEST.sha256.json plus `members` (a list
    of dicts: name, data, and optionally external_attr / compress_type), for
    hand-crafting one hostile property at a time. `manifest_entries`
    defaults to the correct sha256 of every member under its own name;
    pass an explicit dict to build an archive whose manifest omits a member
    the archive actually carries."""
    if manifest_entries is None:
        manifest_entries = {
            m["name"]: hashlib.sha256(m["data"]).hexdigest() for m in members
        }
    manifest_bytes = (json.dumps(manifest_entries, indent=2, sort_keys=True)
                      + "\n").encode("utf-8")
    with zipfile.ZipFile(str(path), "w") as zf:
        zi = zipfile.ZipInfo(_contract.MANIFEST_NAME, date_time=(1980, 1, 1, 0, 0, 0))
        zf.writestr(zi, manifest_bytes)
        for m in members:
            zi = zipfile.ZipInfo(m["name"], date_time=(1980, 1, 1, 0, 0, 0))
            zi.external_attr = m.get("external_attr", 0o644 << 16)
            zf.writestr(zi, m["data"],
                       compress_type=m.get("compress_type", zipfile.ZIP_DEFLATED))


def _assert_nothing_written(dest):
    """After a refused unpack, --out must not exist and no stray extraction
    temp directory may be left behind in its parent."""
    assert not dest.exists()
    leftovers = [p.name for p in dest.parent.iterdir()
                if p.name.startswith(".hotato-unpack-tmp-")]
    assert leftovers == []


def _symlink_creation_supported():
    """Runtime probe, not a platform guess: some Windows accounts (no
    Developer Mode, no elevation) cannot create a symlink at all, others can.
    Try it for real in an isolated temp dir rather than assuming by OS."""
    with tempfile.TemporaryDirectory() as d:
        target = pathlib.Path(d) / "target.txt"
        target.write_text("x")
        link = pathlib.Path(d) / "link.txt"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            return False
    return True


requires_symlinks = pytest.mark.skipif(
    not _symlink_creation_supported(),
    reason="this host/account cannot create filesystem symlinks (no "
           "Developer Mode / elevation on Windows, or a restricted "
           "container); the test itself plants a real symlink to prove the "
           "pack step refuses it",
)


def test_unpack_refuses_a_path_traversal_member(tmp_path):
    archive = tmp_path / "hostile.hotato.pack"
    _hostile_zip(archive, [{"name": "../evil.txt", "data": b"pwned"}])
    dest = tmp_path / "unpacked"
    assert cli.main(["contract", "unpack", str(archive), "--out", str(dest)]) == 2
    _assert_nothing_written(dest)
    assert not (tmp_path.parent / "evil.txt").exists()


def test_unpack_refuses_a_backslash_traversal_member(tmp_path):
    archive = tmp_path / "hostile.hotato.pack"
    _hostile_zip(archive, [{"name": "..\\..\\evil.txt", "data": b"pwned"}])
    dest = tmp_path / "unpacked"
    assert cli.main(["contract", "unpack", str(archive), "--out", str(dest)]) == 2
    _assert_nothing_written(dest)


def test_unpack_refuses_a_drive_letter_member(tmp_path):
    archive = tmp_path / "hostile.hotato.pack"
    _hostile_zip(archive, [{"name": "C:evil.txt", "data": b"pwned"}])
    dest = tmp_path / "unpacked"
    assert cli.main(["contract", "unpack", str(archive), "--out", str(dest)]) == 2
    _assert_nothing_written(dest)


def test_unpack_refuses_a_symlink_member(tmp_path):
    archive = tmp_path / "hostile.hotato.pack"
    _hostile_zip(archive, [{
        "name": "evidence/trust.json", "data": b"/etc/passwd",
        "external_attr": 0o120777 << 16,
    }])
    dest = tmp_path / "unpacked"
    assert cli.main(["contract", "unpack", str(archive), "--out", str(dest)]) == 2
    _assert_nothing_written(dest)


def test_unpack_refuses_duplicate_member_names(tmp_path):
    archive = tmp_path / "hostile.hotato.pack"
    data = b"hello"
    manifest_bytes = (json.dumps({"contract.json": hashlib.sha256(data).hexdigest()},
                                 indent=2, sort_keys=True) + "\n").encode("utf-8")
    with zipfile.ZipFile(str(archive), "w") as zf:
        zi = zipfile.ZipInfo(_contract.MANIFEST_NAME, date_time=(1980, 1, 1, 0, 0, 0))
        zf.writestr(zi, manifest_bytes)
        for _ in range(2):
            zi = zipfile.ZipInfo("contract.json", date_time=(1980, 1, 1, 0, 0, 0))
            zf.writestr(zi, data)
    dest = tmp_path / "unpacked"
    assert cli.main(["contract", "unpack", str(archive), "--out", str(dest)]) == 2
    _assert_nothing_written(dest)


def test_unpack_refuses_a_member_not_declared_in_manifest(tmp_path):
    archive = tmp_path / "hostile.hotato.pack"
    declared = {"name": "contract.json", "data": b"{}"}
    undeclared = {"name": "evidence/trust.json", "data": b"sneaky"}
    _hostile_zip(
        archive, [declared, undeclared],
        manifest_entries={declared["name"]: hashlib.sha256(declared["data"]).hexdigest()},
    )
    dest = tmp_path / "unpacked"
    assert cli.main(["contract", "unpack", str(archive), "--out", str(dest)]) == 2
    _assert_nothing_written(dest)


def test_unpack_refuses_oversized_decompression(tmp_path):
    archive = tmp_path / "hostile.hotato.pack"
    _hostile_zip(archive, [{"name": "contract.json", "data": os.urandom(5000)}])
    dest = tmp_path / "unpacked"
    rc = cli.main(["contract", "unpack", str(archive), "--out", str(dest),
                  "--max-bytes", "1024"])
    assert rc == 2
    _assert_nothing_written(dest)


def test_unpack_refuses_a_compression_ratio_bomb(tmp_path):
    archive = tmp_path / "hostile.hotato.pack"
    _hostile_zip(archive, [{"name": "contract.json", "data": b"\x00" * 2_000_000}])
    dest = tmp_path / "unpacked"
    # Well under the 512 MiB default total-bytes cap: only the per-member
    # ratio check can catch this one.
    assert cli.main(["contract", "unpack", str(archive), "--out", str(dest)]) == 2
    _assert_nothing_written(dest)


def test_force_unpack_of_an_invalid_archive_preserves_the_existing_out_dir(tmp_path):
    # A9 regression (external diligence, 2026-07-10): --force must not delete
    # the destination before the archive has proven valid end to end.
    bad = tmp_path / "bad.hotato.pack"
    bad.write_bytes(b"not a zip")
    dest = tmp_path / "out"
    dest.mkdir()
    sentinel = dest / "sentinel.txt"
    sentinel.write_text("keep-me")
    assert cli.main(["contract", "unpack", str(bad), "--out", str(dest), "--force"]) == 2
    assert sentinel.read_text() == "keep-me"


def test_force_unpack_of_a_hostile_archive_preserves_the_existing_out_dir(tmp_path):
    archive = tmp_path / "hostile.hotato.pack"
    _hostile_zip(archive, [{"name": "../evil.txt", "data": b"pwned"}])
    dest = tmp_path / "out"
    dest.mkdir()
    sentinel = dest / "sentinel.txt"
    sentinel.write_text("keep-me")
    assert cli.main(["contract", "unpack", str(archive), "--out", str(dest), "--force"]) == 2
    assert sentinel.read_text() == "keep-me"


@requires_symlinks
def test_pack_refuses_a_file_symlink_in_the_bundle(tmp_path):
    # v2 diligence (2026-07-10): a planted symlink must never ship outside
    # bytes; the bundle must be self-contained.
    outside = tmp_path / "secret.txt"
    outside.write_text("OUTSIDE_SECRET")
    bundle = tmp_path / "b.hotato"
    (bundle / "audio").mkdir(parents=True)
    (bundle / "contract.json").write_text("{}")
    (bundle / "audio" / "evil.wav").symlink_to(outside)
    out = tmp_path / "b.hotato.pack"
    assert cli.main(["contract", "pack", str(bundle), "--out", str(out)]) == 2
    assert not out.exists()


@requires_symlinks
def test_pack_refuses_a_directory_symlink_in_the_bundle(tmp_path):
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "leak.txt").write_text("OUTSIDE_SECRET")
    bundle = tmp_path / "b.hotato"
    bundle.mkdir()
    (bundle / "contract.json").write_text("{}")
    (bundle / "evidence").symlink_to(outside_dir, target_is_directory=True)
    out = tmp_path / "b.hotato.pack"
    assert cli.main(["contract", "pack", str(bundle), "--out", str(out)]) == 2
    assert not out.exists()


def test_unpack_accepts_a_well_formed_hand_built_archive(tmp_path):
    """Regression guard: the hardening above must not reject a legitimate,
    nested-path archive built the same way the hostile fixtures are."""
    archive = tmp_path / "clean.hotato.pack"
    _hostile_zip(archive, [
        {"name": "contract.json", "data": b'{"ok": true}'},
        {"name": "evidence/trust.json", "data": b'{"trust": "ok"}'},
    ])
    dest = tmp_path / "unpacked"
    assert cli.main(["contract", "unpack", str(archive), "--out", str(dest)]) == 0
    assert (dest / "contract.json").read_bytes() == b'{"ok": true}'
    assert (dest / "evidence" / "trust.json").read_bytes() == b'{"trust": "ok"}'


# --- diarized-mono opt-in path (stub diarizer; no network, no extras) ------

def _timeline(segments, *, n_frames=600, hop=0.01):
    return [any(s <= k * hop < e for s, e in segments) for k in range(n_frames)]


@pytest.fixture
def stub_diarizer():
    saved_f = dict(_diarize._DIARIZER_FACTORIES)
    saved_c = dict(_diarize._DIARIZER_CACHE)

    def _register(name, timelines=None, **kw):
        _diarize.register_diarizer_backend(
            name, _diarize.build_stub_backend(timelines, **kw)
        )

    try:
        yield _register
    finally:
        _diarize._DIARIZER_FACTORIES.clear()
        _diarize._DIARIZER_FACTORIES.update(saved_f)
        _diarize._DIARIZER_CACHE.clear()
        _diarize._DIARIZER_CACHE.update(saved_c)


def test_mono_without_diarize_flag_is_a_usage_error(tmp_path):
    mono = _write_mono(tmp_path / "mono.wav", segments=[(0.2, 5.8)])
    rc = cli.main([
        "contract", "create", "--mono", mono, "--id", "ct-mono-nodiarize",
        "--expect", "yield", "--out", str(tmp_path),
    ])
    assert rc == 2



# Caller onset at 1.8s while the agent (still active since 0.3s) is talking --
# a real barge-in shape, not sequential turns -- so the moment is SCORABLE for
# a should-yield label: agent_talking_at_onset requires temporal overlap. The
# agent stops at 2.0s (a 0.2s yield), well under the diarizer's overlap-ratio
# high-tier bound.
_DIARIZE_AGENT_SEGMENTS = [(0.3, 2.0)]
_DIARIZE_CALLER_SEGMENTS = [(1.8, 2.6)]
_DIARIZE_ONSET = "1.8"


def test_diarized_mono_high_tier_creates_an_indicative_free_contract(
    tmp_path, stub_diarizer,
):
    mono = _write_mono(tmp_path / "mono.wav",
                       segments=_DIARIZE_AGENT_SEGMENTS + _DIARIZE_CALLER_SEGMENTS)
    stub_diarizer("pyannote", {
        _diarize.SPEAKER_A: _timeline(_DIARIZE_CALLER_SEGMENTS),
        _diarize.SPEAKER_B: _timeline(_DIARIZE_AGENT_SEGMENTS),
    }, posterior=0.9, embedding_margin=0.6)
    rc = cli.main([
        "contract", "create", "--mono", mono, "--diarize",
        "--onset", _DIARIZE_ONSET,
        "--caller-speaker", _diarize.SPEAKER_A, "--agent-speaker", _diarize.SPEAKER_B,
        "--id", "ct-diarized-001", "--expect", "yield", "--out", str(tmp_path),
    ])
    assert rc == 0
    c = _contract_json(tmp_path, "ct-diarized-001")
    assert c["source"]["recording_type"] == "diarized-mono"
    assert c["source"]["channels"] == 1
    assert c["measurement"]["diarization"]["confidence_tier"] == "high"
    assert c["measurement"]["indicative_only"] is False
    assert c["measurement"]["scorable"] is True

    b = _bundle(tmp_path, "ct-diarized-001")
    assert (b / "audio" / "event.wav").exists()
    meta = json.loads((b / "evidence" / "frames.jsonl").read_text().splitlines()[0])
    assert meta["available"] is False


def test_diarized_mono_low_tier_is_never_silently_upgraded(tmp_path, stub_diarizer):
    # SAME scorable shape as the high-tier test, but a below-bar segmentation
    # posterior (between POSTERIOR_REFUSE and POSTERIOR_HIGH) pins the tier to
    # "low" -- indicative_only must be true, never silently upgraded.
    mono = _write_mono(tmp_path / "mono.wav",
                       segments=_DIARIZE_AGENT_SEGMENTS + _DIARIZE_CALLER_SEGMENTS)
    stub_diarizer("pyannote", {
        _diarize.SPEAKER_A: _timeline(_DIARIZE_CALLER_SEGMENTS),
        _diarize.SPEAKER_B: _timeline(_DIARIZE_AGENT_SEGMENTS),
    }, posterior=0.55, embedding_margin=0.6)
    rc = cli.main([
        "contract", "create", "--mono", mono, "--diarize",
        "--onset", _DIARIZE_ONSET,
        "--id", "ct-diarized-low", "--expect", "yield", "--out", str(tmp_path),
    ])
    assert rc == 0
    c = _contract_json(tmp_path, "ct-diarized-low")
    assert c["measurement"]["diarization"]["confidence_tier"] == "low"
    assert c["measurement"]["indicative_only"] is True


def test_diarized_mono_refuse_tier_is_refused_with_reason(tmp_path, stub_diarizer):
    mono = _write_mono(tmp_path / "mono.wav", segments=[(0.5, 5.5)])
    # Only one speaker detected -> not two clean parties -> refuse.
    stub_diarizer("pyannote", {_diarize.SPEAKER_A: _timeline([(0.5, 5.5)])})
    rc = cli.main([
        "contract", "create", "--mono", mono, "--diarize",
        "--id", "ct-diarized-refuse", "--expect", "yield", "--out", str(tmp_path),
    ])
    assert rc == 2
    assert not _bundle(tmp_path, "ct-diarized-refuse").exists()


def test_verify_re_scores_a_diarized_mono_contract(tmp_path, stub_diarizer):
    mono = _write_mono(tmp_path / "mono.wav",
                       segments=_DIARIZE_AGENT_SEGMENTS + _DIARIZE_CALLER_SEGMENTS)
    stub_diarizer("pyannote", {
        _diarize.SPEAKER_A: _timeline(_DIARIZE_CALLER_SEGMENTS),
        _diarize.SPEAKER_B: _timeline(_DIARIZE_AGENT_SEGMENTS),
    }, posterior=0.9, embedding_margin=0.6)
    assert cli.main([
        "contract", "create", "--mono", mono, "--diarize",
        "--onset", _DIARIZE_ONSET,
        "--caller-speaker", _diarize.SPEAKER_A, "--agent-speaker", _diarize.SPEAKER_B,
        "--id", "ct-diarized-verify", "--expect", "yield", "--out", str(tmp_path),
    ]) == 0
    # verify re-diarizes with the SAME stub backend still registered: must
    # re-score cleanly and reproduce the same pass/fail this exact audio +
    # policy already gave at creation time (nothing else changed).
    rc = cli.main(["contract", "verify", str(tmp_path)])
    assert rc == 0
