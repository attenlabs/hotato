"""``hotato.assert_packs`` + ``hotato assert packs`` / ``assert run --pack``:
the curated, deterministic assertion packs and their CLI wiring.

Pinned here:

* the four packs (required-disclosure, prohibited-language, pii-leak,
  identity-verification-order) load, and the manifest exactly matches the pack
  files shipped on disk (no orphan file, no dangling entry);
* every pack is built ONLY from deterministic, model-free kinds already in
  ``assert_.KINDS`` (never a rubric kind), and validates through
  ``validate_assertions_doc``; the identity pack exercises the new ``order``
  kind;
* each pack PASSES a clean fixture and CATCHES a seeded violation;
* ``assert packs`` lists the packs (text + a JSON envelope);
* ``assert run --pack`` merges one or more packs (with or without
  ``--assertions``), a duplicate assertion id across the merged sources is a
  usage error (exit 2), an unknown pack name is exit 2, and passing neither
  ``--assertions`` nor ``--pack`` is exit 2.
"""

from __future__ import annotations

import json
from importlib import resources

import pytest

from hotato import assert_ as A
from hotato import assert_packs as AP
from hotato import cli

_EXPECTED_PACKS = {
    "required-disclosure",
    "prohibited-language",
    "pii-leak",
    "identity-verification-order",
}


def _turn(role, text):
    return {"role": role, "text": text}


def _run_pack(name, turns):
    """Merge one pack into an empty run and evaluate it against ``turns``."""
    doc = AP.merge_into_doc(None, [name])
    ctx = A.build_context(transcript=list(turns))
    return A.run_assertions(doc, ctx)


def _statuses(env):
    return {r["id"]: r["status"] for r in env["results"]}


def _write(tmp_path, name, turns):
    path = tmp_path / name
    path.write_text(json.dumps(turns), encoding="utf-8")
    return str(path)


# --- manifest matches the files on disk ------------------------------------

def test_manifest_loads_and_lists_the_expected_packs():
    manifest = AP.load_manifest()
    assert manifest["pack_set"] == AP.PACK_SET_NAME
    assert {e["name"] for e in manifest["packs"]} == _EXPECTED_PACKS
    assert set(AP.names()) == _EXPECTED_PACKS


def test_manifest_matches_the_pack_files_on_disk():
    pack_dir = resources.files("hotato").joinpath(*AP.PACK_DIR)
    on_disk = {
        p.name for p in pack_dir.iterdir()
        if p.name.endswith(".json") and p.name != AP.MANIFEST_FILENAME
    }
    referenced = {e["file"] for e in AP.load_manifest()["packs"]}
    assert on_disk == referenced  # no orphan file, no dangling entry
    for entry in AP.load_manifest()["packs"]:
        assert resources.files("hotato").joinpath(*AP.PACK_DIR, entry["file"]).is_file()


def test_every_pack_validates_and_uses_only_deterministic_kinds():
    for name in AP.names():
        assertions = AP.pack_assertions(name)
        assert assertions, name
        # validates as a standalone assert.v1 document
        A.validate_assertions_doc({"version": 1, "assertions": assertions})
        for a in assertions:
            assert a["kind"] in A.KINDS          # a deterministic kind
            assert a["kind"] not in A.RUBRIC_KINDS  # never the model-judged lane


def test_identity_pack_exercises_the_order_kind():
    kinds = {a["kind"] for a in AP.pack_assertions("identity-verification-order")}
    assert "order" in kinds


# --- each pack passes a clean fixture and catches a violation --------------

def test_required_disclosure_clean_passes_and_violation_fails():
    clean = [
        _turn("agent", "This call is recorded for quality. Do you consent to continue?"),
        _turn("caller", "Yes."),
    ]
    assert _run_pack("required-disclosure", clean)["exit_code"] == 0

    violation = [
        _turn("agent", "Hi, how can I help you today?"),
        _turn("caller", "Just a question."),
    ]
    env = _run_pack("required-disclosure", violation)
    assert env["exit_code"] == 1
    assert _statuses(env)["required-disclosure.recording-notice"] == "FAIL"


def test_prohibited_language_clean_passes_and_violation_fails():
    clean = [_turn("agent", "I can help you look into that today.")]
    assert _run_pack("prohibited-language", clean)["exit_code"] == 0

    violation = [_turn("agent", "I guarantee this refund will go through.")]
    env = _run_pack("prohibited-language", violation)
    assert env["exit_code"] == 1
    assert _statuses(env)["prohibited-language.no-guarantees"] == "FAIL"


def test_pii_leak_clean_passes_and_violation_fails():
    clean = [_turn("agent", "Thanks, your order is confirmed.")]
    assert _run_pack("pii-leak", clean)["exit_code"] == 0

    violation = [_turn("caller", "my email is jane@example.com")]
    env = _run_pack("pii-leak", violation)
    assert env["exit_code"] == 1
    assert _statuses(env)["pii-leak.no-leak"] == "FAIL"


def test_identity_verification_order_clean_passes_and_violation_fails():
    clean = [
        _turn("agent", "To verify your identity, what is your date of birth?"),
        _turn("caller", "January 1st, 1990."),
        _turn("agent", "Thank you. Your account number is on file."),
    ]
    assert _run_pack("identity-verification-order", clean)["exit_code"] == 0

    # account details disclosed BEFORE identity is verified -> order FAIL
    violation = [
        _turn("agent", "Your account number is 12345."),
        _turn("agent", "Now, to verify your identity, what is your date of birth?"),
    ]
    env = _run_pack("identity-verification-order", violation)
    assert env["exit_code"] == 1
    assert _statuses(env)["identity-verification-order.verify-before-details"] == "FAIL"


# --- merge semantics --------------------------------------------------------

def test_merge_into_doc_appends_pack_assertions_after_the_base():
    base = {"version": 1, "assertions": [{"id": "base-a", "kind": "phrase", "regex": "hi"}]}
    merged = AP.merge_into_doc(base, ["pii-leak"])
    ids = [a["id"] for a in merged["assertions"]]
    assert ids == ["base-a", "pii-leak.no-leak"]


def test_merge_into_doc_with_no_packs_returns_doc_unchanged():
    base = {"version": 1, "assertions": [{"id": "a", "kind": "phrase", "regex": "hi"}]}
    assert AP.merge_into_doc(base, []) is base


def test_duplicate_id_across_packs_is_refused():
    # merging the same pack twice collides its ids
    with pytest.raises(ValueError, match="duplicate assertion id"):
        AP.merge_into_doc(None, ["pii-leak", "pii-leak"])


def test_duplicate_id_between_assertions_and_pack_is_refused():
    base = {"version": 1, "assertions": [
        {"id": "pii-leak.no-leak", "kind": "phrase", "regex": "hi"},
    ]}
    with pytest.raises(ValueError, match="duplicate assertion id"):
        AP.merge_into_doc(base, ["pii-leak"])


def test_unknown_pack_name_is_refused():
    with pytest.raises(ValueError, match="unknown assertion pack"):
        AP.merge_into_doc(None, ["does-not-exist"])


# --- CLI: assert packs ------------------------------------------------------

def test_cli_assert_packs_text_lists_every_pack(capsys):
    rc = cli.main(["assert", "packs"])
    assert rc == 0
    out = capsys.readouterr().out
    for name in _EXPECTED_PACKS:
        assert name in out


def test_cli_assert_packs_json_is_a_well_formed_envelope(capsys):
    rc = cli.main(["assert", "packs", "--format", "json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tool"] == "hotato"
    assert payload["kind"] == "assert-packs"
    assert {p["name"] for p in payload["packs"]} == _EXPECTED_PACKS
    ivo = next(p for p in payload["packs"] if p["name"] == "identity-verification-order")
    assert "order" in ivo["kinds"]


# --- CLI: assert run --pack -------------------------------------------------

def test_cli_run_pack_only_clean_passes(tmp_path, capsys):
    transcript = _write(tmp_path, "clean.json", [
        _turn("agent", "recorded for quality. do you consent?"),
    ])
    rc = cli.main(["assert", "run", "--transcript", transcript,
                   "--pack", "required-disclosure"])
    assert rc == 0


def test_cli_run_pack_catches_violation(tmp_path, capsys):
    transcript = _write(tmp_path, "bad.json", [
        _turn("caller", "my email is jane@example.com"),
    ])
    rc = cli.main(["assert", "run", "--transcript", transcript,
                   "--pack", "pii-leak"])
    assert rc == 1


def test_cli_run_merges_assertions_file_and_packs(tmp_path, capsys):
    transcript = _write(tmp_path, "t.json", [
        _turn("agent", "recorded for quality. do you consent? hello world"),
    ])
    af = tmp_path / "extra.json"
    af.write_text(json.dumps({
        "version": 1,
        "assertions": [{"id": "says-hello", "kind": "phrase", "regex": "hello world"}],
    }), encoding="utf-8")
    rc = cli.main(["assert", "run", "--transcript", transcript,
                   "--assertions", str(af), "--pack", "required-disclosure",
                   "--format", "json"])
    assert rc == 0
    env = json.loads(capsys.readouterr().out)
    ids = {r["id"] for r in env["results"]}
    assert "says-hello" in ids
    assert "required-disclosure.recording-notice" in ids


def test_cli_run_unknown_pack_is_usage_error(tmp_path, capsys):
    transcript = _write(tmp_path, "t.json", [_turn("agent", "hi")])
    rc = cli.main(["assert", "run", "--transcript", transcript,
                   "--pack", "nope"])
    assert rc == 2


def test_cli_run_duplicate_id_across_packs_is_usage_error(tmp_path, capsys):
    transcript = _write(tmp_path, "t.json", [_turn("agent", "hi")])
    rc = cli.main(["assert", "run", "--transcript", transcript,
                   "--pack", "pii-leak", "--pack", "pii-leak"])
    assert rc == 2


def test_cli_run_without_assertions_or_pack_is_usage_error(tmp_path, capsys):
    transcript = _write(tmp_path, "t.json", [_turn("agent", "hi")])
    rc = cli.main(["assert", "run", "--transcript", transcript])
    assert rc == 2
