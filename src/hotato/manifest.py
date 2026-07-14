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
import hmac
import json
import os
from dataclasses import asdict, is_dataclass
from typing import Optional

from . import __version__
from ._engine.score import ScoreConfig
from ._engine.vad import VADParams
from .errors import open_regular as _open_regular

SCHEMA_VERSION = "1"


def canonical_json(obj) -> str:
    """Deterministic JSON: sorted keys, no insignificant whitespace, so two
    equal objects hash identically regardless of key order. ``allow_nan=False``
    rejects nan/inf/-inf (not RFC 8259 numbers) so a digest is never taken over
    a value that cannot round-trip through a standard JSON reader."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, allow_nan=False,
    )


def _sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# The scorer identity pins the SOURCE that actually determines a verdict: the
# package version marker (__init__), the scoring orchestration (core.py), and
# the entire vendored scoring engine (_engine/*.py). Hashing only __init__.py
# left _engine/score.py free to change while the pin stayed constant, so a later
# build (or a local edit) could re-score the same audio to a different verdict
# under an unchanged "wheel hash". This covers every byte that moves a score.
_SCORER_SOURCES = ("__init__.py", "core.py")


def wheel_hash() -> str:
    """Deterministic, network-free content hash of the installed scorer source:
    ``__init__.py`` + ``core.py`` + every module under ``_engine/``. Any edit to
    a byte that can change a verdict changes this hash. Returns the documented
    literal ``"unverified"`` when the package source cannot be located (so a
    manifest never fabricates a scorer identity it cannot back)."""
    try:
        import hotato as _pkg
        init = getattr(_pkg, "__file__", None)
        if not init or not os.path.exists(init):
            return "unverified"
        pkg_dir = os.path.dirname(os.path.abspath(init))
        parts = []  # (relpath, sha256) pairs, sorted for a stable manifest
        for name in _SCORER_SOURCES:
            fp = os.path.join(pkg_dir, name)
            if os.path.exists(fp):
                with _open_regular(fp) as fh:
                    parts.append((name, hashlib.sha256(fh.read()).hexdigest()))
        eng_dir = os.path.join(pkg_dir, "_engine")
        if os.path.isdir(eng_dir):
            for root, _dirs, files in os.walk(eng_dir):
                if "__pycache__" in root:
                    continue
                for f in files:
                    if not f.endswith(".py"):
                        continue
                    fp = os.path.join(root, f)
                    # Forward-slash the manifest key so the scorer-identity hash
                    # depends only on source CONTENT, not on the host path
                    # separator: os.sep is "/" on POSIX (no-op) but "\\" on
                    # Windows, which would otherwise give the same _engine source
                    # a different wheel_hash per OS.
                    rel = os.path.relpath(fp, pkg_dir).replace(os.sep, "/")
                    with _open_regular(fp) as fh:
                        parts.append((rel, hashlib.sha256(fh.read()).hexdigest()))
        if not parts:
            return "unverified"
        manifest = "\n".join(f"{rel}:{h}" for rel, h in sorted(parts))
        return hashlib.sha256(manifest.encode("utf-8")).hexdigest()
    except Exception:
        return "unverified"


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
    if not sid:
        return eid
    # collision-free: an event_id containing the separator can never forge
    # another fixture's key (JSON-encodes both components unambiguously).
    return json.dumps([eid, sid], separators=(",", ":"))


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
    adapter_name: Optional[str] = None,
    adapter_version: Optional[str] = None,
    permitted_transformations: Optional[list] = None,
) -> dict:
    """Build an immutable trial manifest from a battery envelope.

    The battery defines the complete fixture universe, per-fixture expectation,
    onset, and scripted-stimulus identity. Only SCORABLE battery events become
    fixtures (a not-scorable battery entry cannot anchor a paired proof)."""
    cfg = cfg or ScoreConfig()
    cfg_dict, cfg_hash = score_config_hash(cfg)
    pol = normalize_policy(policy)
    # Deferred import (K5): labelrecord depends on receipt, which depends on
    # this module (canonical_json) -- importing it at module top would be a
    # cycle. By the time build_manifest is actually CALLED, hotato.manifest is
    # already fully loaded, so this is safe.
    from . import labelrecord as _labelrecord
    fixtures = []
    for ev in battery_env.get("events", []):
        if ev.get("scorable") is False:
            continue
        expected_yield = bool(ev.get("expected_yield", True))
        stimulus_pcm = _stimulus_pcm(ev)
        label_record = ev.get("label_record")
        # label_authority is derived ONLY from a VALID signed label-record
        # bound to this event's decoded audio, or -- absent one -- whether the
        # scenario's own "expected" mapping was EXPLICITLY authored. NEVER
        # inferred from expected_yield's mere presence (that field is written
        # into every event unconditionally, human-authored or not -- the K5
        # bug this replaces).
        if label_record is not None:
            verification = _labelrecord.verify_label_record_local(
                label_record, event_pcm_sha256=stimulus_pcm)
            if verification.get("ok") and verification.get("authority") in (
                    "human", "human-shared"):
                label_authority = verification["authority"]
            else:
                # A label-record was SUPPLIED (tampered, wrong/untrusted key,
                # or not bound to this exact audio) but did not verify: a
                # refusal, never a silent downgrade to "asserted".
                label_authority = "invalid"
        elif bool(ev.get("expected_yield_explicit", False)):
            label_authority = "asserted"
        else:
            label_authority = "none"
        measurements = ev.get("measurements") or {}
        fixtures.append({
            "fixture_id": fixture_key(ev),
            "event_id": ev.get("event_id"),
            "scenario_id": ev.get("scenario_id"),
            "expect": "yield" if expected_yield else "hold",
            "expected_yield": expected_yield,
            "onset_sec": measurements.get("caller_onset_sec"),
            "stimulus_pcm_sha256": stimulus_pcm,
            "label_id": ev.get("label_id"),
            "label_revision": ev.get("label_revision"),
            "label_authority": label_authority,
            # Carried so a later verify can re-check the same label-record
            # (bound to stimulus_pcm_sha256 above) independently of this
            # manifest build's own trust resolution.
            "label_record": label_record,
        })
    fixtures.sort(key=lambda f: f["fixture_id"])
    # Derived, deterministic from the (sorted) fixture universe:
    #  * required_yield_targets  -- scorable yield fixtures capable of a scorable
    #    fail (every manifest fixture is scorable by construction, so a yield
    #    fixture here can produce a fail); the paired proof must move these.
    #  * required_hold_guards    -- the opposite-risk hold fixtures that must not
    #    regress (a fix must never trade talk-over for a false yield).
    required_yield_targets = [f["fixture_id"] for f in fixtures if f["expected_yield"]]
    required_hold_guards = [f["fixture_id"] for f in fixtures if not f["expected_yield"]]
    # Capture plan: the scripted caller-side stimulus PCM identity per fixture,
    # best-effort from each event's audio_provenance, plus one combined hash so a
    # recapture that replays the same stimuli is bound to this manifest.
    per_fixture_stimulus = {f["fixture_id"]: f["stimulus_pcm_sha256"] for f in fixtures}
    capture_plan = {
        "scenario_stimulus_hash": _sha256_str(canonical_json(per_fixture_stimulus)),
        "per_fixture": per_fixture_stimulus,
    }
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
            "wheel_hash": wheel_hash(),
        },
        "policy": {"policy_hash": policy_hash(pol), **pol},
        "fixtures": fixtures,
        "required_yield_targets": required_yield_targets,
        "required_hold_guards": required_hold_guards,
        "capture_plan": capture_plan,
        "min_n": int(min_n),
        "contract_set_hash": contract_set_hash,
        # A real, documented list field (default empty): the transforms a
        # recapture is ALLOWED to apply to the caller stimulus (e.g. codec
        # conversion) without being treated as a different scenario.
        "permitted_transformations": list(permitted_transformations or []),
        "hard_refusal_rules": [
            "same decoded PCM where fresh evidence is required",
            "stored verdict differs from recomputed verdict",
            "policy or scorer differs between sides",
            "fixture missing from either side",
            "capture origin unknown when a machine-verified recapture is claimed",
        ],
    }
    # Optional adapter identity: included ONLY when supplied (kept additive so an
    # unspecified adapter does not inject a null field into the hashed body).
    if adapter_name is not None:
        body["adapter"] = {"name": adapter_name, "version": adapter_version}
    body["manifest_hash"] = compute_manifest_hash(body)
    return body


def compute_manifest_hash(manifest: dict) -> str:
    """sha256 over the canonical manifest body, excluding the hash + signature."""
    body = {k: v for k, v in manifest.items() if k not in ("manifest_hash", "signature")}
    return _sha256_str(canonical_json(body))


def verify_manifest_hash(manifest: dict) -> bool:
    return manifest.get("manifest_hash") == compute_manifest_hash(manifest)


def _manifest_subject(manifest: dict) -> str:
    """Canonical digest of the manifest body EXCLUDING the signature (it includes
    ``manifest_hash``), so a signature cannot be lifted onto a different body."""
    body = {k: v for k, v in manifest.items() if k != "signature"}
    return _sha256_str(canonical_json(body))


def sign_manifest(manifest: dict, key: bytes) -> dict:
    """Return a COPY of ``manifest`` with an HMAC-SHA256 signature over its
    canonical body (mirrors :mod:`hotato.receipt` signing). Optional: a built
    manifest is unsigned by default; signing is a separate, explicit call and
    leaves ``manifest_hash`` intact (that hash excludes the signature)."""
    subject = _manifest_subject(manifest)
    sig = hmac.new(key, subject.encode("ascii"), hashlib.sha256).hexdigest()
    signed = dict(manifest)
    signed["signature"] = {
        "algorithm": "hmac-sha256",
        "subject_digest": subject,
        "value": sig,
    }
    return signed


def verify_manifest_signature(manifest: dict, key: bytes) -> dict:
    """Check a manifest's HMAC signature under ``key``.

    Returns ``{ok, signed, reason}``. ``ok`` is True only when a signature is
    present, its subject digest matches the current body, and the HMAC verifies
    -- so any post-signing edit to the body fails verification."""
    sig = manifest.get("signature")
    if not sig or sig.get("algorithm") in (None, "none"):
        return {"ok": False, "signed": False,
                "reason": "unsigned manifest: no HMAC signature to verify"}
    subject = _manifest_subject(manifest)
    if subject != sig.get("subject_digest"):
        return {"ok": False, "signed": True,
                "reason": "manifest body was altered after signing (subject digest mismatch)"}
    expected = hmac.new(key, subject.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig.get("value", "")):
        return {"ok": False, "signed": True,
                "reason": "manifest signature does not verify under this key"}
    return {"ok": True, "signed": True, "reason": "manifest signature verified"}


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
    "policy_hash", "fixture_key", "wheel_hash", "build_manifest",
    "compute_manifest_hash", "verify_manifest_hash", "sign_manifest",
    "verify_manifest_signature", "fixture_index", "coverage",
]
