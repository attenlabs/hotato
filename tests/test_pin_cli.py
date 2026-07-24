"""``hotato pin <autopsy-ref>``: the graduation bridge from autopsy to CI.

Pinned here:

  * the persisted autopsy envelope (``autopsy-<id>.json`` next to the HTML)
    is content-addressed and deterministic: byte-identical across runs,
    carries the source path and the incidents with onset/kind/scan_kind,
    and never a cost figure (est. cost is a rendering layer, not a fact);
  * the round trip on a rendered example: autopsy -> pin -> the minted
    contract PASSES ``hotato contract verify`` through the existing verify
    logic (no new minting logic anywhere -- pin delegates to
    ``contract.create_contract``);
  * the kind -> expect mapping (BARGE-IN defaults to yield) and the
    ``--expect hold`` human override;
  * EVERY refusal exits 2 with the reason and leaves no artifact: a
    malformed ref, an unknown id, a rank out of range (and rank 0), a
    missing source recording, a changed source recording (the bytes no
    longer hash to the pinned id), a mono-derived incident (contracts
    require the two-channel deterministic path), a bare apx- ref on a call
    with no critical incidents, and the incident kinds that carry no
    yield/hold decision (DEAD AIR / LATENCY SPIKE / ECHO SUSPECTED);
  * ``--from DIR`` resolves envelopes from a non-default store.
"""

import json
import math
import os
import random
import re
import shutil
import struct
import wave
from importlib import resources

from hotato import cli

EXAMPLES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples", "autopsy", "audio",
)


def _example(name: str) -> str:
    return os.path.join(EXAMPLES, name + ".example.wav")


def _bundled(sid: str) -> str:
    return str(
        resources.files("hotato").joinpath("data", "audio", sid + ".example.wav")
    )


def _write_mono_wav(path, active_spans, duration_sec=14.0, sr=16000):
    """A deterministic one-channel WAV (the same shape test_autopsy uses):
    a 220 Hz tone plus seeded noise inside each active span."""
    rng = random.Random(7)
    n = int(duration_sec * sr)
    active = [False] * n
    for a, b in active_spans:
        for i in range(int(a * sr), min(n, int(b * sr))):
            active[i] = True
    frames = []
    for i in range(n):
        if active[i]:
            v = 0.4 * math.sin(2 * math.pi * 220 * i / sr) + 0.1 * rng.uniform(-1, 1)
        else:
            v = 0.0005 * rng.uniform(-1, 1)
        frames.append(int(max(-1.0, min(1.0, v)) * 32767))
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(struct.pack("<%dh" % len(frames), *frames))


def _autopsy(path, capsys):
    """Run autopsy on ``path`` in the current cwd and return the apx id."""
    assert cli.main(["autopsy", str(path)]) == 0
    out = capsys.readouterr().out
    return re.search(r"pin: (apx-[0-9a-f]{12})", out).group(1)


def _no_bundles(out_dir="contracts"):
    """A refused pin leaves no artifact: no bundle dirs under ``out_dir``."""
    if not os.path.isdir(out_dir):
        return True
    return not [n for n in os.listdir(out_dir) if n.endswith(".hotato")]


# --- the persisted envelope ---------------------------------------------------

def test_autopsy_writes_a_deterministic_envelope(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "call.wav"
    shutil.copy(_example("autopsy-01-barge-in-say-do"), src)
    apx = _autopsy(src, capsys)
    env_path = os.path.join("hotato-output", f"autopsy-{apx}.json")
    assert os.path.isfile(env_path)
    env1 = open(env_path, "rb").read()
    apx2 = _autopsy(src, capsys)
    assert apx2 == apx
    assert open(env_path, "rb").read() == env1  # byte-identical across runs
    env = json.loads(env1.decode("utf-8"))
    assert env["kind"] == "autopsy"
    assert env["id"] == apx
    assert env["source_path"] == str(src)
    assert env["mode"] == "stereo"
    for inc in env["incidents"]:
        assert inc["scan_kind"]
        assert isinstance(inc["t_sec"], float)


def test_envelope_carries_no_cost_figures_even_under_cost_config(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "costs.json"
    cfg.write_text(json.dumps(
        {"currency": "USD", "per_incident": {"barge-in": 2.0}}))
    src = tmp_path / "call.wav"
    shutil.copy(_example("autopsy-01-barge-in-say-do"), src)
    assert cli.main(["autopsy", str(src), "--cost-config", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "est. cost" in out  # the rendered surface prices it
    apx = re.search(r"pin: (apx-[0-9a-f]{12})", out).group(1)
    env_text = open(os.path.join("hotato-output", f"autopsy-{apx}.json"),
                    encoding="utf-8").read()
    assert '"est_cost"' not in env_text
    assert "$" not in env_text


# --- the round trip: autopsy -> pin -> contract verify PASSES ------------------

def test_pin_round_trip_contract_verify_passes(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "yielding-call.wav"
    # the bundled yielding call: a BARGE-IN warning where the agent yielded
    # promptly, so the pinned yield expectation holds under re-scoring
    shutil.copy(_bundled("01-hard-interruption"), src)
    apx = _autopsy(src, capsys)
    assert cli.main(["pin", f"{apx}#1"]) == 0
    out = capsys.readouterr().out
    assert f"hotato pin: {apx}#1 -> BARGE-IN" in out
    assert "expect: yield (from the incident kind" in out
    assert "created hotato contract:" in out
    m = re.search(r"dir:\s+(\S+\.hotato)", out)
    assert m and os.path.isdir(m.group(1))
    assert "hotato prove --contracts contracts" in out
    # the existing verify logic passes the minted bundle
    assert cli.main(["contract", "verify", "contracts"]) == 0
    verify_out = capsys.readouterr().out
    assert "[PASS]" in verify_out


def test_bare_ref_pins_the_top_critical_incident(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "barge.wav"
    shutil.copy(_example("autopsy-01-barge-in-say-do"), src)
    apx = _autopsy(src, capsys)
    assert cli.main(["pin", apx, "--format", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["pin"]["ref"] == f"{apx}#1"
    assert result["pin"]["incident_kind"] == "BARGE-IN"
    assert result["pin"]["expect"] == "yield"
    assert result["pin"]["expect_source"] == "incident kind"
    assert result["prove"] == "hotato prove --contracts contracts"
    # the bundle exists and carries the candidate-kind provenance (the ref
    # itself is an identifier, redacted unless --include-identifiers)
    cjson = json.loads(open(
        os.path.join(result["dir"], "contract.json"),
        encoding="utf-8").read())
    assert cjson["source"]["candidate_kind"] == "overlap_while_agent_talking"


def test_expect_hold_override_is_the_humans_call(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "call.wav"
    shutil.copy(_bundled("01-hard-interruption"), src)
    apx = _autopsy(src, capsys)
    assert cli.main(["pin", f"{apx}#1", "--expect", "hold",
                     "--id", "mhm-was-fine-001", "--format", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["pin"]["expect"] == "hold"
    assert result["pin"]["expect_source"] == "--expect"
    assert result["id"] == "mhm-was-fine-001"


def test_pin_resolves_from_a_custom_store_with_from(tmp_path, monkeypatch,
                                                    capsys):
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "call.wav"
    shutil.copy(_bundled("01-hard-interruption"), src)
    apx = _autopsy(src, capsys)
    os.rename("hotato-output", "elsewhere")
    assert cli.main(["pin", f"{apx}#1", "--from", "elsewhere"]) == 0
    capsys.readouterr()


# --- refusals: exit 2, the reason, no artifact ---------------------------------

def test_malformed_ref_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["pin", "not-a-ref"]) == 2
    assert "is not an autopsy ref" in capsys.readouterr().err
    assert cli.main(["pin", "apx-cc33f46fad58#0"]) == 2
    assert "ranks start at 1" in capsys.readouterr().err
    assert _no_bundles()


def test_unknown_id_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["pin", "apx-000000000000"]) == 2
    err = capsys.readouterr().err
    assert "unknown autopsy id apx-000000000000" in err
    assert "hotato autopsy" in err
    assert _no_bundles()


def test_rank_out_of_range_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "call.wav"
    shutil.copy(_example("autopsy-03-talk-over"), src)
    apx = _autopsy(src, capsys)
    assert cli.main(["pin", f"{apx}#9"]) == 2
    err = capsys.readouterr().err
    assert "out of range" in err
    assert "numbered 1..1" in err
    assert _no_bundles()


def test_missing_source_file_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "call.wav"
    shutil.copy(_example("autopsy-03-talk-over"), src)
    apx = _autopsy(src, capsys)
    os.remove(src)
    assert cli.main(["pin", f"{apx}#1"]) == 2
    assert "is not a file on this machine" in capsys.readouterr().err
    assert _no_bundles()


def test_changed_source_file_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "call.wav"
    shutil.copy(_example("autopsy-03-talk-over"), src)
    apx = _autopsy(src, capsys)
    with open(src, "ab") as fh:
        fh.write(b"\x00")  # the file on disk is no longer the analyzed bytes
    assert cli.main(["pin", f"{apx}#1"]) == 2
    err = capsys.readouterr().err
    assert "changed since the autopsy" in err
    assert apx in err
    assert _no_bundles()


def test_mono_derived_incident_refuses_to_pin(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    mono = tmp_path / "mono.wav"
    _write_mono_wav(str(mono), [(0.5, 2.0), (8.5, 10.0)])
    apx = _autopsy(mono, capsys)
    assert cli.main(["pin", apx]) == 2
    err = capsys.readouterr().err
    assert "mono" in err
    assert "two-channel deterministic path" in err
    assert _no_bundles()


def test_dead_air_incident_has_no_decision_to_pin(tmp_path, monkeypatch,
                                                  capsys):
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "call.wav"
    shutil.copy(_example("autopsy-02-latency-dead-air"), src)
    apx = _autopsy(src, capsys)
    assert cli.main(["pin", apx]) == 2
    err = capsys.readouterr().err
    assert "DEAD AIR" in err
    assert "no yield/hold decision" in err
    assert "BARGE-IN and TALK-OVER" in err
    assert _no_bundles()


def test_bare_ref_with_no_critical_incidents_is_refused(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "call.wav"
    # the yielding call: one WARNING barge-in, zero criticals
    shutil.copy(_bundled("01-hard-interruption"), src)
    apx = _autopsy(src, capsys)
    assert cli.main(["pin", apx]) == 2
    err = capsys.readouterr().err
    assert "no critical incidents" in err
    assert f"{apx}#1..#1" in err
    assert _no_bundles()


def test_talk_over_pins_and_stays_red_until_fixed(tmp_path, monkeypatch,
                                                  capsys):
    # A critical TALK-OVER pins (exit 0); the minted contract records the
    # measured failure, so it is the red gate the graduation path promises.
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "call.wav"
    shutil.copy(_example("autopsy-03-talk-over"), src)
    apx = _autopsy(src, capsys)
    assert cli.main(["pin", apx, "--format", "json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["pin"]["incident_kind"] == "TALK-OVER"
    assert result["contract"]["measurement"]["passed"] is False
    assert cli.main(["contract", "verify", "contracts"]) == 1
    capsys.readouterr()
