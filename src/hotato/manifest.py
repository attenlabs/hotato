"""Trial manifest: the immutable pin that makes a before/after proof honest.

Created BEFORE an experiment from the battery (the fixture universe + labels +
onsets + scripted-stimulus identity), it fixes:

  * the exact scorer (package version + a hash over ScoreConfig/VADParams),
  * one policy (pass conditions), applied to BOTH sides,
  * the COMPLETE fixture universe (so neither side can silently drop a fixture),
  * each fixture's expectation, onset, and scripted-stimulus PCM identity.

`hotato fix trial` then IGNORES stored verdicts and recomputes both sides under
this manifest. Before and after must reference the same ``manifest_hash``; a
changed policy, scorer, label, onset, or fixture set refuses the comparison.

Zero-dependency, deterministic. The kernel never invents randomness: the caller
supplies the ``nonce`` (fix_trial derives one from the battery + inputs so a
re-run is reproducible; a fleet runner supplies a real random nonce).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from typing import Optional

from . import __version__
from ._engine.score import ScoreConfig
from ._engine.vad import VADParams

SCHEMA_VERSION = "1"


def canonical_json(obj) -> str:
    """Deterministic JSON: sorted keys, no insignificant whitespace, so two
    equal objects hash identically regardless of key order."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _config_to_dict(cfg: ScoreConfig) -> dict:
    """A stable, fully-expanded dict of the scorer config (including nested
    VADParams), independent of dataclass identity."""
    def expand(v):
        if is_dataclass(v) and not isinstance(v, type):
            return {k: expand(x) for k, x in asdict(v).items()}
        return v
    if cfg is None:
        cfg = ScoreConfig()
    if cfg.caller_vad is None:
        cfg.caller_vad = VADParams()
    if cfg.agent_vad is None:
        cfg.agent_vad = VADParams()
    return {k: expand(v) for k, v in asdict(cfg).items()}


def score_config_hash(cfg: Optional[ScoreConfig] = None) -> tuple:
    """(config_dict, sha256) for the given scorer config."""
    d = _config_to_dict(cfg or ScoreConfig())
    return d, _sha256_str(canonical_json(d))


def normalize_policy(policy: Optional[dict]) -> dict:
    """Canonical pass-condition policy applied to BOTH sides of a trial.

    ``None`` bounds mean 'no numeric bound' -> pass depends only on the
    yield/hold expectation (the base barge-in semantics). A looser after-side
    policy cannot leak in because fix trial applies THIS one to both sides."""
    policy = policy or {}
    out = {
        "max_talk_over_sec": policy.get("max_talk_over_sec"),
        "max_time_to_yield_sec": policy.get("max_time_to_yield_sec"),
    }
    return out


def policy_hash(policy: dict) -> str:
    return _sha256_str(canonical_json(normalize_policy(policy)))


def fixture_key(event: dict) -> str:
    """Stable identity of one fixture: event_id + scenario_id (mirrors verify's
    pairing key)."""
    eid = str(event.get("event_id", ""))
    sid = event.get("scenario_id")
    return f"{eid}::{sid}" if sid else eid


def _stimulus_pcm(event: dict) -> Optional[str]:
    """The scripted caller-side decoded-PCM hash for a fixture, from its audio
    provenance. For a dual-mono pair, the ``caller`` side; for a stereo file,
    the file's own PCM hash (the caller channel is not separately hashed, so the
    whole-file identity is the best available scripted-stimulus pin)."""
    prov = event.get("audio_provenance") or {}
    sides = prov.get("sides") or []
    for s in sides:
        if s.get("role") == "caller":
            return s.get("pcm_sha256")
    # stereo / mono single-file: use the file PCM hash
    if len(sides) == 1:
        return sides[0].get("pcm_sha256")
    return None


def build_manifest(
    battery_env: dict,
    *,
    trial_id: str,
    nonce: str,
    policy: Optional[dict] = None,
    cfg: Optional[ScoreConfig] = None,
    min_n: int = 3,
    created_by: Optional[str] = None,
    workspace_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    deployment_id: Optional[str] = None,
    source_config_hash: Optional[str] = None,
    candidate_config_hash: Optional[str] = None,
    contract_set_hash: Optional[str] = None,
) -> dict:
    """Build an immutable trial manifest from a battery envelope.

    The battery defines the complete fixture universe, per-fixture expectation,
    onset, and scripted-stimulus identity. Only SCORABLE battery events become
    fixtures (a not-scorable battery entry cannot anchor a paired proof)."""
    cfg = cfg or ScoreConfig()
    cfg_dict, cfg_hash = score_config_hash(cfg)
    pol = normalize_policy(policy)
    fixtures = []
    for ev in battery_env.get("events", []):
        if ev.get("scorable") is False:
            continue
        expected_yield = bool(ev.get("expected_yield", True))
        measurements = ev.get("measurements") or {}
        fixtures.append({
            "fixture_id": fixture_key(ev),
            "event_id": ev.get("event_id"),
            "scenario_id": ev.get("scenario_id"),
            "expect": "yield" if expected_yield else "hold",
            "expected_yield": expected_yield,
            "onset_sec": measurements.get("caller_onset_sec"),
            "stimulus_pcm_sha256": _stimulus_pcm(ev),
            "label_id": ev.get("label_id"),
            "label_revision": ev.get("label_revision"),
        })
    fixtures.sort(key=lambda f: f["fixture_id"])
    body = {
        "schema_version": SCHEMA_VERSION,
        "trial_id": trial_id,
        "nonce": nonce,
        "created_by": created_by,
        "workspace_id": workspace_id,
        "agent_id": agent_id,
        "deployment_id": deployment_id,
        "source_config_hash": source_config_hash,
        "candidate_config_hash": candidate_config_hash,
        "scorer": {
            "package_version": __version__,
            "config_hash": cfg_hash,
            "config": cfg_dict,
        },
        "policy": {"policy_hash": policy_hash(pol), **pol},
        "fixtures": fixtures,
        "min_n": int(min_n),
        "contract_set_hash": contract_set_hash,
        "permitted_transformations": [],
        "hard_refusal_rules": [
            "same decoded PCM where fresh evidence is required",
            "stored verdict differs from recomputed verdict",
            "policy or scorer differs between sides",
            "fixture missing from either side",
            "capture origin unknown when a machine-verified recapture is claimed",
        ],
    }
    body["manifest_hash"] = compute_manifest_hash(body)
    return body


def compute_manifest_hash(manifest: dict) -> str:
    """sha256 over the canonical manifest body, excluding the hash + signature."""
    body = {k: v for k, v in manifest.items() if k not in ("manifest_hash", "signature")}
    return _sha256_str(canonical_json(body))


def verify_manifest_hash(manifest: dict) -> bool:
    return manifest.get("manifest_hash") == compute_manifest_hash(manifest)


def fixture_index(manifest: dict) -> dict:
    """fixture_id -> pinned fixture record."""
    return {f["fixture_id"]: f for f in manifest.get("fixtures", [])}


def coverage(manifest: dict, env: dict) -> dict:
    """How a run envelope covers the pinned fixture universe.

    Returns present / missing / extra fixture-id sets so a caller can refuse a
    subset (silent drop from BOTH sides) or an unexpected extra fixture."""
    pinned = set(fixture_index(manifest).keys())
    seen = set()
    for ev in env.get("events", []):
        if ev.get("scorable") is False:
            continue
        seen.add(fixture_key(ev))
    return {
        "present": sorted(pinned & seen),
        "missing": sorted(pinned - seen),
        "extra": sorted(seen - pinned),
        "complete": pinned == seen,
    }


__all__ = [
    "SCHEMA_VERSION", "canonical_json", "score_config_hash", "normalize_policy",
    "policy_hash", "fixture_key", "build_manifest", "compute_manifest_hash",
    "verify_manifest_hash", "fixture_index", "coverage",
]
