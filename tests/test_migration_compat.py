"""Backward compatibility: 0.9-era artifacts still load under 0.10.

The 0.10 kernel adds fields (evidence blocks, contract attestation digests,
boundary measurements) additively. A contract or envelope produced before those
fields existed must still load, verify, and score -- never a hard break."""
import json
import os

from hotato import contract as _contract, core, verify as _verify
from tests import _trial_audio as ta


def _make_contract(tmp_path):
    wav = str(tmp_path / "c.wav"); ta.talkover_call(wav)
    outdir = str(tmp_path / "contracts")
    _contract.create_contract(stereo=wav, expect="yield", out_dir=outdir,
                              onset_sec=2.0, contract_id="c-001",
                              max_time_to_yield_sec=1.0, max_talk_over_sec=1.0)
    for root, _dirs, files in os.walk(outdir):
        if "contract.json" in files:
            return outdir, os.path.join(root, "contract.json")
    raise AssertionError("no contract.json")


def test_pre_010_contract_without_attestation_still_verifies(tmp_path):
    """Simulate a 0.9 bundle: strip the 0.10-added attestation block. It must
    load and verify as 'unattested' (legacy), not error."""
    outdir, cj = _make_contract(tmp_path)
    doc = json.load(open(cj))
    doc.pop("attestation", None)      # 0.9 contracts had no embedded digest
    doc.pop("scorer", None)
    json.dump(doc, open(cj, "w"), indent=2)
    v = _contract.verify_contracts(outdir)
    r = v["results"][0]
    assert r.get("authenticity") in ("unattested", "unsigned")   # never a crash
    assert "passed" in r                                          # still scored


def test_pre_boundary_envelope_still_scores_and_verifies(tmp_path):
    """An envelope from before the additive boundary/measurement fields (a plain
    verdict + measurements) still pairs and verifies."""
    # build a legacy-shaped envelope pair (no boundary keys in measurements)
    def legacy(passed):
        return {"tool": "hotato", "schema_version": "1", "mode": "suite", "stack": "x",
                "offline": True, "engine": {"name": "h", "version": "0.9.0", "upstream": "u"},
                "limits": {"method": "m", "accuracy_claim": None, "ceiling": "c",
                           "best_input": "b", "does_not_do": [], "scope": "s", "offline": True},
                "summary": {"events": 1, "passed": int(passed), "failed": int(not passed),
                            "regression": False},
                "events": [{"event_id": "f1", "expected_yield": True,
                            "verdict": {"passed": passed, "did_yield": passed,
                                        "seconds_to_yield": 0.3 if passed else None,
                                        "talk_over_sec": 0.3, "reasons": []},
                            "measurements": {"caller_onset_sec": 2.0, "hop_sec": 0.01}}],
                "fix_map": [], "funnel": None, "exit_code": 0 if passed else 1}
    b = tmp_path / "b.json"; a = tmp_path / "a.json"
    json.dump(legacy(False), open(b, "w")); json.dump(legacy(True), open(a, "w"))
    v = _verify.verify_sides(str(b), str(a), min_n=1)
    # it verifies (envelope-only), and carries the new evidence block at ASSERTED
    assert "evidence" in v
    assert v["evidence"]["tier"] <= 1     # envelope-only legacy input is asserted, not paired
