"""Recompute-from-audio engine for the proof gate.

`hotato fix trial` used to trust the ``verdict.passed`` stored in the before /
after envelopes. That let a hand-edited verdict, an old call re-encoded under a
looser policy, or unrelated audio reused under the original IDs reach an
``improved`` verdict without the scorer ever agreeing.

This module re-derives every verdict from the on-disk audio under ONE pinned
trial manifest (policy + scorer + onset + expectation), and classifies the
result into an evidence vector. It never reads a stored ``passed`` to decide a
verdict; it reads it only to DETECT tampering (stored != recomputed).

Zero-dependency: it drives the same ``core.run_single`` audio scorer the CLI
uses.
"""
from __future__ import annotations

import hashlib
import os
import wave
from typing import Optional

from . import core as _core
from . import evidence as _evidence
from . import manifest as _manifest
from ._engine.score import ScoreConfig


def _resolve_base(path: str) -> str:
    """Directory that holds a run's audio, given the --before/--after arg
    (a run.json file or a directory)."""
    if os.path.isdir(path):
        return path
    return os.path.dirname(os.path.abspath(path))


def _channel_pcm_sha256(path: str, channel: int) -> Optional[str]:
    """sha256 over ONE channel's decoded PCM samples of a WAV. The scripted
    caller stimulus lives on the caller channel; hashing it (not the whole
    stereo file) lets a legit agent-side fix -- which changes only the AGENT
    channel -- still match its before-side stimulus, while unrelated caller
    audio does not."""
    try:
        with wave.open(path, "rb") as wf:
            nch = wf.getnchannels()
            width = wf.getsampwidth()
            if channel >= nch:
                channel = 0
            h = hashlib.sha256()
            frame_bytes = width * nch
            step = 1 << 16
            while True:
                chunk = wf.readframes(step)
                if not chunk:
                    break
                # extract this channel's sample bytes from each interleaved frame
                for off in range(0, len(chunk), frame_bytes):
                    a = off + channel * width
                    h.update(chunk[a:a + width])
            return h.hexdigest()
    except Exception:
        return None


def _caller_pcm(base_dir: str, event: dict, caller_channel: int = 0) -> Optional[str]:
    """Caller-side (scripted-stimulus) PCM hash for one fixture's audio on disk.
    Stereo -> the caller channel; dual-mono -> the caller file's PCM."""
    prov = event.get("audio_provenance") or {}
    sides = prov.get("sides") or []
    if not sides:
        return None
    roles = [s.get("role") for s in sides]
    if roles == ["stereo"]:
        fp = os.path.join(base_dir, sides[0].get("path", ""))
        return _channel_pcm_sha256(fp, caller_channel) if os.path.isfile(fp) else None
    if set(roles) == {"caller", "agent"}:
        for s in sides:
            if s.get("role") == "caller":
                return s.get("pcm_sha256")
    return None


def _event_index(env: dict) -> dict:
    out = {}
    for ev in env.get("events", []):
        out[_manifest.fixture_key(ev)] = ev
    return out


def _rescore_event(event: dict, base_dir: str, *, fixture: dict,
                   policy: dict, cfg: ScoreConfig) -> dict:
    """Re-score one fixture's audio from disk under the pinned policy/onset/
    expectation. Returns {status, recomputed_passed, stored_passed, ...}.

    status:
      recomputed  - audio found, re-scored; recomputed_passed is authoritative
      missing     - no audio provenance / file to re-score (cannot recompute)
      unverifiable- audio present but mode unsupported for recompute (e.g. mono)
      error       - re-score raised
    """
    prov = event.get("audio_provenance") or {}
    sides = prov.get("sides") or []
    stored_passed = bool((event.get("verdict") or {}).get("passed"))
    onset = fixture.get("onset_sec")
    expect = "yield" if fixture.get("expected_yield", True) else "hold"
    max_over = policy.get("max_talk_over_sec")
    max_ttoy = policy.get("max_time_to_yield_sec")

    if not sides:
        return {"status": "missing", "stored_passed": stored_passed,
                "recomputed_passed": None}

    def side_path(role):
        for s in sides:
            if s.get("role") == role:
                return os.path.join(base_dir, s.get("path", ""))
        return None

    roles = [s.get("role") for s in sides]
    kwargs = dict(onset_sec=onset, expect=expect, max_talk_over_sec=max_over,
                  max_time_to_yield_sec=max_ttoy, cfg=cfg)
    try:
        if roles == ["stereo"]:
            fp = os.path.join(base_dir, sides[0].get("path", ""))
            if not os.path.isfile(fp):
                return {"status": "missing", "stored_passed": stored_passed,
                        "recomputed_passed": None, "path": fp}
            env = _core.run_single(stereo=fp, **kwargs)
        elif set(roles) == {"caller", "agent"}:
            cpath, apath = side_path("caller"), side_path("agent")
            if not (cpath and apath and os.path.isfile(cpath) and os.path.isfile(apath)):
                return {"status": "missing", "stored_passed": stored_passed,
                        "recomputed_passed": None}
            env = _core.run_single(caller=cpath, agent=apath, **kwargs)
        else:
            # mono / diarized / unknown layout: cannot recompute a clean paired
            # verdict from audio here.
            return {"status": "unverifiable", "stored_passed": stored_passed,
                    "recomputed_passed": None}
    except Exception as exc:  # noqa: BLE001 - any decode/score failure is data, not a crash
        return {"status": "error", "stored_passed": stored_passed,
                "recomputed_passed": None, "error": str(exc)}

    ev = env["events"][0]
    verdict = ev.get("verdict") or {}
    recomputed_passed = bool(verdict.get("passed"))
    # audio IDENTITY comes from the FRESH provenance run_single decoded off disk,
    # never the stored envelope hash: same_pcm and audio_identity must reflect
    # what is on disk now. A stored pcm_sha256 that disagrees with the freshly
    # decoded one means the envelope's provenance does not match its audio.
    fresh_sides = ((ev.get("audio_provenance") or {}).get("sides")) or []
    fresh_pcm = _side_pcm(fresh_sides)
    stored_pcm = _side_pcm(sides)
    pcm_mismatch = bool(stored_pcm and fresh_pcm and stored_pcm != fresh_pcm)
    return {
        "status": "recomputed",
        "stored_passed": stored_passed,
        "recomputed_passed": recomputed_passed,
        "verdict": verdict,
        "measurements": ev.get("measurements"),
        "scorable": ev.get("scorable", True) is not False,
        "not_scorable_reason": ev.get("not_scorable_reason"),
        "pcm_sha256": fresh_pcm,          # authoritative: decoded off disk now
        "stored_pcm_sha256": stored_pcm,
        "pcm_mismatch": pcm_mismatch,
    }


def _side_pcm(sides) -> Optional[str]:
    if len(sides) == 1:
        return sides[0].get("pcm_sha256")
    for s in sides:
        if s.get("role") == "agent":
            return s.get("pcm_sha256")
    return sides[0].get("pcm_sha256") if sides else None


def _rebuilt_envelope(env: dict, per_fixture: dict) -> dict:
    """A copy of ``env`` with each event's verdict.passed REPLACED by the
    recomputed value, and summary recounted, so downstream verify_sides operates
    on trustworthy pass/fail. Events that could not be recomputed keep their
    stored verdict but are flagged (the trial refuses/inconclusive on those)."""
    import copy
    out = copy.deepcopy(env)
    passed = failed = 0
    for ev in out.get("events", []):
        if ev.get("scorable") is False:
            continue
        key = _manifest.fixture_key(ev)
        rec = per_fixture.get(key)
        if rec and rec.get("status") == "recomputed" and rec.get("recomputed_passed") is not None:
            ev.setdefault("verdict", {})["passed"] = rec["recomputed_passed"]
            if rec.get("verdict"):
                ev["verdict"]["did_yield"] = rec["verdict"].get("did_yield")
                ev["verdict"]["seconds_to_yield"] = rec["verdict"].get("seconds_to_yield")
                ev["verdict"]["talk_over_sec"] = rec["verdict"].get("talk_over_sec")
            ev["score_integrity"] = "recomputed"
        if (ev.get("verdict") or {}).get("passed"):
            passed += 1
        else:
            failed += 1
    summary = out.setdefault("summary", {})
    summary["passed"] = passed
    summary["failed"] = failed
    summary["events"] = passed + failed
    summary["regression"] = failed > 0
    return summary and out or out


def recompute_trial(
    before_env: dict, before_arg: str,
    after_env: dict, after_arg: str,
    man: dict,
    *,
    cfg: Optional[ScoreConfig] = None,
    capture_receipts: Optional[dict] = None,
    capture_context: str = "operator",
    caller_channel: int = 0,
) -> dict:
    """Recompute both sides under the manifest and classify the evidence.

    Returns a dict with:
      before_rebuilt / after_rebuilt : envelopes with recomputed verdicts
      per_fixture                    : {before, after} recompute records
      coverage                       : manifest coverage on each side
      evidence                       : the evidence classification block
      refusal                        : {kind, reason} or None (hard-gate failure)
    """
    cfg = cfg or ScoreConfig()
    policy = _manifest.normalize_policy(man.get("policy"))
    fidx = _manifest.fixture_index(man)
    before_base = _resolve_base(before_arg)
    after_base = _resolve_base(after_arg)
    b_events = _event_index(before_env)
    a_events = _event_index(after_env)

    before_cov = _manifest.coverage(man, before_env)
    after_cov = _manifest.coverage(man, after_env)

    per_before, per_after = {}, {}
    score_mismatch = False
    same_pcm_any = False
    stimulus_mismatch = False
    unrecomputable = False
    pcm_mismatch_any = False
    label_authorities = []

    for key, fixture in fidx.items():
        bev = b_events.get(key)
        aev = a_events.get(key)
        if bev is not None:
            rb = _rescore_event(bev, before_base, fixture=fixture, policy=policy, cfg=cfg)
            per_before[key] = rb
            if rb["status"] == "recomputed" and rb["stored_passed"] != rb["recomputed_passed"]:
                score_mismatch = True
            if rb["status"] in ("missing", "unverifiable", "error"):
                unrecomputable = True
        if aev is not None:
            ra = _rescore_event(aev, after_base, fixture=fixture, policy=policy, cfg=cfg)
            per_after[key] = ra
            if ra["status"] == "recomputed" and ra["stored_passed"] != ra["recomputed_passed"]:
                score_mismatch = True
            if ra["status"] in ("missing", "unverifiable", "error"):
                unrecomputable = True
        # a stored provenance hash that disagrees with the freshly decoded one
        if (per_before.get(key) or {}).get("pcm_mismatch") or \
           (per_after.get(key) or {}).get("pcm_mismatch"):
            pcm_mismatch_any = True
        # per-fixture label authority pinned by the manifest (M1: never assumed)
        label_authorities.append(fixture.get("label_authority", "none"))
        # same decoded PCM across sides (a re-scored old call, not a fresh one)
        if bev is not None and aev is not None:
            b_pcm = (per_before.get(key) or {}).get("pcm_sha256")
            a_pcm = (per_after.get(key) or {}).get("pcm_sha256")
            if b_pcm and a_pcm and b_pcm == a_pcm:
                same_pcm_any = True
        # scripted-stimulus binding (caller channel only): a legit fix changes
        # the AGENT side; the caller's scripted interruption is replayed. If the
        # after-side caller stimulus differs from the before-side one, the pair
        # is not the same scenario recaptured (unrelated audio / different
        # stimulus) and machine-verified pairing cannot be certified.
        if bev is not None and aev is not None:
            b_stim = _caller_pcm(before_base, bev, caller_channel)
            a_stim = _caller_pcm(after_base, aev, caller_channel)
            if b_stim and a_stim and b_stim != a_stim:
                stimulus_mismatch = True

    before_rebuilt = _rebuilt_envelope(before_env, per_before)
    after_rebuilt = _rebuilt_envelope(after_env, per_after)

    # --- evidence vector ----------------------------------------------------
    vector = {}
    # score integrity
    if score_mismatch:
        vector["score_integrity"] = "mismatch"
    elif unrecomputable:
        vector["score_integrity"] = "envelope_only"
    else:
        vector["score_integrity"] = "recomputed"
    # audio identity across the pair (decoded off disk, not stored)
    if pcm_mismatch_any:
        vector["audio_identity"] = "mismatch"
    elif same_pcm_any:
        vector["audio_identity"] = "same_pcm"
    elif unrecomputable:
        vector["audio_identity"] = "missing"
    else:
        vector["audio_identity"] = "recomputed"
    # policy: pinned by the manifest and applied to both sides
    vector["policy_integrity"] = "manifest_pinned" if _manifest.verify_manifest_hash(man) else "changed"
    # fixture universe completeness (both sides must cover the pinned set)
    if before_cov["complete"] and after_cov["complete"]:
        vector["fixture_set_integrity"] = "manifest_complete"
    else:
        vector["fixture_set_integrity"] = "subset"
    # pairing / capture origin
    receipts = capture_receipts or {}
    if receipts:
        # a signed/attested capture runner vouches origin for every fixture
        vector["pairing_integrity"] = "contract_bound"
        vector["capture_origin"] = "runner_attested"
    elif not stimulus_mismatch:
        # the after side replayed the same scripted caller stimulus: the pair is
        # the same scenario recaptured. Origin is whoever ran the trial.
        vector["pairing_integrity"] = "contract_bound"
        vector["capture_origin"] = (
            "operator_asserted" if capture_context == "operator" else "unknown"
        )
    else:
        # caller stimulus differs and no receipt: cannot certify the pair.
        vector["pairing_integrity"] = "id_only"
        vector["capture_origin"] = "unknown"
    # input health / mapping / label default to their honest unknowns unless a
    # caller supplies better (fix_trial fills these from trust + labels).
    # trust-derived dimensions are left for the caller (fix_trial runs a trust
    # preflight and fills these); recompute floors channel_mapping at "inferred"
    # for a pair it successfully re-scored from stereo/dual audio, so a
    # recompute-only tier reads MEASURED, and trust can lift it to confirmed.
    vector.setdefault("input_health", None)
    vector.setdefault("channel_mapping", "inferred" if not unrecomputable else None)
    # label authority is the WEAKEST across fixtures, read from the manifest's
    # pinned per-fixture label metadata -- never assumed. A fixture whose
    # expectation was not an explicit human label caps the proof below PAIRED.
    _lorder = {"none": 0, "suggested": 1, "human": 2}
    if label_authorities:
        vector["label_authority"] = min(
            label_authorities, key=lambda a: _lorder.get(a, 0))
    else:
        vector.setdefault("label_authority", "none")

    classification = _evidence.classify(vector)

    # --- hard gates (refusals that override any average improvement) --------
    refusal = None
    if score_mismatch:
        refusal = {"kind": "score_mismatch",
                   "reason": "a stored verdict disagrees with the score recomputed from audio; "
                             "the envelope was not produced by scoring this audio."}
    elif pcm_mismatch_any:
        refusal = {"kind": "provenance_mismatch",
                   "reason": "a recording's decoded PCM does not match the pcm_sha256 recorded in "
                             "its envelope provenance; the audio on disk is not the audio the "
                             "envelope claims."}
    elif same_pcm_any:
        refusal = {"kind": "same_audio",
                   "reason": "before and after decode to the same PCM for at least one fixture; "
                             "this is a re-score of the same recording, not a fresh result."}
    elif not (before_cov["complete"] and after_cov["complete"]):
        refusal = {"kind": "incomplete_fixture_set",
                   "reason": "before and after must each cover the complete pinned fixture "
                             f"universe; missing before={before_cov['missing']} "
                             f"after={after_cov['missing']}."}
    elif stimulus_mismatch and not receipts:
        refusal = {"kind": "stimulus_mismatch",
                   "reason": "the after-side caller stimulus differs from the before side and "
                             "no capture receipt binds this recording to the scenario; the pair "
                             "cannot be certified as the same scenario recaptured."}

    return {
        "manifest_hash": man.get("manifest_hash"),
        "before_rebuilt": before_rebuilt,
        "after_rebuilt": after_rebuilt,
        "per_fixture": {"before": per_before, "after": per_after},
        "coverage": {"before": before_cov, "after": after_cov},
        "flags": {
            "score_mismatch": score_mismatch,
            "same_pcm": same_pcm_any,
            "stimulus_mismatch": stimulus_mismatch,
            "unrecomputable": unrecomputable,
        },
        "evidence": classification,
        "refusal": refusal,
    }


__all__ = ["recompute_trial"]
