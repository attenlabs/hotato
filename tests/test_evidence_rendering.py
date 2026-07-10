"""Correct evidence rendering + card accessibility (plan rank 4).

Two proven bugs are fixed and pinned here:

(1) A hand-written NO-AUDIO ``hotato verify`` envelope pair returns a supported
    claim (data unchanged, that is honest), but the RENDERERS must not dress an
    envelope comparison up as a fresh-recapture paired proof. ``verify_sides``
    now attaches an evidence classification whose tier is ASSERTED (a standalone
    verify re-derives nothing from audio), and the card refuses the green
    "PAIRED EVIDENCE IMPROVED" headline for anything below the paired tier.

(2) The generated SVG carried no accessible text equivalent. Every card now
    ships ``role="img"``, an ``aria-labelledby`` pointing at a ``<title>`` and a
    ``<desc>``, and the pass/refuse status is carried in WORDS (readable in
    monochrome), not color alone.

The DATA (claim, counts) is unchanged; only the RENDERING is gated and the
evidence block is ADDED.
"""

from __future__ import annotations

import json
import xml.dom.minidom as minidom

from hotato import card as _card
from hotato import evidence as _evidence
from hotato import verify as _verify


# --- fixtures: reuse the exact envelope shape the verify tests build --------
#
# (mirrors tests/test_verify.py :: _ev / _env / _write, so this exercises the
# same standalone-verify path a hand-edited no-audio envelope pair takes)

def _ev(eid, expected_yield, passed, tov=0.0, tty=None, scorable=True):
    v = {
        "passed": passed,
        "did_yield": expected_yield if passed else (not expected_yield),
        "talk_over_sec": tov,
        "seconds_to_yield": tty,
        "reasons": [] if passed else ["out of bound"],
    }
    e = {"event_id": eid, "scenario_id": eid,
         "expected_yield": expected_yield, "verdict": v}
    if not scorable:
        e["scorable"] = False
    return e


def _env(events):
    passed = sum(1 for e in events if e["verdict"]["passed"])
    return {
        "tool": "hotato", "mode": "suite", "stack": "vapi", "offline": True,
        "events": events,
        "summary": {"events": len(events), "passed": passed,
                    "failed": len(events) - passed},
        "exit_code": 0,
    }


def _write(tmp_path, name, events):
    p = tmp_path / name
    p.write_text(json.dumps(_env(events)), encoding="utf-8")
    return str(p)


def _no_audio_sides(tmp_path):
    """A hand-written no-audio envelope pair: four previously-failing yield
    fixtures now pass, a hold guard still passes. NO ``audio_provenance`` on any
    event -- exactly the shape the bug accepted as 'paired evidence'."""
    before = _write(tmp_path, "before.json", [
        _ev("f1", True, False, 1.2), _ev("f2", True, False, 0.9, 2.1),
        _ev("f3", True, False, 1.5), _ev("f4", True, False, 0.8, 1.9),
        _ev("h1", False, True, 0.0),
    ])
    after = _write(tmp_path, "after.json", [
        _ev("f1", True, True, 0.3, 0.4), _ev("f2", True, True, 0.2, 0.5),
        _ev("f3", True, True, 0.4, 0.6), _ev("f4", True, True, 0.3, 0.4),
        _ev("h1", False, True, 0.0),
    ])
    return before, after


# A paired-tier evidence block, as a fix-trial recompute-from-audio produces:
# every required dimension caps at PAIRED or above.
_PAIRED_EVIDENCE = _evidence.classify({
    "score_integrity": "recomputed",
    "audio_identity": "recomputed",
    "policy_integrity": "manifest_pinned",
    "fixture_set_integrity": "manifest_complete",
    "input_health": "clean",
    "channel_mapping": "confirmed",
    "label_authority": "human",
    "pairing_integrity": "contract_bound",
    "capture_origin": "operator_asserted",
})


# --- (a) no-audio verify -> tier ASSERTED, card is not green ----------------

def test_verify_sides_attaches_asserted_evidence_for_no_audio_pair(tmp_path):
    before, after = _no_audio_sides(tmp_path)
    v = _verify.verify_sides(before, after, min_n=3)

    # DATA is unchanged: the claim is still supported, the counts still hold.
    assert v["claim"]["supported"] is True
    assert v["regression_axis"]["now_pass"] == 4

    # The ADDED evidence block classifies a standalone verify as ASSERTED.
    ev = v["evidence"]
    assert v["evidence_class"] == "paired-envelope-comparison"
    assert ev["tier"] == _evidence.TIER_ASSERTED
    assert ev["allows_positive_paired"] is False
    # No audio_provenance anywhere -> the identity dimension is honestly missing.
    assert ev["vector"]["audio_identity"] == "missing"
    assert ev["vector"]["score_integrity"] == "envelope_only"


def test_no_audio_verify_card_is_not_green_paired(tmp_path):
    before, after = _no_audio_sides(tmp_path)
    v = _verify.verify_sides(before, after, min_n=3)
    p = tmp_path / "verify.json"
    p.write_text(json.dumps(v), encoding="utf-8")

    svg = _card.make_card(str(p))
    # the green flagship headline must NOT appear for an envelope-only proof
    assert "PAIRED EVIDENCE IMPROVED" not in svg
    # the honest tier headline is shown instead, in words
    assert _evidence.TIER_HEADLINE[_evidence.TIER_ASSERTED] in svg  # ASSERTED (UNVERIFIED)
    # the green accent color is not used to fake a pass
    assert _card._C["green"] not in svg
    # the counts are still reported, never fabricated or dropped
    assert "4 of 4" in svg


def test_no_audio_verify_card_still_renders_a_card(tmp_path):
    """The muted card is still a valid, well-formed 1200x630 SVG (not exit 2):
    it reports the envelope comparison honestly rather than refusing outright."""
    before, after = _no_audio_sides(tmp_path)
    v = _verify.verify_sides(before, after, min_n=3)
    p = tmp_path / "verify.json"
    p.write_text(json.dumps(v), encoding="utf-8")
    svg = _card.make_card(str(p))
    root = minidom.parseString(svg).documentElement
    assert root.tagName == "svg"
    assert root.getAttribute("width") == "1200"
    assert root.getAttribute("height") == "630"


def test_legacy_verify_no_evidence_block_refuses_green(tmp_path):
    """A legacy verify JSON with NO evidence block at all is treated as the
    envelope-only ASSERTED ceiling: still no green card."""
    doc = {
        "tool": "hotato", "kind": "verify", "schema_version": "1",
        "claim": {"supported": True, "statement": "synthetic"},
        "regression_axis": {"now_pass": 2, "used_to_fail": 3},
        "hold_axis": {"hold_guards": 2, "still_pass": 2, "regressed": 0},
    }
    p = tmp_path / "legacy.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    svg = _card.make_card(str(p))
    assert "PAIRED EVIDENCE IMPROVED" not in svg
    assert _evidence.TIER_HEADLINE[_evidence.TIER_ASSERTED] in svg


# --- (b) every generated SVG carries an accessible text equivalent ----------

def _accessible_svg_checks(svg: str) -> None:
    doc = minidom.parseString(svg)  # well-formed XML
    root = doc.documentElement
    assert root.tagName == "svg"
    assert root.getAttribute("role") == "img"
    labelledby = root.getAttribute("aria-labelledby").split()
    assert labelledby, "svg must have aria-labelledby"
    titles = doc.getElementsByTagName("title")
    descs = doc.getElementsByTagName("desc")
    assert titles and descs, "svg needs a <title> and a <desc>"
    ids = {titles[0].getAttribute("id"), descs[0].getAttribute("id")}
    # aria-labelledby must actually resolve to the title and desc ids
    assert set(labelledby) == ids
    # the text equivalents are non-empty words (status readable without color)
    assert titles[0].firstChild and titles[0].firstChild.data.strip()
    assert descs[0].firstChild and descs[0].firstChild.data.strip()


def test_verify_card_svg_has_accessible_title_desc_role(tmp_path):
    before, after = _no_audio_sides(tmp_path)
    v = _verify.verify_sides(before, after, min_n=3)
    p = tmp_path / "verify.json"
    p.write_text(json.dumps(v), encoding="utf-8")
    svg = _card.make_card(str(p))
    assert 'role="img"' in svg
    assert "<title" in svg and "<desc" in svg
    assert 'aria-labelledby="card-title card-desc"' in svg
    _accessible_svg_checks(svg)


def test_paired_evidence_card_svg_is_accessible(tmp_path):
    doc = {
        "tool": "hotato", "kind": "verify", "schema_version": "1",
        "claim": {"supported": True, "statement": "synthetic"},
        "regression_axis": {"now_pass": 2, "used_to_fail": 3},
        "hold_axis": {"hold_guards": 2, "still_pass": 2, "regressed": 0},
        "evidence": _PAIRED_EVIDENCE,
    }
    p = tmp_path / "verify.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    svg = _card.make_card(str(p))
    _accessible_svg_checks(svg)
    # its text equivalent states the status in words, not color
    desc = minidom.parseString(svg).getElementsByTagName("desc")[0]
    assert "now pass" in desc.firstChild.data


def test_contract_card_also_accessible(tmp_path):
    """Accessibility applies to ALL kinds routing through _frame, contract too."""
    contract = {
        "tool": "hotato", "kind": "voice-turn-taking-contract", "id": "c-1",
        "label": {"expected_behavior": "yield"},
        "measurement": {"scorable": True, "passed": True,
                        "seconds_to_yield": 0.4},
    }
    p = tmp_path / "contract.json"
    p.write_text(json.dumps(contract), encoding="utf-8")
    svg = _card.make_card(str(p))
    _accessible_svg_checks(svg)
    # the pass status is carried in words (monochrome-readable)
    title = minidom.parseString(svg).getElementsByTagName("title")[0]
    assert "PASSED" in title.firstChild.data


# --- (c) an injected paired-tier evidence block DOES render green -----------

def test_injected_paired_evidence_renders_green_card(tmp_path):
    doc = {
        "tool": "hotato", "kind": "verify", "schema_version": "1",
        "claim": {"supported": True, "statement": "synthetic"},
        "regression_axis": {"now_pass": 2, "used_to_fail": 3},
        "hold_axis": {"hold_guards": 2, "still_pass": 2, "regressed": 0},
        "evidence": _PAIRED_EVIDENCE,
    }
    p = tmp_path / "verify.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    svg = _card.make_card(str(p))
    # a paired-tier block earns the green flagship headline and the green accent
    assert "PAIRED EVIDENCE IMPROVED" in svg
    assert _card._C["green"] in svg
    assert "2 of 3" in svg
    # the retired / reserved words never appear
    assert "VERIFIED" not in svg


def test_measured_tier_is_still_not_green(tmp_path):
    """A tier BETWEEN asserted and paired (MEASURED) is still refused the green
    card: the gate is >= paired, not merely 'better than asserted'."""
    measured = _evidence.classify({
        "score_integrity": "recomputed",
        "audio_identity": "asserted",     # distinct audio, not machine-verified
        "policy_integrity": "manifest_pinned",
        "fixture_set_integrity": "manifest_complete",
        "input_health": "clean",
        "channel_mapping": "confirmed",
        "label_authority": "human",
        "pairing_integrity": "contract_bound",
        "capture_origin": "operator_asserted",
    })
    # audio_identity=asserted caps the whole vector at MEASURED
    assert measured["tier"] == _evidence.TIER_MEASURED
    doc = {
        "tool": "hotato", "kind": "verify", "schema_version": "1",
        "claim": {"supported": True, "statement": "synthetic"},
        "regression_axis": {"now_pass": 2, "used_to_fail": 3},
        "hold_axis": {"hold_guards": 2, "still_pass": 2, "regressed": 0},
        "evidence": measured,
    }
    p = tmp_path / "verify.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    svg = _card.make_card(str(p))
    assert "PAIRED EVIDENCE IMPROVED" not in svg
    assert _evidence.TIER_HEADLINE[_evidence.TIER_MEASURED] in svg  # MEASURED FROM AUDIO
