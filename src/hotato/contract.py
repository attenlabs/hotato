"""``hotato contract create/verify/inspect/pack/unpack``: the portable failure
contract.

A contract turns ONE real call moment -- a recording you already have, the
onset the caller took or attempted the floor, and YOUR label for what the
agent should have done -- into a self-contained bundle directory
(``<id>.hotato/``) that carries the audio, the frame-level timing evidence, an
input-health (trust) report, a shareable SVG card, a CI pass/fail policy, and
the exact commands to replay and re-verify it. See ``docs/CONTRACTS.md`` for
the full bundle layout.

Hotato does not infer intent and does not prove authorization, identity,
compliance, or policy safety. A contract's ``label.expected_behavior`` is
always a human call (``label_source`` is frozen to ``"human"``); Hotato
measures whether the recorded timing matched that label, and ``contract
verify`` re-measures the SAME recording later (after an engine upgrade, a
config change, or a re-capture) and reports pass/fail for CI.

This module deliberately WRAPS the existing primitives instead of
reimplementing them:

* :func:`hotato.fixture.create_fixture` / candidate resolution
  (:func:`hotato.fixture.parse_candidate_ref` and friends) do the input
  parsing, onset clipping, and the round-trip scorability validation -- a
  contract creation refuses a not-scorable moment with the SAME honest reason
  fixture creation does, before any bundle file is written;
* :func:`hotato.trust.trust_report` is the input-health check written to
  ``evidence/trust.json``;
* :func:`hotato.core.dump_frames_for_input` is the frame-level evidence
  written to ``evidence/frames.jsonl``;
* :mod:`hotato.report`'s timeline model and SVG renderer draw
  ``evidence/timeline.html`` and ``reports/initial.html``;
* :mod:`hotato.card` renders ``evidence/card.svg``.

A single-channel (mono) recording is rejected by default, exactly like
``fixture create``: caller and agent cannot be told apart on one channel. The
opt-in ``--mono --diarize`` path (mirroring ``hotato run --mono --diarize``)
scores a diarized-mono recording through the SAME quality-gated diarizer
front-end and NEVER silently upgrades an indicative-only verdict to a
confident one: ``measurement.indicative_only`` is carried into the contract
and every renderer, and frame-level evidence (which the diarized-mono path
does not produce) is honestly reported as unavailable rather than fabricated.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from typing import Optional

from . import attest as _attest
from . import card as _card
from . import fixture as _fixture
from . import labelrecord as _labelrecord
from . import report as _report
from . import trust as _trust
from ._engine.score import ScoreConfig
from .core import dump_frames_for_input, run_single
from .errors import open_regular as _open_regular
from .errors import require_regular_file as _require_regular_file
from .errors import wav_read as _wav_read

__all__ = [
    "SCHEMA",
    "BUNDLE_SUFFIX",
    "create_contract",
    "render_create_text",
    "create_result_json",
    "discover_bundles",
    "verify_contracts",
    "render_verify_text",
    "render_verify_html",
    "render_verify_junit",
    "verify_result_json",
    "inspect_contract",
    "render_inspect_text",
    "pack_contract",
    "render_pack_text",
    "pack_result_json",
    "unpack_contract",
    "render_unpack_text",
    "unpack_result_json",
]

SCHEMA = "hotato.contract.v1"
CREATED_BY = "hotato contract create"
BUNDLE_SUFFIX = ".hotato"
MANIFEST_NAME = "MANIFEST.sha256.json"

# --- unpack hardening: a .hotato archive is meant to travel between teams --
# (`contract pack` on one machine, `contract unpack` on another), so it is
# untrusted input, not just a corruption check. Every limit below is
# enforced as a ValueError, so a hostile archive hits the SAME clean exit-2
# contract as any other unpack usage error (see errors.HANDLED) -- never an
# uncaught exception or a partially-written --out.
DEFAULT_MAX_UNPACK_BYTES = 512 * 1024 * 1024  # 512 MiB of real bundle content
_MAX_UNPACK_BYTES_ENV = "HOTATO_CONTRACT_MAX_UNPACK_BYTES"
# A real bundle carries on the order of 15-30 files (contract.json, audio,
# evidence/*, source/*, policy/*, reports/*, ci/*, provenance.json, plus any
# traces); this is generous headroom against a many-tiny-members bomb while
# still being cheap to reject before any member is opened.
MAX_UNPACK_MEMBERS = 5_000
# Per-member compression-ratio bomb: below this declared size, ratio alone is
# noise (a tiny file that compresses well is not a threat by itself). A
# single-pass DEFLATE stream tops out near ~1032:1 on pathological input;
# 300:1 is already far outside anything a real bundle member (audio/json/
# html/svg) produces.
_RATIO_BOMB_MIN_DECLARED_BYTES = 1_000_000
_RATIO_BOMB_MAX_RATIO = 300
_UNPACK_COPY_CHUNK_BYTES = 1024 * 1024
_DRIVE_LETTER_RE = re.compile(r"^[A-Za-z]:")

# Same slug rule fixture ids and the corpus label schema use.
_SLUG_RE = _fixture._SLUG_RE

# Relative paths every bundle carries (contract.json["bundle"]["paths"] is
# built from this constant so the schema and the writer can never drift).
_REL = {
    "contract": "contract.json",
    "audio": "audio/event.wav",
    "evidence": {
        "frames": "evidence/frames.jsonl",
        "timeline": "evidence/timeline.html",
        "trust": "evidence/trust.json",
        "card": "evidence/card.svg",
        "label_record": "evidence/label_record.json",
    },
    "traces_dir": "traces",
    "source": {
        "call_metadata": "source/call_metadata.json",
        "stack_config_snapshot": "source/stack_config_snapshot.json",
    },
    "policy": "policy/verify.yaml",
    "reports": {
        "initial": "reports/initial.html",
        "after": "reports/after.html",
    },
    "provenance": "provenance.json",
    "ci": {
        "github_action": "ci/github-action.yml",
        "junit": "ci/junit.xml",
    },
}

_NOT_PROVED = (
    "Hotato does not prove authorization, identity, compliance, or policy "
    "safety. Hotato proves timing behavior against this explicit contract."
)

# Every `contract verify` run re-scores the bundled audio.wav that shipped
# with each contract -- the SAME bytes every time, never a new recording. A
# reader who has not internalized the two-lane table in docs/CONTRACTS.md can
# easily mistake a green run here for "the deployed agent is fine today," so
# every render of this report says the opposite outright. See
# docs/RECAPTURE.md for the fresh-capture lane this report is NOT.
_STORED_EVIDENCE_CAVEAT = (
    "This result re-measures stored evidence. It does not test the current "
    "agent."
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with _open_regular(path) as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _decoded_pcm_sha256(path: str) -> str:
    """sha256 over the DECODED PCM samples of a WAV (all channels, interleaved),
    independent of container/codec framing. Bound into a contract's signed
    subject so `contract verify` can detect a bundle whose audio was replaced
    after signing even if the new file re-encodes to different raw bytes."""
    h = hashlib.sha256()
    with _wav_read(path) as wf:
        while True:
            chunk = wf.readframes(1 << 16)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _sha256_two_files(a: str, b: str) -> str:
    """A deterministic combined hash for a caller+agent mono pair: order-
    stable (caller then agent), so re-running on the same two files always
    reproduces the same source hash."""
    h = hashlib.sha256()
    for p in (a, b):
        h.update(_sha256_file(p).encode("ascii"))
    return h.hexdigest()


def _mkparents(path: str) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)


def _write_text(path: str, text: str) -> None:
    _mkparents(path)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _write_json(path: str, obj) -> None:
    _write_text(path, json.dumps(obj, indent=2, sort_keys=True) + "\n")


# --- create ------------------------------------------------------------

def create_contract(
    *,
    from_candidate: Optional[str] = None,
    stereo: Optional[str] = None,
    caller: Optional[str] = None,
    agent: Optional[str] = None,
    mono: Optional[str] = None,
    diarize: bool = False,
    diarizer: str = "pyannote",
    caller_speaker: Optional[str] = None,
    agent_speaker: Optional[str] = None,
    egress_opt_in: bool = False,
    contract_id: str,
    expect: str,
    out_dir: str,
    onset_sec: Optional[float] = None,
    folder: Optional[str] = None,
    stack: Optional[str] = None,
    max_talk_over_sec: Optional[float] = None,
    max_time_to_yield_sec: Optional[float] = None,
    rationale: Optional[str] = None,
    pre_sec: float = 2.0,
    post_sec: float = 6.0,
    no_clip: bool = False,
    force: bool = False,
    caller_channel: int = 0,
    agent_channel: int = 1,
    include_identifiers: bool = False,
    confirm_channels: bool = False,
    reviewer_principal: Optional[str] = None,
) -> dict:
    """Create one ``<id>.hotato`` failure-contract bundle under ``out_dir``.

    Exactly ONE input is required: ``from_candidate`` (a ``FILE#N`` /
    ``FILE#CALL:N`` sweep/analyze candidate ref), ``stereo`` (a two-channel
    WAV, with ``onset_sec``), ``caller``+``agent`` (two mono WAVs, with
    ``onset_sec``), or ``mono`` (with ``diarize=True``, the opt-in
    diarized-mono path). A single channel passed as ``stereo`` (or a bare
    ``mono`` without ``diarize``) is refused, never silently scored.

    The moment is validated by scoring it immediately, through the SAME
    round-trip guarantee ``fixture create`` gives: a not-scorable input is
    refused with the honest reason (raises ``ValueError``, CLI exit 2) and no
    bundle is written. The bundle is built in a temp directory next to
    ``out_dir`` and moved into place with one atomic rename, so a crash or
    kill mid-write never leaves a half-written bundle at the final path.

    Returns a result dict: ``{"id", "dir", "contract", "paths", "next"}``.

    ``confirm_channels`` (K6): a HUMAN explicit confirmation that the caller/
    agent channel mapping is correct despite a suspected swap, or (the
    caller's own responsibility) authenticated provider metadata confirming
    it. The bundle is still WRITTEN and candidate-eligible even without it --
    a suspected swap or crosstalk/leakage never blocks contract creation
    outright -- but the recorded ``measurement`` carries a NULL verdict
    (did_yield/seconds_to_yield/talk_over_sec/passed) and
    ``measurement.verdict_ineligible_reason`` until confirmed. The
    confirmation is bound into the contract's attestation digest, so
    ``contract verify`` re-derives verdict eligibility against the recording
    on disk and honors this SAME recorded confirmation, never a fresh
    unverified flag.

    ``reviewer_principal`` (K5, ``--from-candidate``/``--stereo``/
    ``--caller``+``--agent`` paths only): the human reviewer's name to bind
    into the signed label-record :func:`hotato.fixture.create_fixture` mints
    for this event (falling back to ``fixture._default_reviewer_principal()``
    -- ``HOTATO_REVIEWER``/``USER``/``USERNAME`` -- when omitted). The minted
    record (or ``None`` if no signing key is configured anywhere) is carried
    on the returned contract as ``contract["label_record"]``, and an
    HONEST tier -- ``"human"`` (Ed25519), ``"human-shared"`` (HMAC),
    ``"asserted"`` (no key configured, never fabricated), or ``"invalid"``
    (a record was minted but does not locally verify) -- as
    ``contract["label_authority"]``. The reviewer name itself is also bound
    into ``contract["identity"]["reviewer"]`` and the attestation digest, so
    tampering with it after creation is caught by ``contract verify``.
    """
    if not _SLUG_RE.match(contract_id or ""):
        raise ValueError(
            f"--id {contract_id!r} is not a valid contract id; use a "
            "lowercase slug like refund-cutoff-001 (letters, digits, hyphens)"
        )
    if str(expect).strip().lower() not in ("yield", "hold"):
        raise ValueError(f"--expect must be 'yield' or 'hold', got {expect!r}")
    want_yield = str(expect).strip().lower() == "yield"

    modes = {
        "from_candidate": bool(from_candidate),
        "stereo": bool(stereo),
        "caller_agent": bool(caller or agent),
        "mono": bool(mono),
    }
    chosen = [k for k, v in modes.items() if v]
    if len(chosen) != 1:
        raise ValueError(
            "provide exactly ONE input: --from-candidate FILE#N, --stereo "
            "FILE, --caller FILE + --agent FILE, or --mono FILE (with "
            "--diarize); got " + (", ".join(chosen) or "none")
        )
    if not out_dir:
        raise ValueError("--out DIR is required (e.g. --out contracts)")

    bundle_dir = os.path.join(out_dir, contract_id + BUNDLE_SUFFIX)
    if os.path.exists(bundle_dir) and not force:
        raise ValueError(
            f"contract {contract_id!r} already exists ({bundle_dir}); pass "
            "--force to overwrite it, or pick a new --id"
        )

    os.makedirs(out_dir, exist_ok=True)
    tmp_dir = tempfile.mkdtemp(prefix=f".{contract_id}.hotato.tmp-", dir=out_dir)
    try:
        if mono:
            contract = _create_diarized_mono(
                tmp_dir,
                mono=mono, diarize=diarize, diarizer=diarizer,
                caller_speaker=caller_speaker, agent_speaker=agent_speaker,
                egress_opt_in=egress_opt_in,
                contract_id=contract_id, want_yield=want_yield, expect=expect,
                onset_sec=onset_sec, stack=stack,
                max_talk_over_sec=max_talk_over_sec,
                max_time_to_yield_sec=max_time_to_yield_sec,
                rationale=rationale, include_identifiers=include_identifiers,
            )
        else:
            contract = _create_from_fixture_path(
                tmp_dir,
                from_candidate=from_candidate, stereo=stereo,
                caller=caller, agent=agent, folder=folder,
                contract_id=contract_id, want_yield=want_yield, expect=expect,
                onset_sec=onset_sec, stack=stack,
                max_talk_over_sec=max_talk_over_sec,
                max_time_to_yield_sec=max_time_to_yield_sec,
                rationale=rationale, pre_sec=pre_sec, post_sec=post_sec,
                no_clip=no_clip, caller_channel=caller_channel,
                agent_channel=agent_channel,
                include_identifiers=include_identifiers,
                confirm_channels=confirm_channels,
                reviewer_principal=reviewer_principal,
            )
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    if os.path.exists(bundle_dir):
        shutil.rmtree(bundle_dir)
    os.replace(tmp_dir, bundle_dir)

    paths = {k: os.path.join(bundle_dir, v) if isinstance(v, str) else
             {kk: os.path.join(bundle_dir, vv) for kk, vv in v.items()}
             for k, v in _REL.items()}
    return {
        "id": contract_id,
        "dir": bundle_dir,
        "contract": contract,
        "paths": paths,
        "next": f"hotato contract verify {shlex.quote(out_dir)}",
    }


def _base_policy(want_yield: bool, max_talk_over_sec, max_time_to_yield_sec) -> dict:
    pass_conditions = {"yield": want_yield}
    if want_yield:
        pass_conditions["max_talk_over_sec"] = max_talk_over_sec
        pass_conditions["max_time_to_yield_sec"] = max_time_to_yield_sec
    else:
        pass_conditions["max_talk_over_sec"] = None
        pass_conditions["max_time_to_yield_sec"] = None
    return {
        "pass_conditions": pass_conditions,
        # The opposite axis is not tested by ONE contract; a battery of
        # contracts (some yield, some hold) is what exercises it. Recorded
        # here so a downstream verify --policy battery can require it.
        "opposite_risk_required": want_yield,
    }


def _measurement_from_event(event: dict, *, indicative_only: bool = False,
                            diarization=None, verdict_eligible: bool = True,
                            verdict_ineligible_reason: Optional[str] = None) -> dict:
    """Build the contract's ``measurement`` block. K6: ``verdict_eligible`` is
    the trust-layer channel-mapping gate (a suspected swap or crosstalk/leakage
    at the contract-mode threshold), NARROWER than and independent of
    ``scorable`` (the engine's own not-scorable gate). Either one being False
    nulls did_yield/seconds_to_yield/talk_over_sec/passed -- a suspected swap
    or high crosstalk can never silently produce a verdict."""
    scorable = event.get("scorable") is not False
    verdict_ok = scorable and verdict_eligible
    v = event.get("verdict") or {}
    return {
        "scorable": scorable,
        "not_scorable_reason": event.get("not_scorable_reason"),
        "verdict_eligible": verdict_ok,
        "verdict_ineligible_reason": (
            None if verdict_ok else
            (event.get("not_scorable_reason") if not scorable
             else verdict_ineligible_reason)
        ),
        "did_yield": v.get("did_yield") if verdict_ok else None,
        "seconds_to_yield": v.get("seconds_to_yield") if verdict_ok else None,
        "talk_over_sec": v.get("talk_over_sec") if verdict_ok else None,
        "passed": v.get("passed") if verdict_ok else None,
        "indicative_only": bool(indicative_only),
        "diarization": diarization,
    }


def _channel_verdict_eligible(trust_rep: dict):
    """K6 gate for the contract's measurement, from a ``trust_report``:
    ``(verdict_eligible, verdict_ineligible_reason)``.

    ONLY the swap/crosstalk signal gates the measurement here. Trust's own
    broader not-scorable / candidate-eligibility finding on the (often short,
    already-clipped) bundle audio is an ORTHOGONAL, pre-existing, purely
    informational check -- recorded unchanged in ``contract.trust`` -- that
    never gated the measurement before K6 and must not start to now (the
    engine's OWN ``event.get('scorable')`` -- via ``fixture.create_fixture``'s
    round-trip validation -- remains the sole not-scorable gate on the
    measurement). Coupling trust's broader gate in here would refuse
    contracts whose engine-scored clip is perfectly good evidence merely
    because trust's independent VAD read the same short clip differently."""
    if not trust_rep.get("scorable"):
        return True, None
    return bool(trust_rep.get("verdict_eligible")), trust_rep.get("verdict_ineligible_reason")


def _trust_block(trust_rep: dict) -> dict:
    channels = trust_rep.get("channels")
    return {
        "status": trust_rep.get("recommendation"),
        "scorable": bool(trust_rep.get("scorable")),
        "warnings": list(trust_rep.get("warnings") or []),
        "possible_swap": (channels or {}).get("possible_swap"),
        # K6: the channel-mapping verdict-eligibility gate, carried immutably
        # (bound into the attestation digest) so a re-verify can honor the SAME
        # human confirmation recorded at creation, never a fresh unverified one.
        "verdict_eligible": trust_rep.get("verdict_eligible"),
        "verdict_ineligible_reason": trust_rep.get("verdict_ineligible_reason"),
        "channel_mapping_confirmed": bool(trust_rep.get("channel_map_confirmed")),
    }


def _policy_yaml_text(contract_id: str, want_yield: bool) -> str:
    guard = "require_yield_fixture" if want_yield else "require_hold_fixture"
    return (
        f"# hotato contract policy for {contract_id}\n"
        "# Generated by `hotato contract create`. This is the SAME subset\n"
        "# `hotato verify --policy` reads (see docs/CONTRACTS.md and\n"
        "# docs/FIX-LOOP.md): wire it into a before/after battery once you\n"
        "# have re-captured this moment after a fix. Edit by hand.\n"
        "target:\n"
        "  improve:\n"
        "    failed_count: decrease\n"
        "guardrails:\n"
        "  max_new_false_yields: 0\n"
        "  max_not_scorable: 0\n"
        f"  {guard}: true\n"
    )


def _github_action_yaml(contracts_dir: str = "contracts") -> str:
    return (
        "name: hotato contracts\n"
        "on:\n"
        "  push:\n"
        "  pull_request:\n"
        "  schedule:\n"
        "    - cron: \"0 6 * * 1\"\n"
        "jobs:\n"
        "  verify:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - uses: actions/setup-python@v5\n"
        "        with:\n"
        "          python-version: \"3.12\"\n"
        "      - name: verify hotato contracts\n"
        "        run: >-\n"
        f"          uvx hotato contract verify {contracts_dir}/\n"
        "          --junit contracts-junit.xml --format json > contracts-verify.json\n"
        "      - name: publish JUnit\n"
        "        if: always()\n"
        "        uses: actions/upload-artifact@v4\n"
        "        with:\n"
        "          name: hotato-contracts-junit\n"
        "          path: contracts-junit.xml\n"
    )


def _after_report_placeholder(contract_id: str) -> str:
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>hotato contract {contract_id}: after (pending)</title></head>"
        "<body style=\"font:15px system-ui;background:#1b1714;color:#f1e8d7;"
        "padding:32px\">"
        f"<h1>{contract_id}: no fix verified yet</h1>"
        "<p>This contract has not been re-verified after a fix. Re-capture "
        "the failing moment, then run <code>hotato verify --before "
        "reports/initial.html-equivalent-run.json --after after-run.json</code> "
        "or <code>hotato contract verify</code> once the recording is "
        "replaced, and overwrite this file with the result.</p>"
        f"<p>{_NOT_PROVED}</p></body></html>\n"
    )


def _call_metadata(*, stack, expect, category, recording_type, channels,
                    duration_sec, candidate_ref, candidate_kind, source_name,
                    include_identifiers: bool) -> dict:
    out = {
        "stack": stack or "generic",
        "expect": expect,
        "category": category,
        "recording_type": recording_type,
        "channels": channels,
        "duration_sec": duration_sec,
        "candidate_ref": candidate_ref if include_identifiers else None,
        "candidate_kind": candidate_kind,
        "note": (
            "redacted by default: a call id, a filesystem path, and a vendor "
            "recording name are not stored here unless --include-identifiers "
            "was passed at creation time."
        ),
    }
    if include_identifiers and source_name:
        out["source_name"] = source_name
    return out


def _stack_config_snapshot(stack: Optional[str]) -> dict:
    return {
        "stack": stack or "generic",
        "config": {},
        "note": (
            "no live stack connection at contract-creation time, so this is "
            "a placeholder, not a fabricated snapshot. Populate it by hand "
            "(or from `hotato inspect --stack ... --format json`) before "
            "relying on it for a config diff."
        ),
    }


def _provenance(*, contract_id, created_by, candidate_ref, source_sha256,
               rationale) -> dict:
    return {
        "tool": "hotato",
        "schema": "hotato.contract-provenance.v1",
        "id": contract_id,
        "created_at": _now_iso(),
        "created_by": created_by,
        "candidate_ref": candidate_ref,
        "source_audio_sha256": source_sha256,
        "rationale": rationale,
    }


# --- evidence rendering (reuses report.py's model + SVG, never redraws) ----

_TIMELINE_CSS = """
body{margin:0;background:#1b1714;color:#f1e8d7;
 font:15px/1.5 -apple-system,'Helvetica Neue',Helvetica,Arial,sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:28px 20px}
h1{font-size:19px;margin:0 0 4px}
.sub{color:#b7ab97;font-size:13px;margin:0 0 18px}
.tl{background:#241f1a;border:1px solid #3a3128;border-radius:10px;
 padding:14px 16px;margin-bottom:16px}
.stats{display:flex;flex-wrap:wrap;gap:14px 26px}
.stat{display:flex;flex-direction:column;gap:2px}
.stat .k{color:#b7ab97;font-size:11.5px;text-transform:uppercase;
 letter-spacing:.04em}
.stat .v{font:600 15px/1 'SFMono-Regular',Menlo,Consolas,monospace}
.note{color:#b7ab97;font-size:12.5px;margin-top:18px}
"""


def _render_timeline_html(model: dict, *, contract_id: str, expect: str) -> str:
    """A compact, self-contained evidence page: just the to-scale SVG
    timeline plus the measured stat chips, drawn from the SAME event model
    and ``_svg_timeline`` renderer ``hotato report`` uses. This is
    deliberately smaller than ``reports/initial.html`` (no analytics, no
    thresholds, no frame inspector), so the two evidence artifacts are not a
    duplicate of each other."""
    esc = _report._esc
    s = _report._s
    svg = (_report._svg_timeline(model) if model["has_frames"] else
          '<div class="note">no frame data for this event.</div>')
    stats = [
        ("caller onset", s(model["onset"])),
        ("time to yield", s(model["seconds_to_yield"])),
        ("talk-over", s(model["talk_over_sec"])),
        ("response gap", s(model["response_gap_sec"])),
    ]
    stat_html = "".join(
        f'<div class="stat"><span class="k">{esc(k)}</span>'
        f'<span class="v">{esc(v)}</span></div>' for k, v in stats
    )
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>hotato contract {esc(contract_id)}: timeline evidence</title>"
        f"<style>{_TIMELINE_CSS}</style></head><body><div class=\"wrap\">"
        f"<h1>{esc(contract_id)}: timeline evidence</h1>"
        f'<p class="sub">expect {esc(expect)} &middot; frame-level evidence '
        "from the same scorer hotato run/verify use.</p>"
        f'<div class="tl">{svg}</div>'
        f'<div class="stats">{stat_html}</div>'
        f'<p class="note">{esc(_NOT_PROVED)}</p>'
        "</div></body></html>\n"
    )


def _render_mono_timeline_placeholder(contract_id: str, trust_rep: dict) -> str:
    esc = _report._esc
    tier = ((trust_rep.get("diarization") or {}).get("confidence_tier")
            or trust_rep.get("confidence_tier") or "unknown")
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>hotato contract {esc(contract_id)}: timeline evidence "
        "(unavailable)</title>"
        f"<style>{_TIMELINE_CSS}</style></head><body><div class=\"wrap\">"
        f"<h1>{esc(contract_id)}: frame-level timeline not available</h1>"
        '<p class="sub">this contract was created from a diarized-mono '
        "recording.</p>"
        "<p>Frame-level timing evidence (the caller/agent activity tracks "
        "drawn to scale) is produced by the dual-channel frame dump, which "
        "the diarized-mono path does not run in this release. See "
        f"<code>evidence/trust.json</code> for the separation confidence "
        f"tier this contract was scored at (<b>{esc(tier)}</b>) instead of a "
        "fabricated timeline."
        f"</p><p class=\"note\">{esc(_NOT_PROVED)}</p>"
        "</div></body></html>\n"
    )


def _frame_lines(dump: dict) -> str:
    meta = {
        "_meta": True,
        "source": dump.get("source"),
        "sample_rate": dump.get("sample_rate"),
        "hop_sec": dump.get("hop_sec"),
        "caller_onset_sec": dump.get("caller_onset_sec"),
        "config": dump.get("config"),
    }
    lines = [json.dumps(meta, sort_keys=True)]
    for f in dump.get("frames") or []:
        lines.append(json.dumps(f, sort_keys=True))
    return "\n".join(lines) + "\n"


def _mono_frame_lines(reason: str) -> str:
    return json.dumps({
        "_meta": True,
        "available": False,
        "reason": reason,
    }, sort_keys=True) + "\n"


# --- create: stereo / caller+agent / from-candidate (wraps fixture.py) ----

def _resolve_raw_input(*, from_candidate, stereo, caller, agent, folder,
                       onset_sec):
    """Resolve the non-mono input modes to ``(fixture_kwargs, onset_sec,
    recording_type, candidate_ref, candidate_kind)``. Reuses the EXACT
    candidate-ref resolution ``fixture promote`` uses, so a contract and a
    fixture agree on which recording and onset a ref names."""
    if from_candidate:
        path, call, number = _fixture.parse_candidate_ref(from_candidate)
        doc = _fixture._load_result(path)
        cand = _fixture._resolve_candidate(doc, path=path, call=call, number=number)
        audio = _fixture._resolve_source_audio(doc, cand, ref_path=path, folder=folder)
        return (
            {"stereo": audio},
            float(cand["t_sec"]),
            "stereo",
            from_candidate,
            cand.get("kind"),
        )
    if stereo:
        if onset_sec is None:
            raise ValueError("--onset is required with --stereo")
        return {"stereo": stereo}, onset_sec, "stereo", None, None
    # caller + agent (fixture.create_fixture itself refuses an incomplete pair)
    if onset_sec is None:
        raise ValueError("--onset is required with --caller/--agent")
    return {"caller": caller, "agent": agent}, onset_sec, "caller+agent", None, None


def _create_from_fixture_path(
    tmp_dir, *, from_candidate, stereo, caller, agent, folder,
    contract_id, want_yield, expect, onset_sec, stack, max_talk_over_sec,
    max_time_to_yield_sec, rationale, pre_sec, post_sec, no_clip,
    caller_channel, agent_channel, include_identifiers, confirm_channels=False,
    reviewer_principal=None,
) -> dict:
    fx_kwargs, resolved_onset, recording_type, candidate_ref, candidate_kind = (
        _resolve_raw_input(from_candidate=from_candidate, stereo=stereo,
                           caller=caller, agent=agent, folder=folder,
                           onset_sec=onset_sec)
    )
    source_audio = (fx_kwargs.get("stereo")
                    or fx_kwargs.get("caller"))  # for the pre-clip sha256
    if recording_type == "caller+agent":
        source_sha = _sha256_two_files(fx_kwargs["caller"], fx_kwargs["agent"])
        source_name = (os.path.basename(fx_kwargs["caller"]) + "+"
                       + os.path.basename(fx_kwargs["agent"]))
    else:
        source_sha = _sha256_file(fx_kwargs["stereo"])
        source_name = os.path.basename(fx_kwargs["stereo"])

    # K5: resolve the reviewer name ONCE, before minting, so the label-record's
    # own reviewer_principal and the contract's identity.reviewer (bound into
    # the attestation digest) can never independently drift onto two different
    # fallback values.
    resolved_reviewer = (reviewer_principal
                         or _fixture._default_reviewer_principal())

    with tempfile.TemporaryDirectory(prefix="hotato-contract-fx-") as fx_root:
        fx_result = _fixture.create_fixture(
            stereo=fx_kwargs.get("stereo"),
            caller=fx_kwargs.get("caller"),
            agent=fx_kwargs.get("agent"),
            fixture_id=contract_id,
            title=None,
            onset_sec=resolved_onset,
            expect=expect,
            out_dir=fx_root,
            stack=stack,
            max_talk_over_sec=max_talk_over_sec,
            max_time_to_yield_sec=max_time_to_yield_sec,
            pre_sec=pre_sec,
            post_sec=post_sec,
            no_clip=no_clip,
            force=True,
            caller_channel=caller_channel,
            agent_channel=agent_channel,
            created_by=CREATED_BY,
            reviewer_principal=resolved_reviewer,
        )
        # A ValueError above (not-scorable) propagates as-is: no bundle files
        # are written before this point, matching fixture create's own
        # honest, nothing-partial-left-behind refusal.
        clipped_audio = fx_result["paths"]["audio"]
        scenario = fx_result["scenario"]
        validation = fx_result["validation"]
        event = next(e for e in validation["events"]
                     if e["event_id"] == contract_id)

        # fixture.create_fixture ALWAYS writes the clip as caller on channel 0,
        # agent on channel 1 -- regardless of the ORIGINAL --caller-channel /
        # --agent-channel the raw input used -- so every read of the clipped
        # bundle audio below uses the fixed 0/1 mapping, never the caller's
        # original (possibly different) channel indices.
        cfg = ScoreConfig()
        # K6: "contract" mode applies the STRICTER contract/CI crosstalk bar (a
        # false-confident pass here is a CI regression that ships), and honors an
        # explicit human channel-map confirmation (--confirm-channels).
        trust_rep = _trust.trust_report(
            clipped_audio, caller_channel=0, agent_channel=1, cfg=cfg,
            mode=_trust.VERDICT_MODE_CONTRACT,
            channel_map_confirmed=confirm_channels,
        )
        dump = dump_frames_for_input(
            stereo=clipped_audio, caller_channel=0, agent_channel=1,
            onset_sec=None, cfg=cfg,
        )
        model = _report._event_model(event, dump["frames"], dump["hop_sec"], cfg)

        audio_dest = os.path.join(tmp_dir, _REL["audio"])
        _mkparents(audio_dest)
        shutil.copyfile(clipped_audio, audio_dest)
        # Hash the BUNDLED clip (what actually ships as audio/event.wav), not just
        # the pre-clip source: verify recomputes these and refuses a swapped clip.
        bundle_audio_sha = _sha256_file(audio_dest)
        bundle_pcm_sha = _decoded_pcm_sha256(audio_dest)

        # K5: carry the label-record `fixture.create_fixture` minted (bound to
        # the fixture's clipped audio, which is byte-identical to the audio
        # just bundled above) forward into the CONTRACT itself, instead of
        # letting it evaporate with the throwaway fx_root -- this is the
        # signed proof a human authored the label, not just a name string.
        # Re-verify locally rather than trusting the mint call blindly: the
        # SAME honest tiers `manifest.build_manifest` derives (human /
        # human-shared / asserted / invalid), never a fabricated "human".
        label_record = scenario.get("label_record")
        if label_record is not None:
            _label_verification = _labelrecord.verify_label_record_local(
                label_record, event_pcm_sha256=bundle_pcm_sha,
            )
            label_authority = (_label_verification["authority"]
                               if _label_verification.get("ok") else "invalid")
        else:
            # No signing key configured anywhere: the label stays an explicit,
            # operator-asserted expectation -- never silently upgraded.
            label_authority = "asserted"
        _write_json(os.path.join(tmp_dir, _REL["evidence"]["label_record"]),
                   label_record)

        _write_text(os.path.join(tmp_dir, _REL["evidence"]["frames"]),
                   _frame_lines(dump))
        _write_text(os.path.join(tmp_dir, _REL["evidence"]["timeline"]),
                   _render_timeline_html(model, contract_id=contract_id,
                                          expect=expect))
        _write_json(os.path.join(tmp_dir, _REL["evidence"]["trust"]), trust_rep)

        initial_report_path = os.path.join(tmp_dir, _REL["reports"]["initial"])
        _mkparents(initial_report_path)
        _report.write_report(
            initial_report_path, fmt="html",
            stereo=clipped_audio, onset_sec=scenario["caller_onset_sec"],
            expect=expect, stack=stack, max_talk_over_sec=max_talk_over_sec,
            max_time_to_yield_sec=max_time_to_yield_sec, cfg=cfg,
        )

    duration_sec = scenario.get("duration_sec")
    _v_elig, _v_reason = _channel_verdict_eligible(trust_rep)
    measurement = _measurement_from_event(
        event, verdict_eligible=_v_elig, verdict_ineligible_reason=_v_reason,
    )
    trust_block = _trust_block(trust_rep)
    policy = _base_policy(want_yield, max_talk_over_sec, max_time_to_yield_sec)

    contract = {
        "schema": SCHEMA,
        "id": contract_id,
        "created_at": _now_iso(),
        "created_by": CREATED_BY,
        "kind": "voice-turn-taking-contract",
        "label": {
            "expected_behavior": "yield" if want_yield else "hold",
            "label_source": "human",
            "rationale": rationale,
        },
        # K5: the REAL evidence a human reviewed this exact recording and
        # decided --expect -- a signed label-record (or None, honestly, when
        # no signing key is configured anywhere), plus the same human/
        # human-shared/asserted/invalid tier `manifest.build_manifest` derives
        # elsewhere. `label.label_source` above stays frozen to "human" (a
        # human ran this command); THIS is the cryptographic proof, never
        # conflated with that fixed string.
        "label_record": label_record,
        "label_authority": label_authority,
        "identity": {"reviewer": resolved_reviewer},
        "source": {
            "stack": stack or "generic",
            "recording_type": recording_type,
            "channels": 2,
            "source_audio_sha256": source_sha,
            "bundle_audio_sha256": bundle_audio_sha,
            "bundle_pcm_sha256": bundle_pcm_sha,
            "candidate_ref": candidate_ref if include_identifiers else
                            (candidate_ref and "(redacted; --include-identifiers to show)"),
            "candidate_kind": candidate_kind,
        },
        "event": {
            "onset_sec": scenario["caller_onset_sec"],
            "source_onset_sec": resolved_onset,
            "pre_sec": None if no_clip else pre_sec,
            "post_sec": None if no_clip else post_sec,
            "clipped": not no_clip,
        },
        "measurement": measurement,
        "trust": trust_block,
        "policy": policy,
        "fix": event.get("fix"),
        "replay": {
            "command": (
                f"hotato run --stereo {_REL['audio']} --expect {expect}"
                + (f" --stack {stack}" if stack else "")
            ),
            "ci_command": "hotato contract verify . --junit junit.xml",
        },
        "bundle": {"paths": _bundle_paths_rel()},
    }
    # Bind the contract's semantic identity (schema + label + policy +
    # source-audio hash + scorer version/config + timestamp) into an embedded
    # canonical digest, and write a detached attestation.json into the bundle.
    # A later verify recomputes this digest: a body edited after creation (e.g.
    # a loosened policy re-packed with a fresh manifest) no longer matches.
    _attest.embed_attestation(contract, bundle_dir=tmp_dir)
    _finish_bundle(
        tmp_dir, contract, want_yield=want_yield, expect=expect,
        stack=stack, category=scenario.get("category"),
        candidate_ref=candidate_ref, candidate_kind=candidate_kind,
        source_name=source_name, duration_sec=duration_sec,
        source_sha=source_sha, rationale=rationale,
        include_identifiers=include_identifiers,
    )
    return contract


def _create_diarized_mono(
    tmp_dir, *, mono, diarize, diarizer, caller_speaker, agent_speaker,
    egress_opt_in, contract_id, want_yield, expect, onset_sec, stack,
    max_talk_over_sec, max_time_to_yield_sec, rationale, include_identifiers,
) -> dict:
    # ``run_single`` itself raises the clean, actionable ValueError when
    # ``--mono`` is given without ``--diarize`` (mirroring ``hotato run``);
    # no separate mono-rejection needed here.
    env = run_single(
        mono=mono, diarize=diarize, diarizer=diarizer,
        onset_sec=onset_sec, expect=expect, stack=stack,
        max_talk_over_sec=max_talk_over_sec,
        max_time_to_yield_sec=max_time_to_yield_sec,
        caller_speaker=caller_speaker, agent_speaker=agent_speaker,
        egress_opt_in=egress_opt_in,
    )
    event = env["events"][0]
    if event.get("scorable") is False:
        raise ValueError(
            "the recording is not scorable, so no contract was created. "
            f"Reason: {event.get('not_scorable_reason')}"
        )
    trust_rep = _trust.trust_report(mono, diarize=True, diarizer=diarizer,
                                    egress_opt_in=egress_opt_in,
                                    mode=_trust.VERDICT_MODE_CONTRACT)

    audio_dest = os.path.join(tmp_dir, _REL["audio"])
    _mkparents(audio_dest)
    shutil.copyfile(mono, audio_dest)
    bundle_audio_sha = _sha256_file(audio_dest)
    bundle_pcm_sha = _decoded_pcm_sha256(audio_dest)

    reason = (
        "frame-level evidence is not produced for the diarized-mono path in "
        "this release; see evidence/trust.json for the separation confidence "
        "tier instead"
    )
    _write_text(os.path.join(tmp_dir, _REL["evidence"]["frames"]),
               _mono_frame_lines(reason))
    _write_text(os.path.join(tmp_dir, _REL["evidence"]["timeline"]),
               _render_mono_timeline_placeholder(contract_id, trust_rep))
    _write_json(os.path.join(tmp_dir, _REL["evidence"]["trust"]), trust_rep)
    _write_text(
        os.path.join(tmp_dir, _REL["reports"]["initial"]),
        _render_mono_timeline_placeholder(contract_id, trust_rep),
    )

    source_sha = _sha256_file(mono)
    source_name = os.path.basename(mono)
    diarization = event.get("diarization")
    indicative_only = bool(event.get("indicative_only"))
    _v_elig, _v_reason = _channel_verdict_eligible(trust_rep)
    measurement = _measurement_from_event(
        event, indicative_only=indicative_only, diarization=diarization,
        verdict_eligible=_v_elig, verdict_ineligible_reason=_v_reason,
    )
    trust_block = _trust_block(trust_rep)
    policy = _base_policy(want_yield, max_talk_over_sec, max_time_to_yield_sec)
    m = event.get("measurements") or {}

    contract = {
        "schema": SCHEMA,
        "id": contract_id,
        "created_at": _now_iso(),
        "created_by": CREATED_BY,
        "kind": "voice-turn-taking-contract",
        "label": {
            "expected_behavior": "yield" if want_yield else "hold",
            "label_source": "human",
            "rationale": rationale,
        },
        "source": {
            "stack": stack or "generic",
            "recording_type": "diarized-mono",
            "channels": 1,
            "source_audio_sha256": source_sha,
            "bundle_audio_sha256": bundle_audio_sha,
            "bundle_pcm_sha256": bundle_pcm_sha,
            "candidate_ref": None,
            "candidate_kind": None,
        },
        "event": {
            "onset_sec": m.get("caller_onset_sec", onset_sec),
            "source_onset_sec": onset_sec,
            "pre_sec": None,
            "post_sec": None,
            "clipped": False,
        },
        "measurement": measurement,
        "trust": trust_block,
        "policy": policy,
        "fix": event.get("fix"),
        "replay": {
            "command": (
                f"hotato run --mono {_REL['audio']} --diarize "
                f"--diarizer {diarizer} --expect {expect}"
                + (f" --stack {stack}" if stack else "")
            ),
            "ci_command": "hotato contract verify . --junit junit.xml",
        },
        "bundle": {"paths": _bundle_paths_rel()},
    }
    # Same canonical-digest binding as the fixture path (see the note there):
    # the diarized-mono contract is attested identically so a re-packed,
    # policy-loosened mono bundle is caught by the digest-mismatch check.
    _attest.embed_attestation(contract, bundle_dir=tmp_dir)
    _finish_bundle(
        tmp_dir, contract, want_yield=want_yield, expect=expect,
        stack=stack, category=("should_yield" if want_yield else "should_not_yield"),
        candidate_ref=None, candidate_kind=None, source_name=source_name,
        duration_sec=None, source_sha=source_sha, rationale=rationale,
        include_identifiers=include_identifiers,
    )
    return contract


def _bundle_paths_rel() -> dict:
    return json.loads(json.dumps(_REL))  # deep copy


def _finish_bundle(tmp_dir, contract, *, want_yield, expect, stack, category,
                   candidate_ref, candidate_kind, source_name, duration_sec,
                   source_sha, rationale, include_identifiers) -> None:
    """Write the remaining bundle files that do not depend on which input
    path produced the contract: policy, source metadata, provenance, CI
    scaffold, and the shareable card (rendered from the contract dict itself,
    so the card and the contract can never disagree)."""
    contract_id = contract["id"]

    _write_text(os.path.join(tmp_dir, _REL["policy"]),
               _policy_yaml_text(contract_id, want_yield))

    call_meta = _call_metadata(
        stack=stack, expect=expect, category=category,
        recording_type=contract["source"]["recording_type"],
        channels=contract["source"]["channels"], duration_sec=duration_sec,
        candidate_ref=candidate_ref, candidate_kind=candidate_kind,
        source_name=source_name, include_identifiers=include_identifiers,
    )
    _write_json(os.path.join(tmp_dir, _REL["source"]["call_metadata"]), call_meta)
    _write_json(os.path.join(tmp_dir, _REL["source"]["stack_config_snapshot"]),
               _stack_config_snapshot(stack))

    prov = _provenance(
        contract_id=contract_id, created_by=contract["created_by"],
        candidate_ref=candidate_ref if include_identifiers else None,
        source_sha256=source_sha, rationale=rationale,
    )
    _write_json(os.path.join(tmp_dir, _REL["provenance"]), prov)

    _write_text(os.path.join(tmp_dir, _REL["ci"]["github_action"]),
               _github_action_yaml())

    result = {
        "id": contract_id, "dir": os.path.dirname(tmp_dir) or ".",
        "passed": bool(contract["measurement"].get("passed")),
        "scorable": bool(contract["measurement"].get("scorable")),
        "not_scorable_reason": contract["measurement"].get("not_scorable_reason"),
    }
    _write_text(os.path.join(tmp_dir, _REL["ci"]["junit"]),
               render_verify_junit({"results": [result]}, suite_name="hotato contract create"))

    os.makedirs(os.path.join(tmp_dir, _REL["traces_dir"]), exist_ok=True)
    _write_text(
        os.path.join(tmp_dir, _REL["traces_dir"], ".gitkeep"),
        "# populated by `hotato trace attach` (see docs/TRACE.md). Empty "
        "until a voice trace is attached.\n",
    )

    _write_text(os.path.join(tmp_dir, _REL["reports"]["after"]),
               _after_report_placeholder(contract_id))

    card_svg = _card._render_contract(contract, include_identifiers=include_identifiers)
    _write_text(os.path.join(tmp_dir, _REL["evidence"]["card"]), card_svg)

    _write_json(os.path.join(tmp_dir, _REL["contract"]), contract)


def render_create_text(result: dict) -> str:
    c = result["contract"]
    m = c["measurement"]
    lines = [
        f"created hotato contract: {result['id']}",
        f"  dir:      {result['dir']}",
        f"  expect:   {c['label']['expected_behavior']}",
        f"  scorable: {'yes' if m['scorable'] else 'NOT SCORABLE'}",
    ]
    label_authority = c.get("label_authority")
    if label_authority is not None:
        reviewer = (c.get("identity") or {}).get("reviewer")
        if label_authority in ("human", "human-shared"):
            lines.append(
                f"  label:    {label_authority} (signed label-record bound "
                f"to this exact audio; reviewer={reviewer})"
            )
        elif label_authority == "asserted":
            lines.append(
                f"  label:    asserted (reviewer={reviewer}; no signing key "
                "configured, so this is an operator-asserted expectation, "
                "not a cryptographically signed human label)"
            )
        else:
            lines.append(
                f"  label:    {label_authority} (a label-record was minted "
                "but does not locally verify; treat this label as unproven)"
            )
    if m["scorable"]:
        lines.append(f"  passed:   {m['passed']}")
        lines.append(
            f"  measured: did_yield={m['did_yield']} "
            f"seconds_to_yield={m['seconds_to_yield']} "
            f"talk_over={m['talk_over_sec']}"
        )
        if m["indicative_only"]:
            lines.append("  note:     indicative only (diarized-mono, below "
                         "the confidence bar) -- never treated as a "
                         "confident dual-channel measurement")
        if not m.get("verdict_eligible", True):
            lines.append(
                f"  [!] verdict withheld: {m.get('verdict_ineligible_reason')}"
            )
            lines.append(
                "      `contract verify` will REFUSE this contract until the "
                "channel mapping is confirmed (--confirm-channels) or "
                "authenticated provider metadata is supplied"
            )
    else:
        lines.append(f"  reason:   {m['not_scorable_reason']}")
    lines.append("next:")
    lines.append(f"  {result['next']}")
    return "\n".join(lines)


def create_result_json(result: dict) -> dict:
    return {
        "tool": "hotato",
        "kind": "contract",
        "schema_version": "1",
        "id": result["id"],
        "dir": result["dir"],
        "contract": result["contract"],
        "next": result["next"],
    }


# --- verify --------------------------------------------------------------

def discover_bundles(path: str) -> list:
    """Every ``<id>.hotato`` bundle under ``path``: ``path`` itself if it IS a
    bundle (has ``contract.json``), else every immediate ``*.hotato``
    subdirectory, sorted."""
    if not os.path.isdir(path):
        raise ValueError(f"{path!r} is not a directory")
    if os.path.isfile(os.path.join(path, "contract.json")):
        return [path]
    out = []
    for name in sorted(os.listdir(path)):
        p = os.path.join(path, name)
        if os.path.isdir(p) and os.path.isfile(os.path.join(p, "contract.json")):
            out.append(p)
    return out


def _load_contract(bundle_dir: str) -> dict:
    cpath = os.path.join(bundle_dir, "contract.json")
    try:
        with _open_regular(cpath, "r", encoding="utf-8") as fh:
            contract = json.load(fh)
    except (OSError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"{cpath!r} is not a readable hotato contract: {exc}") from exc
    if contract.get("schema") != SCHEMA:
        raise ValueError(
            f"{cpath!r} is not a {SCHEMA} contract (schema="
            f"{contract.get('schema')!r})"
        )
    return contract


def _require_shape(contract: dict, cpath: str, *required: tuple) -> None:
    """Shape-check nested fields a caller is ABOUT to dereference directly,
    BEFORE any of that access happens: a contract bundle is untrusted
    third-party input (docs/SUBMITTING.md), and valid JSON with the right
    schema string but a missing/mistyped nested field must fail closed with a
    clean ``ValueError`` (CLI exit 2, structured MCP error) rather than an
    uncaught ``KeyError``/``TypeError`` breaking a caller's documented "never
    an exception" contract. Scoped to just the fields a caller actually
    needs (not enforced blanket in :func:`_load_contract`) so a minimal, but
    otherwise valid, contract -- e.g. a not-scorable one ``inspect``/
    ``explain`` only reads the label/measurement off of -- is not refused for
    fields nothing in that path reads."""
    for keys, expected_type in required:
        node = contract
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                raise ValueError(
                    f"{cpath!r} is missing required field "
                    f"{'.'.join(keys)!r}; not a valid hotato contract"
                )
            node = node[key]
        if not isinstance(node, expected_type):
            raise ValueError(
                f"{cpath!r} is missing required field "
                f"{'.'.join(keys)!r}; not a valid hotato contract"
            )


def _bundle_trace_spans(bundle_dir: str) -> Optional[list]:
    """Spans for embedded-assertion evaluation: the bundle's OWN attached
    trace (``traces/voice_trace.jsonl``, written by ``hotato trace attach``),
    if one was ever attached. A freshly created bundle's ``traces/`` holds
    only the ``.gitkeep`` placeholder (see :func:`_finish_bundle`), so this
    returns ``None`` for it -- a ``tool_call`` assertion evaluated against
    ``None`` spans reports ``INCONCLUSIVE`` (missing input), never a
    fabricated PASS/FAIL. Never re-derives spans from anywhere else; this is
    the SAME trace ``contract inspect``/``explain`` would show."""
    path = os.path.join(bundle_dir, _REL["traces_dir"], "voice_trace.jsonl")
    if not os.path.isfile(path):
        return None
    # Lazy import: `assert_` -> `trace` -> `contract` -> `report` ->
    # `assert_` is a real module-cycle at IMPORT time (report.py imports
    # `SCHEMA` off `assert_` by attribute), so `contract` must never import
    # `assert_` at module scope. By the time this function actually runs (a
    # `contract verify` call), every module involved has long finished
    # loading, so the deferred import here is always safe.
    from . import assert_ as _assert_mod
    return _assert_mod.load_spans_file(path)


def _run_embedded_assertions(contract: dict, bundle_dir: str, *,
                             event: dict,
                             transcript_path: Optional[str]) -> Optional[dict]:
    """Run this contract's own optional ``assertions`` block (schema/
    contract.v1.json) through the SAME ``assert.v1`` engine ``hotato assert``
    uses, and return its envelope. Returns ``None`` when the contract carries
    no ``assertions`` block at all -- a contract that never asked for
    assertions never gets a fabricated envelope. This is an ADDITIONAL,
    separately reported dimension: it never touches the timing pass/fail
    computed elsewhere in :func:`_verify_one`.

    Context: ``spans`` come from the bundle's own attached trace (see
    :func:`_bundle_trace_spans`); ``timing`` is this exact re-verify run's
    freshly re-scored event (so an ``outcome`` assertion's ``field_present``
    reads the CURRENT re-score, not a stale one); ``transcript`` comes from
    ``--transcript FILE`` if the caller passed one to ``contract verify``
    (the bundle format itself carries no stored transcript) -- reusing
    :mod:`hotato.assert_`'s own file loader, so a missing/malformed
    ``--transcript`` file is refused the same way it is for ``hotato
    assert`` (a clean ``ValueError``, exit 2), never silently skipped.
    Absent a transcript, any ``phrase``/``pii``/``policy`` assertion (or an
    ``outcome`` predicate that needs one) reports ``INCONCLUSIVE``, never a
    guessed result -- matching :mod:`hotato.assert_`'s own honesty
    invariant."""
    doc = contract.get("assertions")
    if doc is None:
        return None
    from . import assert_ as _assert_mod  # see _bundle_trace_spans: deferred
    ctx = _assert_mod.build_context(
        transcript_path=transcript_path,
        spans=_bundle_trace_spans(bundle_dir),
        timing=event,
    )
    return _assert_mod.run_assertions(doc, ctx)


def _verify_one(bundle_dir: str, *, transcript_path: Optional[str] = None) -> dict:
    contract = _load_contract(bundle_dir)
    cpath = os.path.join(bundle_dir, "contract.json")
    _require_shape(
        contract, cpath,
        (("bundle", "paths", "audio"), str),
        (("source", "recording_type"), str),
        (("label", "expected_behavior"), str),
        (("policy", "pass_conditions"), dict),
        (("event",), dict),
    )
    audio_rel = contract["bundle"]["paths"]["audio"]
    # audio_rel is untrusted (a contract bundle is third-party input to
    # `contract verify`: a hand-built bundle, or one unpacked from a
    # `.hotato` archive before its OWN authenticity check even runs). An
    # absolute path or a `..` escape here must never be opened: `os.path.join`
    # silently DISCARDS `bundle_dir` when the second argument is absolute, so
    # an unchecked join is a path-traversal / arbitrary-file-read bug, not
    # just a corruption case. Reuse the same member-name safety check
    # `contract pack/unpack` already applies to archive paths, then verify
    # the resolved path is still CONTAINED inside bundle_dir (the same
    # realpath/commonpath containment `_load_bundled_scenarios` uses in
    # core.py) as defense in depth against a symlink planted inside the
    # bundle.
    try:
        audio_parts = _safe_member_parts(audio_rel)
    except ValueError as exc:
        raise ValueError(
            f"{bundle_dir!r}: contract.json audio path {audio_rel!r} is "
            f"unsafe ({exc}); refusing to read it"
        ) from exc
    bundle_real = os.path.realpath(bundle_dir)
    audio_path = os.path.realpath(os.path.join(bundle_dir, *audio_parts))
    if os.path.commonpath([bundle_real, audio_path]) != bundle_real:
        raise ValueError(
            f"{bundle_dir!r}: contract.json audio path {audio_rel!r} "
            "resolves outside the bundle directory; refusing to read it"
        )
    if not os.path.isfile(audio_path):
        raise ValueError(
            f"{bundle_dir!r}: contract.json points at missing audio "
            f"{audio_rel!r}"
        )
    rec_type = contract["source"]["recording_type"]
    expect = contract["label"]["expected_behavior"]
    pol = contract["policy"]["pass_conditions"]
    stack = contract["source"].get("stack")
    onset_sec = contract["event"].get("onset_sec")

    if rec_type == "diarized-mono":
        dz = contract["measurement"].get("diarization") or {}
        speaker_map = dz.get("speaker_map") or {}
        env = run_single(
            mono=audio_path, diarize=True, diarizer=dz.get("backend", "pyannote"),
            caller_speaker=speaker_map.get("caller"),
            agent_speaker=speaker_map.get("agent"),
            onset_sec=onset_sec, expect=expect, stack=stack,
            max_talk_over_sec=pol.get("max_talk_over_sec"),
            max_time_to_yield_sec=pol.get("max_time_to_yield_sec"),
        )
        # No caller/agent channel-swap concept on the diarized-mono path (its own
        # separation-confidence tier already carries the honest confidence
        # signal): verdict eligibility mirrors the engine's own scorable gate.
        verdict_eligible = env["events"][0].get("scorable") is not False
        verdict_ineligible_reason = None
    else:
        # A caller+agent-originated contract's bundle audio is ALWAYS one
        # two-channel WAV (fixture.create_fixture writes it that way), so
        # both stereo-derived and caller+agent-derived contracts re-score
        # the same bundle file the same way here.
        env = run_single(
            stereo=audio_path, onset_sec=onset_sec, expect=expect, stack=stack,
            max_talk_over_sec=pol.get("max_talk_over_sec"),
            max_time_to_yield_sec=pol.get("max_time_to_yield_sec"),
        )
        # K6: re-derive channel-mapping verdict eligibility from the bundle
        # audio ON DISK NOW, at the STRICTER contract/CI threshold, honoring
        # the SAME human confirmation recorded at creation (bound into the
        # attestation digest) -- never a fresh, unverified override. A legacy
        # bundle created before this field existed is treated as unconfirmed
        # (the honest default; never silently auto-confirmed).
        confirmed = bool((contract.get("trust") or {}).get("channel_mapping_confirmed"))
        verdict_trust_rep = _trust.trust_report(
            audio_path, caller_channel=0, agent_channel=1,
            mode=_trust.VERDICT_MODE_CONTRACT, channel_map_confirmed=confirmed,
        )
        verdict_eligible, verdict_ineligible_reason = _channel_verdict_eligible(
            verdict_trust_rep
        )
    event = env["events"][0]
    scorable = event.get("scorable") is not False
    verdict_ok = scorable and verdict_eligible
    v = event.get("verdict") or {}
    passed = verdict_ok and bool(v.get("passed"))
    # MEDIA BINDING: the signed subject records the bundled clip's raw + decoded
    # PCM identity. Recompute them from the audio/event.wav ON DISK NOW and refuse
    # if either differs from what was signed -- a bundle whose audio was replaced
    # after creation (fail -> pass by swapping the wav) is a tampered bundle even
    # though contract.json (and thus its signature) is untouched. A legacy bundle
    # created before media binding has no stored bundle hash; it simply cannot be
    # media-verified (its authenticity is decided by the digest axis alone).
    src = contract.get("source") or {}
    want_raw = src.get("bundle_audio_sha256")
    want_pcm = src.get("bundle_pcm_sha256")
    media_tampered = False
    media_reason = None
    if want_raw or want_pcm:
        got_raw = _sha256_file(audio_path)
        got_pcm = _decoded_pcm_sha256(audio_path)
        if (want_raw and got_raw != want_raw) or (want_pcm and got_pcm != want_pcm):
            media_tampered = True
            media_reason = ("the bundled audio does not match the signed source: the "
                            "recording in audio/event.wav was replaced after the "
                            "contract was created")
    # Authenticity is an ADDITIONAL axis, orthogonal to the pass/fail re-scoring
    # above: recompute the canonical digest and compare to the one embedded at
    # creation. A body edited after creation (a loosened policy re-packed with a
    # fresh, self-consistent manifest) is caught here as "tampered"; a bundle
    # that matches but carries no verifying signature is "unsigned, internally
    # consistent evidence", NEVER "authenticated".
    auth = _attest.assess_contract(contract, bundle_dir=bundle_dir)
    # A SEPARATE reported dimension from the timing pass/fail above: this
    # contract's own optional embedded `assertions` block (schema/
    # contract.v1.json), re-evaluated through the exact `hotato assert`
    # engine and reported as its own assert.v1 envelope (or `None` when the
    # contract carries no assertions block). Never blended into `passed`.
    assertions_env = _run_embedded_assertions(
        contract, bundle_dir, event=event, transcript_path=transcript_path,
    )
    if media_tampered:
        # A swapped recording cannot be certified: force tampered + non-passing,
        # so a fail->pass audio swap can never report an authenticated pass.
        return {
            "id": contract.get("id") or os.path.basename(bundle_dir),
            "dir": bundle_dir,
            "expect": expect,
            "passed": False,
            "scorable": scorable,
            "verdict_eligible": verdict_eligible,
            "verdict_ineligible_reason": verdict_ineligible_reason,
            "not_scorable_reason": event.get("not_scorable_reason"),
            "measurement": {"did_yield": None, "seconds_to_yield": None,
                            "talk_over_sec": None},
            "authenticity": "tampered",
            "authenticated": False,
            "authenticity_reason": media_reason,
            "assertions": assertions_env,
        }
    return {
        "id": contract.get("id") or os.path.basename(bundle_dir),
        "dir": bundle_dir,
        "expect": expect,
        "passed": passed,
        "scorable": scorable,
        # K6: distinct from `scorable` -- False here REFUSES the contract (a
        # suspected channel swap or contract-mode crosstalk/leakage), never a
        # silent pass, even though the engine itself found the audio scorable.
        "verdict_eligible": verdict_eligible,
        "verdict_ineligible_reason": verdict_ineligible_reason,
        "not_scorable_reason": event.get("not_scorable_reason"),
        "measurement": {
            "did_yield": v.get("did_yield") if verdict_ok else None,
            "seconds_to_yield": v.get("seconds_to_yield") if verdict_ok else None,
            "talk_over_sec": v.get("talk_over_sec") if verdict_ok else None,
        },
        "authenticity": auth["authenticity"],
        "authenticated": auth["authenticated"],
        "authenticity_reason": auth["reason"],
        "assertions": assertions_env,
    }


def verify_contracts(path: str, *, transcript_path: Optional[str] = None) -> dict:
    """Re-score every contract's bundled audio against its recorded policy
    and return the batch proof dict. ``path`` is a single bundle dir or a
    parent directory of ``*.hotato`` bundles. ``transcript_path``, if given,
    is a transcript JSON file (:func:`hotato.assert_.load_transcript_file`'s
    shape) used as context for every contract's embedded ``assertions``
    block, if any -- the bundle format itself carries no stored transcript
    (see :func:`_run_embedded_assertions`). Raises ``ValueError`` (CLI exit
    2) for a missing/corrupt contract, a directory with no contracts, an
    unreadable ``--transcript`` file, or a malformed embedded ``assertions``
    block; otherwise always returns (a regression, or an assertion FAIL, is a
    per-contract result field, never an exception)."""
    bundle_dirs = discover_bundles(path)
    if not bundle_dirs:
        raise ValueError(
            f"{path!r} has no hotato contracts (looked for contract.json "
            "directly, or *.hotato/contract.json inside it)"
        )
    results = [_verify_one(bd, transcript_path=transcript_path) for bd in bundle_dirs]
    # A regressed score OR a tampered contract (edited after creation) fails the
    # batch. The per-result "passed" (the re-scoring axis) is left untouched;
    # tampering is an additional reason to fail, never a rewrite of the score.
    # This axis stays exactly as it was before embedded assertions existed --
    # see `assertions_failed` below for the separate assertions axis.
    failed = [r for r in results
              if not r["passed"] or r.get("authenticity") == "tampered"]
    tampered = [r for r in results if r.get("authenticity") == "tampered"]
    # K6: a REFUSED contract (scorable, but verdict_eligible is False -- a
    # suspected channel swap or contract-mode crosstalk/leakage) is counted
    # among "failed" above (passed is False), but broken out separately here so
    # a CI dashboard can tell "the agent regressed" apart from "the channel
    # mapping needs confirming" -- never a silent pass either way.
    refused = [r for r in results
               if r.get("scorable") and not r.get("verdict_eligible", True)]
    # A SEPARATE reported dimension, never blended into `summary`/`failed`
    # above: contracts carrying an embedded `assertions` block whose
    # deterministic assert.v1 evaluation had at least one FAIL. A contract
    # with no `assertions` block at all is never counted here.
    assertions_failed = [
        r for r in results
        if r.get("assertions") is not None and r["assertions"].get("exit_code") != 0
    ]
    return {
        "tool": "hotato",
        "kind": "contract-verify",
        "schema_version": "1",
        "offline": True,
        "dir": path,
        "count": len(results),
        "results": results,
        "summary": {"passed": len(results) - len(failed), "failed": len(failed)},
        "tampered": len(tampered),
        "refused": len(refused),
        "assertions_failed": len(assertions_failed),
        # A deterministic assertion FAIL contributes to the batch's nonzero
        # exit exactly like a timing regression, even though the two are
        # reported as the SEPARATE dimensions above (summary vs.
        # assertions_failed) -- never merged into one score.
        "exit_code": 1 if (failed or assertions_failed) else 0,
    }


def _assertions_text_line(r: dict) -> Optional[str]:
    """One extra report line for a contract's embedded-assertions result
    (schema/assert.v1.json), kept SEPARATE from the timing pass/fail/refused/
    not-scorable line above it -- never blended into that verdict. Returns
    ``None`` when the contract carries no ``assertions`` block at all."""
    env = r.get("assertions")
    if env is None:
        return None
    d = env["summary"]["deterministic"]
    mark = "FAIL" if env["exit_code"] else "PASS"
    line = (
        f"    [ASSERTIONS {mark}] {r['id']}: {d['pass']} pass, {d['fail']} fail, "
        f"{d['inconclusive']} inconclusive (deterministic; "
        f"{env['summary']['judge']['pass'] + env['summary']['judge']['fail']} judge)"
    )
    if env["exit_code"]:
        failed_ids = [x["id"] for x in env["results"] if x["status"] == "FAIL"]
        line += f" -- failed: {failed_ids}"
    return line


def render_verify_text(v: dict) -> str:
    lines = [
        f"hotato contract verify: {v['dir']} ({v['count']} contract"
        f"{'' if v['count'] == 1 else 's'})",
    ]
    for r in v["results"]:
        auth = r.get("authenticity", "unattested")
        if not r["scorable"]:
            lines.append(
                f"  [NOT SCORABLE] {r['id']}: {r['not_scorable_reason']} "
                f"| authenticity: {auth}"
            )
        elif not r.get("verdict_eligible", True):
            # K6: REFUSED, distinct from FAIL -- the engine found the audio
            # scorable but the channel mapping is unconfirmed (suspected swap or
            # contract-mode crosstalk/leakage), so no verdict is invented.
            lines.append(
                f"  [REFUSED] {r['id']} (expect {r['expect']}): "
                f"{r.get('verdict_ineligible_reason')} | authenticity: {auth}"
            )
        else:
            mark = "PASS" if r["passed"] else "FAIL"
            m = r["measurement"]
            lines.append(
                f"  [{mark}] {r['id']} (expect {r['expect']}): "
                f"did_yield={m['did_yield']} "
                f"seconds_to_yield={m['seconds_to_yield']} "
                f"talk_over={m['talk_over_sec']} "
                f"| authenticity: {auth}"
            )
            if auth == "tampered":
                lines.append(
                    f"    [TAMPERED] {r['id']}: {r.get('authenticity_reason', '')}"
                )
        # The assertions dimension is reported for EVERY contract that carries
        # one, regardless of the timing verdict above (NOT SCORABLE/REFUSED/
        # PASS/FAIL) -- it is a separate axis, never gated on the timing one.
        aline = _assertions_text_line(r)
        if aline is not None:
            lines.append(aline)
    s = v["summary"]
    lines.append(f"  {s['passed']}/{v['count']} contracts pass; exit_code={v['exit_code']}")
    if v.get("tampered"):
        lines.append(
            f"  {v['tampered']} contract(s) TAMPERED (canonical digest "
            "mismatch: edited after creation)"
        )
    if v.get("refused"):
        lines.append(
            f"  {v['refused']} contract(s) REFUSED (channel mapping "
            "unconfirmed: suspected swap/crosstalk)"
        )
    if v.get("assertions_failed"):
        lines.append(
            f"  {v['assertions_failed']} contract(s) have a FAILING embedded "
            "assertion (separate from the timing verdict above)"
        )
    lines.append(f"  {_STORED_EVIDENCE_CAVEAT}")
    return "\n".join(lines)


def verify_result_json(v: dict) -> dict:
    return v


_JUNIT_ESCAPE = str.maketrans({"&": "&amp;", "<": "&lt;", ">": "&gt;",
                               '"': "&quot;"})


def _jesc(s) -> str:
    return str(s if s is not None else "").translate(_JUNIT_ESCAPE)


def render_verify_junit(v: dict, *, suite_name: str = "hotato contracts") -> str:
    """JUnit XML for CI: one ``<testcase>`` per contract for the timing
    re-score, a ``<failure>`` child for a regressed or not-scorable one, PLUS
    -- for a contract that carries an embedded ``assertions`` block -- one
    ADDITIONAL, separate ``<testcase>`` (``classname="hotato.contract.
    assertions"``) for its assert.v1 result. The two are always distinct
    ``<testcase>`` elements, never one blended pass/fail, even though both
    count toward this file's ``failures`` total. Consumed by any JUnit-reading
    CI dashboard (the shipped ``ci/github-action.yml`` publishes it as an
    artifact)."""
    results = v["results"]
    failures = sum(1 for r in results if not r["passed"])
    cases = []
    for r in results:
        case = f'  <testcase classname="hotato.contract" name="{_jesc(r["id"])}">'
        if not r["passed"]:
            reason = (r.get("not_scorable_reason")
                      or (r.get("verdict_ineligible_reason")
                          if not r.get("verdict_eligible", True) else None)
                      or "the contract's measured timing no longer meets its "
                         "policy pass_conditions")
            case += (f'\n    <failure message="{_jesc(reason)}">'
                    f'{_jesc(json.dumps(r.get("measurement"), sort_keys=True))}'
                    "</failure>\n  ")
        case += "</testcase>"
        cases.append(case)
        env = r.get("assertions")
        if env is not None:
            acase = (f'  <testcase classname="hotato.contract.assertions" '
                     f'name="{_jesc(r["id"])}">')
            if env["exit_code"]:
                failures += 1
                failed_ids = [x["id"] for x in env["results"] if x["status"] == "FAIL"]
                reason = f"{len(failed_ids)} assertion(s) failed: {failed_ids}"
                acase += (f'\n    <failure message="{_jesc(reason)}">'
                         f'{_jesc(json.dumps(env["summary"], sort_keys=True))}'
                         "</failure>\n  ")
            acase += "</testcase>"
            cases.append(acase)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<testsuite name="{_jesc(suite_name)}" tests="{len(cases)}" '
        f'failures="{failures}">\n' + "\n".join(cases) + "\n</testsuite>\n"
    )


def render_verify_html(v: dict) -> str:
    """A minimal, self-contained HTML rollup, reusing report.py's escape
    helper and warm-charcoal palette so it reads as the same family as every
    other hotato report."""
    esc = _report._esc
    rows = []
    for r in v["results"]:
        if not r["scorable"]:
            mark, color, detail = "NOT SCORABLE", "#b7ab97", r["not_scorable_reason"]
        elif not r.get("verdict_eligible", True):
            mark, color, detail = ("REFUSED", "#e0664f",
                                   r.get("verdict_ineligible_reason"))
        else:
            m = r["measurement"]
            mark = "PASS" if r["passed"] else "FAIL"
            color = "#74c98a" if r["passed"] else "#e0664f"
            detail = (f"did_yield={m['did_yield']} "
                     f"seconds_to_yield={m['seconds_to_yield']} "
                     f"talk_over={m['talk_over_sec']}")
        auth = r.get("authenticity", "unattested")
        acolor = "#e0664f" if auth == "tampered" else (
            "#74c98a" if auth == "authenticated" else "#b7ab97")
        # Assertions: a SEPARATE column, never blended into `mark`/`color`
        # above (the timing verdict). "-" when this contract carries no
        # embedded `assertions` block at all.
        aenv = r.get("assertions")
        if aenv is None:
            amark, acolor2 = "-", "#b7ab97"
        else:
            amark = "FAIL" if aenv["exit_code"] else "PASS"
            acolor2 = "#e0664f" if aenv["exit_code"] else "#74c98a"
        rows.append(
            f'<tr><td class="mono">{esc(r["id"])}</td>'
            f'<td>{esc(r["expect"])}</td>'
            f'<td style="color:{color};font-weight:700">{mark}</td>'
            f'<td class="mono">{esc(detail)}</td>'
            f'<td style="color:{acolor};font-weight:600">{esc(auth)}</td>'
            f'<td style="color:{acolor2};font-weight:600">{esc(amark)}</td></tr>'
        )
    s = v["summary"]
    verdict = "PASSED" if v["exit_code"] == 0 else "FAILED"
    vcolor = "#74c98a" if v["exit_code"] == 0 else "#e0664f"
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>hotato contract verify: {verdict}</title>"
        "<style>body{margin:0;background:#1b1714;color:#f1e8d7;"
        "font:15px system-ui,sans-serif}"
        ".wrap{max-width:900px;margin:0 auto;padding:28px 20px}"
        "table{width:100%;border-collapse:collapse;margin-top:14px}"
        "th,td{text-align:left;padding:7px 10px;border-bottom:"
        "1px solid #3a3128;font-size:13.5px}"
        ".mono{font-family:'SFMono-Regular',Menlo,Consolas,monospace}"
        "</style></head><body><div class=\"wrap\">"
        f'<h1>hotato contract verify <span style="color:{vcolor}">'
        f'{verdict}</span></h1>'
        f'<p>{esc(v["dir"])}: {s["passed"]}/{v["count"]} contracts pass.</p>'
        f'<p style="color:#e8c547;font-weight:600">{esc(_STORED_EVIDENCE_CAVEAT)}</p>'
        '<table><thead><tr><th>id</th><th>expect</th><th>result</th>'
        '<th>measured</th><th>authenticity</th><th>assertions</th></tr></thead><tbody>'
        + "".join(rows)
        + "</tbody></table>"
        f'<p style="color:#b7ab97;font-size:12.5px;margin-top:18px">'
        f"{esc(_NOT_PROVED)} Hotato reports coincidence, not causation."
        "</p></div></body></html>\n"
    )


# --- inspect ---------------------------------------------------------------

def inspect_contract(path: str) -> dict:
    """Load and return one contract's ``contract.json``. ``path`` is a bundle
    dir or a ``contract.json`` file directly."""
    if os.path.isdir(path):
        bundle_dir = path
    else:
        bundle_dir = os.path.dirname(os.path.abspath(path)) or "."
    return _load_contract(bundle_dir)


def render_inspect_text(contract: dict) -> str:
    lines = [
        f"hotato contract: {contract['id']}",
        f"  expect:    {contract['label']['expected_behavior']}",
        f"  stack:     {contract['source']['stack']}",
        f"  recording: {contract['source']['recording_type']} "
        f"({contract['source']['channels']} channel"
        f"{'s' if contract['source']['channels'] != 1 else ''})",
        f"  trust:     {contract['trust']['status']}",
    ]
    m = contract["measurement"]
    if m["scorable"]:
        lines.append(
            f"  measured:  did_yield={m['did_yield']} "
            f"seconds_to_yield={m['seconds_to_yield']} "
            f"talk_over={m['talk_over_sec']} passed={m['passed']}"
        )
        if m["indicative_only"]:
            lines.append("  note:      indicative only (diarized-mono)")
    else:
        lines.append(f"  measured:  NOT SCORABLE ({m['not_scorable_reason']})")
    lines.append(f"  replay:    {contract['replay']['command']}")
    lines.append(f"  ci:        {contract['replay']['ci_command']}")
    return "\n".join(lines)


# --- pack / unpack -----------------------------------------------------

def _iter_bundle_files(bundle_dir: str):
    out = []
    for root, dirs, files in os.walk(bundle_dir):
        dirs.sort()
        # A packed bundle must be self-contained: a symlink anywhere under the
        # bundle would silently archive bytes from OUTSIDE it (a planted link
        # to a secret would ship the secret). Refuse links fail-closed, for
        # directories and files alike (os.walk already declines to descend
        # into linked dirs, which would otherwise just vanish from the pack).
        for name in list(dirs) + list(files):
            cand = os.path.join(root, name)
            if os.path.islink(cand):
                rel = os.path.relpath(cand, bundle_dir).replace(os.sep, "/")
                raise ValueError(
                    f"bundle contains a symlink ({rel!r}); refusing to pack. "
                    "A .hotato bundle must be self-contained: copy the real "
                    "file into the bundle instead of linking to it"
                )
        for fn in sorted(files):
            fp = os.path.join(root, fn)
            rel = os.path.relpath(fp, bundle_dir).replace(os.sep, "/")
            out.append((rel, fp))
    out.sort(key=lambda e: e[0])
    return out


def pack_contract(bundle_dir: str, *, out_path: Optional[str] = None,
                  force: bool = False) -> dict:
    """Pack a ``<id>.hotato`` bundle directory into a single deterministic
    ``.hotato`` archive file, with a sha256 manifest of every member so
    :func:`unpack_contract` can verify the round trip byte for byte.

    Determinism (packing the SAME bundle directory twice, even on different
    machines, produces byte-identical archives) is a deliberate property of
    every value written into each member's ``ZipInfo``, not an accident of
    the platform this happens to run on: member order comes from
    :func:`_iter_bundle_files`, which walks and sorts by relative path;
    ``date_time`` is pinned to a fixed epoch instead of the real mtime;
    ``external_attr`` (file mode) and ``compress_type`` / ``compresslevel``
    are fixed constants; and ``create_system`` -- which ``zipfile`` otherwise
    defaults from ``sys.platform`` (0 on Windows, 3 elsewhere) and would
    silently make a Windows-packed archive differ byte-for-byte from a
    Linux-packed one of the SAME bundle -- is pinned to 3 below."""
    if not os.path.isdir(bundle_dir):
        raise ValueError(f"{bundle_dir!r} is not a directory")
    cpath = os.path.join(bundle_dir, "contract.json")
    if not os.path.isfile(cpath):
        raise ValueError(
            f"{bundle_dir!r} has no contract.json; it is not a hotato "
            "contract bundle"
        )
    norm = os.path.normpath(bundle_dir)
    if out_path is None:
        # NOT bare "<id>.hotato": that is the SAME path as the bundle
        # directory being packed (a file and a directory cannot share one
        # path), so the default archive name is "<id>.hotato.pack" instead.
        base = os.path.basename(norm)
        if not base.endswith(BUNDLE_SUFFIX):
            base += BUNDLE_SUFFIX
        out_path = os.path.join(os.path.dirname(norm) or ".", base + ".pack")
    if os.path.exists(out_path) and not force:
        raise ValueError(f"{out_path!r} already exists; pass --force to overwrite")

    entries = _iter_bundle_files(bundle_dir)
    if not entries:
        raise ValueError(f"{bundle_dir!r} is empty; nothing to pack")
    manifest = {rel: _sha256_file(fp) for rel, fp in entries}
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")

    tmp_out = out_path + ".part"
    try:
        # open-ok: write mode to a temp path this function just created
        with zipfile.ZipFile(tmp_out, "w", zipfile.ZIP_DEFLATED,
                             compresslevel=6) as zf:
            zi = zipfile.ZipInfo(MANIFEST_NAME, date_time=(1980, 1, 1, 0, 0, 0))
            zi.external_attr = 0o644 << 16
            zi.create_system = 3  # pin to Unix; see the determinism note above
            zf.writestr(zi, manifest_bytes)
            for rel, fp in entries:
                zi = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))
                zi.external_attr = 0o644 << 16
                zi.create_system = 3  # pin to Unix; see the determinism note above
                with _open_regular(fp) as fh:
                    zf.writestr(zi, fh.read())
        os.replace(tmp_out, out_path)
    except BaseException:
        try:
            os.remove(tmp_out)
        except OSError:
            pass
        raise
    return {
        "path": out_path, "bundle_dir": bundle_dir, "files": len(entries),
        "manifest": manifest,
    }


def render_pack_text(result: dict) -> str:
    return (
        f"packed {result['bundle_dir']} -> {result['path']} "
        f"({result['files']} files, sha256-manifested)"
    )


def pack_result_json(result: dict) -> dict:
    return {
        "tool": "hotato", "kind": "contract-pack", "schema_version": "1",
        "path": result["path"], "bundle_dir": result["bundle_dir"],
        "files": result["files"],
    }


def _max_unpack_bytes() -> int:
    """Resolve the total-decompressed-bytes cap: ``HOTATO_CONTRACT_MAX_UNPACK_BYTES``
    if set (same override convention as ``HOTATO_ALLOW_MONO`` /
    ``HOTATO_INGEST_ALLOWED_HOSTS`` elsewhere in the codebase), else
    :data:`DEFAULT_MAX_UNPACK_BYTES`."""
    raw = os.environ.get(_MAX_UNPACK_BYTES_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_UNPACK_BYTES
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(
            f"{_MAX_UNPACK_BYTES_ENV}={raw!r} is not an integer number of bytes"
        )
    if value <= 0:
        raise ValueError(f"{_MAX_UNPACK_BYTES_ENV} must be a positive integer")
    return value


def _safe_member_parts(name: str) -> list:
    """Validate one archive member name for extraction safety and return its
    validated forward-slash path parts. Rejects absolute paths, ``..``
    traversal, and empty segments (also catches a trailing-slash directory
    entry, which a real ``.hotato`` pack never contains). Backslashes and
    drive letters (``C:\\...``) are rejected outright rather than
    interpreted, so a Windows-style path is caught the same way regardless of
    the host platform doing the unpacking -- POSIX ``os.path.join`` would
    otherwise treat a backslash as a harmless literal filename character and
    let it through."""
    if not name:
        raise ValueError("archive contains a member with an empty name")
    if "\\" in name:
        raise ValueError(f"unsafe path in archive (backslash path separator): {name!r}")
    if name.startswith("/") or _DRIVE_LETTER_RE.match(name):
        raise ValueError(f"unsafe path in archive (absolute path): {name!r}")
    parts = name.split("/")
    if ".." in parts or "" in parts:
        raise ValueError(f"unsafe path in archive: {name!r}")
    return parts


def _check_member_safety(info: zipfile.ZipInfo) -> None:
    """Reject one archive member outright (fail closed) for any property that
    would make it unsafe to extract, before any of its bytes are
    decompressed: an unsafe path (see :func:`_safe_member_parts`), a symlink
    (its ``external_attr`` upper bits carry the POSIX file mode), or an
    encrypted entry (would otherwise raise an uncaught ``RuntimeError`` deep
    in zipfile, breaking the tool's clean exit-2 contract)."""
    _safe_member_parts(info.filename)
    mode = (info.external_attr >> 16) & 0o170000
    if mode == 0o120000:
        raise ValueError(f"archive member is a symbolic link, which is not allowed: {info.filename!r}")
    if info.flag_bits & 0x1:
        raise ValueError(f"archive member is encrypted, which is not supported: {info.filename!r}")


def _check_ratio_bomb(info: zipfile.ZipInfo) -> None:
    """Reject a member whose declared compression ratio is far beyond
    anything a real bundle member (audio/json/html/svg) produces -- a
    single small compressed member designed to expand enormously. This is a
    fast, pre-decompression heuristic on the archive's own metadata; the
    total-bytes cap enforced during actual extraction (see
    :func:`unpack_contract`) is the authoritative defense and does not trust
    this metadata."""
    if info.compress_size <= 0:
        if info.file_size > 0:
            raise ValueError(
                f"archive member {info.filename!r} claims decompressed "
                "content from zero compressed bytes; refusing to unpack"
            )
        return
    if info.file_size < _RATIO_BOMB_MIN_DECLARED_BYTES:
        return
    ratio = info.file_size / info.compress_size
    if ratio > _RATIO_BOMB_MAX_RATIO:
        raise ValueError(
            f"archive member {info.filename!r} has a {ratio:.0f}:1 "
            "compression ratio, far beyond anything a real bundle member "
            "produces; refusing to unpack a possible zip bomb"
        )


def _read_member_capped(zf: zipfile.ZipFile, name: str, max_bytes: int) -> bytes:
    """Read one archive member fully into memory, capping the ACTUAL
    decompressed byte count (not the archive's declared, and therefore
    untrusted, size metadata) against ``max_bytes`` -- the same authoritative
    defense :func:`unpack_contract`'s main extraction loop applies to bundle
    members written to disk, for the one member (the sha256 manifest) that
    must be read into memory instead."""
    chunks = []
    total = 0
    with zf.open(name) as src:
        while True:
            chunk = src.read(_UNPACK_COPY_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(
                    f"archive member {name!r} decompresses to more than "
                    f"{max_bytes} bytes; refusing to unpack a possible zip bomb"
                )
            chunks.append(chunk)
    return b"".join(chunks)


def unpack_contract(archive_path: str, out_dir: str, *,
                    force: bool = False, max_bytes: Optional[int] = None) -> dict:
    """Unpack a ``.hotato`` archive to ``out_dir``, verifying every member
    against the sha256 manifest packed alongside it. Raises ``ValueError`` on
    any mismatch (a corrupt or tampered archive); the partial extraction is
    removed, never left half-written.

    A ``.hotato`` archive travels between teams, so this treats it as
    HOSTILE input, not just a corruption check. Before any member is
    extracted: every member name is checked for path traversal (``..``),
    absolute paths, and Windows-style backslash / drive-letter forms;
    symlink and encrypted members are refused; duplicate member names are
    refused; the member count is capped (:data:`MAX_UNPACK_MEMBERS`); and
    each member's declared compression ratio is checked for a bomb
    (:func:`_check_ratio_bomb`). Every member actually present in the
    archive must be declared in the sha256 manifest -- an undeclared extra
    member is refused, not silently ignored. During extraction itself, the
    ACTUAL decompressed byte count (not the archive's declared, and
    therefore untrusted, size metadata) is capped against ``max_bytes``
    (default :data:`DEFAULT_MAX_UNPACK_BYTES`, override with
    ``HOTATO_CONTRACT_MAX_UNPACK_BYTES`` or this argument / ``--max-bytes``),
    so a member whose real decompressed content outgrows what it claims is
    still caught. Every rejection leaves nothing behind outside the target:
    extraction happens into a sibling temp directory that is removed on any
    failure, and ``out_dir`` is only ever populated by the final atomic
    rename once every check has passed."""
    if max_bytes is None:
        max_bytes = _max_unpack_bytes()
    elif isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")

    # No pre-check: zipfile.ZipFile(archive_path) below raises the SAME
    # FileNotFoundError a plain open() would (with .filename set), which
    # errors.classify() already turns into a clean file_not_found error --
    # no need to duplicate that check here.
    if os.path.exists(out_dir) and not force:
        raise ValueError(
            f"{out_dir!r} already exists; pass --force to overwrite it, "
            "or choose a new --out"
        )
    # With --force the existing out_dir is NOT touched yet: the archive must
    # first prove valid end to end (every guard + full extraction into the
    # temp dir below). A hostile or corrupt archive must never cost the user
    # the directory they asked to replace.

    parent = os.path.dirname(os.path.normpath(out_dir)) or "."
    os.makedirs(parent, exist_ok=True)
    tmp_out = tempfile.mkdtemp(prefix=".hotato-unpack-tmp-", dir=parent)
    try:
        try:
            _require_regular_file(archive_path)
            # open-ok: _require_regular_file(archive_path) guards on the line above
            zf_ctx = zipfile.ZipFile(archive_path)
        except zipfile.BadZipFile as exc:
            # Not a valid zip at all (truncated / corrupt / wrong file):
            # reraise as ValueError so it hits the SAME handled-error / exit-2
            # contract as every other usage error, instead of an uncaught
            # zipfile.BadZipFile (which is not a ValueError/OSError subclass).
            raise ValueError(f"{archive_path!r} is not a valid .hotato archive: {exc}") from exc
        try:
            with zf_ctx as zf:
                infos = zf.infolist()
                if len(infos) > MAX_UNPACK_MEMBERS:
                    raise ValueError(
                        f"archive contains {len(infos)} members, more than the "
                        f"{MAX_UNPACK_MEMBERS} limit; refusing to unpack"
                    )
                seen_names = set()
                declared_total = 0
                for info in infos:
                    _check_member_safety(info)
                    if info.filename in seen_names:
                        raise ValueError(
                            f"archive contains a duplicate member: {info.filename!r}"
                        )
                    seen_names.add(info.filename)
                    _check_ratio_bomb(info)
                    declared_total += info.file_size
                    if declared_total > max_bytes:
                        raise ValueError(
                            f"archive declares more than {max_bytes} bytes of "
                            "decompressed content; refusing to unpack a "
                            "possible zip bomb (set --max-bytes or "
                            f"{_MAX_UNPACK_BYTES_ENV} to raise the limit for a "
                            "trusted archive)"
                        )

                names = set(zf.namelist())
                if MANIFEST_NAME not in names:
                    raise ValueError(
                        f"{archive_path!r} is not a hotato .hotato pack (no "
                        f"{MANIFEST_NAME})"
                    )
                manifest = json.loads(
                    _read_member_capped(zf, MANIFEST_NAME, max_bytes).decode("utf-8")
                )
                if not isinstance(manifest, dict):
                    raise ValueError(
                        f"{archive_path!r} has a malformed {MANIFEST_NAME} "
                        "(not a JSON object); the archive is corrupt"
                    )
                declared_members = set()
                for rel in sorted(manifest):
                    _safe_member_parts(rel)
                    declared_members.add(rel)
                    if rel not in names:
                        raise ValueError(
                            f"{archive_path!r} is missing {rel!r}, which the "
                            "manifest lists; the archive is corrupt"
                        )
                # Fail closed on any member the archive carries but its own
                # manifest does not declare, instead of silently ignoring it
                # (an undeclared member never reaches disk either way, since
                # extraction below only walks `manifest`, but a member the
                # sender never accounted for is itself a tamper signal a
                # sha256-only check would miss).
                undeclared = (names - declared_members) - {MANIFEST_NAME}
                if undeclared:
                    raise ValueError(
                        "archive contains members not declared in its "
                        f"manifest: {sorted(undeclared)}"
                    )

                written_total = 0
                for rel in sorted(manifest):
                    parts = rel.split("/")
                    dest = os.path.join(tmp_out, *parts)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with zf.open(rel) as src, open(dest, "wb") as out_f:
                        while True:
                            chunk = src.read(_UNPACK_COPY_CHUNK_BYTES)
                            if not chunk:
                                break
                            written_total += len(chunk)
                            if written_total > max_bytes:
                                raise ValueError(
                                    f"archive decompresses to more than "
                                    f"{max_bytes} bytes (exceeded while "
                                    f"extracting {rel!r}); refusing to unpack "
                                    "a possible zip bomb (set --max-bytes or "
                                    f"{_MAX_UNPACK_BYTES_ENV} to raise the "
                                    "limit for a trusted archive)"
                                )
                            out_f.write(chunk)
                    got = _sha256_file(dest)
                    want = manifest[rel]
                    if got != want:
                        raise ValueError(
                            f"{rel!r} failed sha256 verification after "
                            f"unpack (expected {want}, got {got}); the "
                            "archive is corrupt"
                        )
        except zipfile.BadZipFile as exc:
            # A member's own CRC-32 check (zipfile's built-in integrity
            # check, independent of hotato's sha256 manifest) caught the
            # corruption first. Same clean exit-2 treatment as every other
            # corrupt-archive case above.
            raise ValueError(
                f"{archive_path!r} is corrupt (bad CRC-32 while reading a "
                f"member): {exc}"
            ) from exc

        # Authenticity axis: the sha256 manifest above only proves the archive
        # is INTERNALLY byte-consistent, and `contract pack` recomputes it on
        # every pack -- so a bundle whose contract.json was edited (a loosened
        # policy) and re-packed passes the manifest check. Now that the bundle
        # is extracted, recompute the contract's canonical digest and compare it
        # to the one embedded at creation. A mismatch means the body was edited
        # after creation; refuse fail-closed (the temp extraction is removed and
        # out_dir is never created), matching unpack's hostile-input posture.
        auth = None
        cpath = os.path.join(tmp_out, "contract.json")
        if os.path.isfile(cpath):
            try:
                with _open_regular(cpath, "r", encoding="utf-8") as fh:
                    _unpacked_contract = json.load(fh)
            except (OSError, json.JSONDecodeError):
                _unpacked_contract = None
            if isinstance(_unpacked_contract, dict) and \
                    _unpacked_contract.get("schema") == SCHEMA:
                auth = _attest.assess_contract(_unpacked_contract, bundle_dir=tmp_out)
                if auth["authenticity"] == "tampered":
                    raise ValueError(
                        f"{archive_path!r} unpacked but its contract fails "
                        "authenticity: " + auth["reason"] + ". The sha256 "
                        "manifest only proves internal byte-consistency (a "
                        "re-pack recomputes it); refusing to unpack a tampered "
                        "contract"
                    )

        if os.path.exists(out_dir):
            shutil.rmtree(out_dir)
        os.replace(tmp_out, out_dir)
    except BaseException:
        shutil.rmtree(tmp_out, ignore_errors=True)
        raise
    return {"path": out_dir, "archive": archive_path, "files": len(manifest),
           "manifest": manifest,
           "authenticity": auth["authenticity"] if auth else "unattested",
           "authenticated": bool(auth and auth["authenticated"])}


def render_unpack_text(result: dict) -> str:
    return (
        f"unpacked {result['archive']} -> {result['path']} "
        f"({result['files']} files, sha256-verified; "
        f"authenticity: {result.get('authenticity', 'unattested')})"
    )


def unpack_result_json(result: dict) -> dict:
    return {
        "tool": "hotato", "kind": "contract-unpack", "schema_version": "1",
        "path": result["path"], "archive": result["archive"],
        "files": result["files"],
        "authenticity": result.get("authenticity", "unattested"),
        "authenticated": bool(result.get("authenticated")),
    }
