"""``hotato card``: render a hotato result into a shareable SVG card.

Pinned here:

  * a talk-over and a false-stop candidate card render from a real demo sweep
    candidate ref (FILE#N), and the threshold-funnel plan renders the hero card;
  * the SVG is 1200x630, well-formed XML, references NO external resource (no
    font, image, stylesheet, script, or link) and is byte-identical across runs;
  * a source recording's identifiers (a call id inside a pulled recording name)
    are hidden by default and only shown under --include-identifiers;
  * a bad ref, a bare sweep result with no ref, and a non-card JSON all exit 2;
  * the committed docs/assets/cards SVGs match a fresh render.
"""

import json
import os
import xml.dom.minidom as minidom
from importlib import resources

import pytest

from hotato import analyze as _analyze
from hotato import card as _card
from hotato import cli
from hotato import evidence as _evidence
from hotato.core import run_suite
from hotato.diagnose import diagnose_envelope
from hotato.fixplan import build_plan

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARDS = os.path.join(ROOT, "docs", "assets", "cards")

_TALK_OVER = ("overlap_while_agent_talking", "agent_start_during_caller")
_FALSE_STOP = ("agent_stop_no_caller",)


# --- demo-derived fixtures (no network, no credentials) -------------------

def _demo_sweep(tmp_path):
    """A real sweep result over the two bundled demo calls, written to disk."""
    audio = str(resources.files("hotato").joinpath(
        "data", "demo", "failing", "audio"))
    aggregate, _ = _analyze.analyze_folder(audio)
    p = tmp_path / "hotato-sweep.json"
    p.write_text(json.dumps(aggregate), encoding="utf-8")
    return p, aggregate


def _demo_plan(tmp_path):
    """The threshold-funnel fix plan the bundled failing battery produces."""
    root = resources.files("hotato").joinpath("data", "demo", "failing")
    env = run_suite(scenarios_dir=str(root.joinpath("scenarios")),
                    audio_dir=str(root.joinpath("audio")))
    plan = build_plan(diagnosis=diagnose_envelope(env))
    p = tmp_path / "plan.json"
    p.write_text(json.dumps(plan), encoding="utf-8")
    return p, plan


def _rank_of(aggregate, kinds):
    for i, c in enumerate(aggregate["candidates"], 1):
        if c.get("kind") in kinds:
            return i
    raise AssertionError(f"no candidate of kind {kinds} in the demo sweep")


def _card_cli(tmp_path, ref, *extra):
    out = tmp_path / "card.svg"
    rc = cli.main(["card", ref, "--out", str(out), *extra])
    svg = out.read_text(encoding="utf-8") if out.exists() else None
    return rc, svg


def _assert_is_card_svg(svg):
    doc = minidom.parseString(svg)  # raises if not well-formed
    root = doc.documentElement
    assert root.tagName == "svg"
    assert root.getAttribute("width") == "1200"
    assert root.getAttribute("height") == "630"


# --- candidate cards (A / B) from a real demo sweep -----------------------

def test_card_from_demo_candidate_talk_over(tmp_path):
    sweep, agg = _demo_sweep(tmp_path)
    n = _rank_of(agg, _TALK_OVER)
    rc, svg = _card_cli(tmp_path, f"{sweep}#{n}")
    assert rc == 0
    _assert_is_card_svg(svg)
    assert "TALK-OVER CANDIDATE" in svg  # the kind tag
    assert "of overlap while the agent was talking" in svg
    assert "Hotato reports timing candidates, not intent." in svg


def test_card_from_demo_candidate_false_stop(tmp_path):
    sweep, agg = _demo_sweep(tmp_path)
    n = _rank_of(agg, _FALSE_STOP)
    rc, svg = _card_cli(tmp_path, f"{sweep}#{n}")
    assert rc == 0
    _assert_is_card_svg(svg)
    assert "FALSE-STOP CANDIDATE" in svg  # the kind tag
    assert "caller nearby" in svg


# --- the threshold-funnel hero card (C) -----------------------------------

def test_card_from_plan_funnel(tmp_path):
    plan_path, _ = _demo_plan(tmp_path)
    rc, svg = _card_cli(tmp_path, str(plan_path))
    assert rc == 0
    _assert_is_card_svg(svg)
    assert "NO SINGLE THRESHOLD CAN" in svg
    assert "THRESHOLD FUNNEL" in svg
    assert "fix class: engagement-control" in svg
    assert "Hotato refused threshold tuning." in svg
    # no accuracy number anywhere on the hero card
    assert "%" not in svg


# --- the paired-comparison card (D): a supported verify rollup ------------
#
# This card is the audit's flagship honesty case: the most authoritative-
# looking artifact must never be the weakest evidence path. It renders "PAIRED
# EVIDENCE IMPROVED", never the bare word "VERIFIED" or "fix verified", and is
# refused (no card, exit 2) whenever that headline would be false: an
# unsupported claim, a regressed hold guard, or -- the gap the previous
# render missed -- a supported claim where nothing actually improved.

def _verify_json(tmp_path, *, supported=True, now_pass=2, used_to_fail=3,
                  hold_guards=2, still_pass=2, regressed=0, evidence=None):
    doc = {
        "tool": "hotato", "kind": "verify", "schema_version": "1",
        "claim": {"supported": supported, "statement": "synthetic"},
        "regression_axis": {"now_pass": now_pass, "used_to_fail": used_to_fail},
        "hold_axis": {"hold_guards": hold_guards, "still_pass": still_pass,
                      "regressed": regressed},
    }
    if evidence is not None:
        doc["evidence"] = evidence
    p = tmp_path / "verify.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


# A paired-tier evidence block (as a fix-trial recompute-from-audio produces):
# every required dimension caps at PAIRED or above. Only this may render green.
# What a real fix trial WITHOUT a capture receipt produces: paired before/after,
# but the recapture origin is only operator-asserted -> a qualified PAIRED card,
# never the fresh-recapture green.
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
    "opposite_risk_guard": "present_passing",
})

# The only vector that earns the green fresh-recapture card: a runner-attested,
# signed, hold-guarded pair (evidence tier ATTESTED).
_ATTESTED_EVIDENCE = _evidence.classify({
    "score_integrity": "recomputed",
    "audio_identity": "recomputed",
    "policy_integrity": "signed",
    "fixture_set_integrity": "manifest_complete",
    "input_health": "clean",
    "channel_mapping": "confirmed",
    "label_authority": "human",
    "pairing_integrity": "contract_bound",
    "capture_origin": "runner_attested",
    "opposite_risk_guard": "present_passing",
})


def test_card_from_attested_evidence_renders_fresh_recapture_green(tmp_path):
    # The green fresh-recapture card renders ONLY when the evidence reaches the
    # ATTESTED tier (runner-verified capture receipt + hold guard).
    v = _verify_json(tmp_path, now_pass=2, used_to_fail=3,
                      still_pass=2, hold_guards=2, evidence=_ATTESTED_EVIDENCE)
    rc, svg = _card_cli(tmp_path, str(v))
    assert rc == 0
    _assert_is_card_svg(svg)
    assert "PAIRED FRESH-RECAPTURE" in svg
    assert _card._C["green"] in svg
    assert "2 of 3" in svg  # failing fixtures now pass
    assert "2 of 2" in svg  # hold fixtures still pass
    assert "Hotato reports coincidence, not causation." in svg
    assert "VERIFIED" not in svg
    assert "FIX VERIFIED" not in svg


def test_card_from_operator_asserted_pair_is_qualified_not_green(tmp_path):
    # Operator-asserted paired evidence renders a QUALIFIED card: it names the
    # origin, does NOT claim fresh recapture, and does NOT use the green accent.
    v = _verify_json(tmp_path, now_pass=2, used_to_fail=3,
                      still_pass=2, hold_guards=2, evidence=_PAIRED_EVIDENCE)
    rc, svg = _card_cli(tmp_path, str(v))
    assert rc == 0
    _assert_is_card_svg(svg)
    assert "PAIRED (OPERATOR-ASSERTED)" in svg
    assert "PAIRED FRESH-RECAPTURE" not in svg
    assert _card._C["green"] not in svg
    assert "2 of 3" in svg


def test_card_from_verify_unsupported_claim_refused(tmp_path):
    v = _verify_json(tmp_path, supported=False)
    rc, svg = _card_cli(tmp_path, str(v))
    assert rc == 2
    assert svg is None


def test_card_from_verify_regressed_hold_refused(tmp_path):
    v = _verify_json(tmp_path, regressed=1)
    rc, svg = _card_cli(tmp_path, str(v))
    assert rc == 2
    assert svg is None


def test_card_from_verify_zero_improvement_refused(tmp_path):
    # A supported claim (enough previously-failing fixtures) where NOTHING
    # newly passes must not render "PAIRED EVIDENCE IMPROVED": that headline
    # would be false even though the claim technically clears --min-n.
    v = _verify_json(tmp_path, supported=True, now_pass=0, used_to_fail=3)
    rc, svg = _card_cli(tmp_path, str(v))
    assert rc == 2
    assert svg is None


# --- redaction ------------------------------------------------------------

def _sweep_with_call_id(tmp_path):
    """A sweep whose top candidate came from a pulled recording whose name
    carries the call id -- the case redaction exists for."""
    _, agg = _demo_sweep(tmp_path)
    agg["candidates"][0]["source"] = "vapi__call_SECRET123.wav"
    p = tmp_path / "sweep-id.json"
    p.write_text(json.dumps(agg), encoding="utf-8")
    return p


def test_card_redacts_identifiers_by_default(tmp_path):
    sweep = _sweep_with_call_id(tmp_path)
    rc, svg = _card_cli(tmp_path, f"{sweep}#1")
    assert rc == 0
    assert "SECRET123" not in svg
    assert "vapi__" not in svg


def test_card_include_identifiers_shows_the_basename(tmp_path):
    sweep = _sweep_with_call_id(tmp_path)
    rc, svg = _card_cli(tmp_path, f"{sweep}#1", "--include-identifiers")
    assert rc == 0
    assert "vapi__call_SECRET123.wav" in svg


# --- input rejection: exit 2 ----------------------------------------------

def test_card_invalid_ref_exits_2(tmp_path):
    sweep, _ = _demo_sweep(tmp_path)
    assert cli.main(["card", f"{sweep}#99", "--out",
                     str(tmp_path / "x.svg")]) == 2


def test_card_bare_sweep_result_needs_a_ref(tmp_path):
    sweep, _ = _demo_sweep(tmp_path)
    assert cli.main(["card", str(sweep), "--out", str(tmp_path / "x.svg")]) == 2


def test_card_non_card_json_exits_2(tmp_path):
    junk = tmp_path / "junk.json"
    junk.write_text(json.dumps({"tool": "hotato", "kind": "frame-dump"}),
                    encoding="utf-8")
    assert cli.main(["card", str(junk), "--out", str(tmp_path / "x.svg")]) == 2


def test_card_missing_file_exits_2(tmp_path):
    assert cli.main(["card", str(tmp_path / "nope.json"),
                     "--out", str(tmp_path / "x.svg")]) == 2


# --- the no-external-resource invariant -----------------------------------

def test_card_svg_has_no_external_links(tmp_path):
    plan_path, _ = _demo_plan(tmp_path)
    _, svg = _card_cli(tmp_path, str(plan_path))
    for banned in ("xlink", "<image", "<script", "@import", "url(", "href",
                   "src="):
        assert banned not in svg, f"card SVG must not contain {banned!r}"
    # the only URL in the file is the required SVG namespace declaration.
    assert svg.count("http") == 1
    assert 'xmlns="http://www.w3.org/2000/svg"' in svg


# --- determinism: byte-identical across runs ------------------------------

def test_card_svg_is_deterministic(tmp_path):
    sweep, agg = _demo_sweep(tmp_path)
    ref = f"{sweep}#{_rank_of(agg, _TALK_OVER)}"
    a = _card.make_card(ref)
    b = _card.make_card(ref)
    assert a == b
    plan_path, plan = _demo_plan(tmp_path)
    assert _card.render_plan_card(plan) == _card.render_plan_card(plan)


# --- stdout path (no --out) -----------------------------------------------

def test_card_writes_to_stdout_without_out(tmp_path, capsys):
    plan_path, _ = _demo_plan(tmp_path)
    rc = cli.main(["card", str(plan_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("<svg")
    assert "NO SINGLE THRESHOLD CAN" in out


# --- the committed assets stay in lockstep with the generator -------------

@pytest.mark.skipif(not os.path.isdir(CARDS),
                    reason="docs/assets/cards pruned from sdist "
                           "(regenerate with scripts/render_card_assets.py)")
def test_committed_cards_match_a_fresh_render(tmp_path):
    _, plan = _demo_plan(tmp_path)
    sweep, agg = _demo_sweep(tmp_path)
    expected = {
        "no-single-threshold-card.svg": _card.render_plan_card(plan),
        "talk-over-card.svg": _card.make_card(
            f"{sweep}#{_rank_of(agg, _TALK_OVER)}"),
        "false-stop-card.svg": _card.make_card(
            f"{sweep}#{_rank_of(agg, _FALSE_STOP)}"),
    }
    for name, svg in expected.items():
        committed = open(os.path.join(CARDS, name), encoding="utf-8").read()
        assert committed == svg, (
            f"{name} is stale; regenerate with "
            "python3 scripts/render_card_assets.py")
