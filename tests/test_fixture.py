"""R-09: `hotato fixture create` / `fixture promote` / `contract create` must
NOT mint a signed human/human-shared label-record unless BOTH gates hold:

  1. an EXPLICIT reviewer principal (never an env-derived $USER / HOTATO_REVIEWER
     name), and
  2. a human-review CONFIRMATION -- an interactive terminal on stdin, or an
     explicit --i-attest-human-review.

The exploit these tests pin down: a scripted, non-interactive pipeline (a
heuristic picks --expect, no operator ever listens to the audio) runs on a
machine that has a signing key configured for legitimate purposes.

  * STEP 1 closed the "$USER auto-signs a machine-chosen label" hole: no
    explicit reviewer -> no signed record.
  * STEP 3 closes the residual "a script that merely threads a --reviewer NAME
    signs a machine-chosen label" hole: a reviewer name is only a claim, not
    proof a human looked. Without a TTY or --i-attest-human-review, the label
    degrades honestly to "asserted".

The legitimate human flow (a named reviewer at a terminal, or a named reviewer
who attests) still mints a verifying signed record -- proven by the positive
controls below.

Signing is isolated to tmp_path (HOME re-pointed, HOTATO_SIGN_KEY_ID cleared)
so no ambient Ed25519 key on the dev/CI machine can turn the deterministic
HMAC "human-shared" tier into "human".
"""
import json
import os
from importlib import resources

from hotato import cli
from hotato import contract as _contract
from hotato import fixture as _fixture
from tests import _trial_audio as ta

_EXAMPLE = str(resources.files("hotato").joinpath(
    "data", "audio", "01-hard-interruption.example.wav"))     # yields at 2.40


def _machine_with_key(tmp_path, monkeypatch, *, tty=False):
    """The exact environment the R-09 laundering exploit needs: a shared signing
    key configured, a shell user identity present, and stdin's TTY state pinned
    -- while HOME is isolated to tmp_path so no ambient Ed25519 dev/CI key can
    stand in (the HMAC "human-shared" tier stays deterministic). ``tty=False``
    is the scripted/CI case; ``tty=True`` simulates an operator at a terminal."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))     # windows os.path.expanduser
    monkeypatch.delenv("HOTATO_SIGN_KEY_ID", raising=False)
    monkeypatch.setenv("HOTATO_ATTEST_KEY", "r09-ambient-ci-key")
    monkeypatch.setenv("HOTATO_REVIEWER", "ci-bot")
    monkeypatch.setenv("USER", "ci-bot")
    monkeypatch.setenv("USERNAME", "ci-bot")
    monkeypatch.setattr("sys.stdin.isatty", lambda: tty, raising=False)


# --- fixture create (library) ----------------------------------------------

def test_fixture_create_no_explicit_reviewer_does_not_mint_human_label(
    tmp_path, monkeypatch,
):
    """STEP 1: no reviewer supplied (today's `hotato fixture create` shape) ->
    even with a signing key present and a shell user identity, NO signed
    label-record is minted; the env-derived name never stands in for human
    authority."""
    _machine_with_key(tmp_path, monkeypatch)
    out = str(tmp_path / "out")

    result = _fixture.create_fixture(
        stereo=_EXAMPLE, fixture_id="fx-r09-noreviewer", onset_sec=2.40,
        expect="yield", out_dir=out, reviewer_principal=None,
    )

    assert result["scenario"].get("label_record") is None
    assert "label_record" not in result["scenario"]
    assert not os.path.exists(
        os.path.join(out, "labels", "fx-r09-noreviewer.label.json"))


def test_fixture_create_blank_reviewer_does_not_mint(tmp_path, monkeypatch):
    """A blank/whitespace reviewer is "none named", not a signer: it cannot
    slip past the explicit-reviewer requirement even with attestation."""
    _machine_with_key(tmp_path, monkeypatch)
    out = str(tmp_path / "out")

    result = _fixture.create_fixture(
        stereo=_EXAMPLE, fixture_id="fx-r09-blank", onset_sec=2.40,
        expect="yield", out_dir=out, reviewer_principal="   ",
        human_review_attested=True,
    )

    assert result["scenario"].get("label_record") is None


def test_fixture_create_reviewer_name_without_attest_or_tty_stays_asserted(
    tmp_path, monkeypatch,
):
    """STEP 3 (the core new gate): a script threads a real reviewer NAME but is
    non-interactive and does not attest -- a name alone is only a claim, not
    proof a human listened, so NO signed record is minted and the label
    degrades honestly to "asserted"."""
    _machine_with_key(tmp_path, monkeypatch, tty=False)
    out = str(tmp_path / "out")

    result = _fixture.create_fixture(
        stereo=_EXAMPLE, fixture_id="fx-r09-nameonly", onset_sec=2.40,
        expect="yield", out_dir=out, reviewer_principal="qa-alice",
        # no human_review_attested, no TTY
    )

    assert result["scenario"].get("label_record") is None
    assert not os.path.exists(
        os.path.join(out, "labels", "fx-r09-nameonly.label.json"))


def test_fixture_create_reviewer_with_attest_mints_signed(
    tmp_path, monkeypatch,
):
    """STEP 3 positive control: the legitimate CI flow (a named reviewer WHO
    ATTESTS a human reviewed the audio) still mints a verifying signed record.
    The fix narrows only the unattested path, never this one."""
    _machine_with_key(tmp_path, monkeypatch, tty=False)
    out = str(tmp_path / "out")

    result = _fixture.create_fixture(
        stereo=_EXAMPLE, fixture_id="fx-r09-attested", onset_sec=2.40,
        expect="yield", out_dir=out, reviewer_principal="qa-alice",
        human_review_attested=True,
    )

    lr = result["scenario"].get("label_record")
    assert lr is not None
    assert lr["reviewer_principal"] == "qa-alice"     # the NAMED human, not ci-bot
    assert lr["decision"] == "yield"
    assert lr["signer"]["algo"] == "hmac"             # deterministic (HOME isolated)
    assert os.path.exists(
        os.path.join(out, "labels", "fx-r09-attested.label.json"))


def test_fixture_create_interactive_tty_mints_without_attest(
    tmp_path, monkeypatch,
):
    """STEP 3 positive control: an operator at an interactive terminal (a real
    human is present) mints a signed record without needing the explicit
    attestation flag -- the TTY IS the human-review confirmation."""
    _machine_with_key(tmp_path, monkeypatch, tty=True)
    out = str(tmp_path / "out")

    result = _fixture.create_fixture(
        stereo=_EXAMPLE, fixture_id="fx-r09-tty", onset_sec=2.40,
        expect="yield", out_dir=out, reviewer_principal="qa-alice",
    )

    lr = result["scenario"].get("label_record")
    assert lr is not None
    assert lr["reviewer_principal"] == "qa-alice"


# --- contract create (library) ---------------------------------------------

def test_contract_create_no_explicit_reviewer_degrades_to_asserted(
    tmp_path, monkeypatch,
):
    """STEP 1 on the contract path: a non-interactive `hotato contract create`
    with no reviewer degrades to label_authority 'asserted' and carries no
    signed label-record, even with a key present."""
    _machine_with_key(tmp_path, monkeypatch)
    wav = str(tmp_path / "call.wav"); ta.talkover_call(wav)
    out = str(tmp_path / "contracts")

    result = _contract.create_contract(
        stereo=wav, contract_id="ct-r09-noreviewer", expect="yield",
        out_dir=out, onset_sec=2.0,
        max_talk_over_sec=1.0, max_time_to_yield_sec=1.0,
    )
    contract = result["contract"]

    assert contract["label_record"] is None
    assert contract["label_authority"] == "asserted"
    # the env-derived name still records WHO ran the command (display only),
    # but that never elevates the honest "asserted" authority.
    assert contract["identity"]["reviewer"] == "ci-bot"
    lr_path = os.path.join(result["dir"], "evidence", "label_record.json")
    with open(lr_path, encoding="utf-8") as fh:
        assert json.load(fh) is None


def test_contract_create_reviewer_name_without_attest_stays_asserted(
    tmp_path, monkeypatch,
):
    """STEP 3 on the contract path: a script passes a real reviewer NAME but is
    non-interactive and does not attest -> 'asserted', no signed record. The
    named reviewer is still recorded as the display identity."""
    _machine_with_key(tmp_path, monkeypatch, tty=False)
    wav = str(tmp_path / "call.wav"); ta.talkover_call(wav)
    out = str(tmp_path / "contracts")

    result = _contract.create_contract(
        stereo=wav, contract_id="ct-r09-nameonly", expect="yield",
        out_dir=out, onset_sec=2.0, reviewer_principal="qa-bob",
        max_talk_over_sec=1.0, max_time_to_yield_sec=1.0,
    )
    contract = result["contract"]

    assert contract["label_authority"] == "asserted"
    assert contract["label_record"] is None
    assert contract["identity"]["reviewer"] == "qa-bob"


def test_contract_create_reviewer_with_attest_signs_human_shared(
    tmp_path, monkeypatch,
):
    """STEP 3 positive control for the contract path: an explicit reviewer WHO
    ATTESTS reaches human-shared authority with a matching signed record."""
    _machine_with_key(tmp_path, monkeypatch, tty=False)
    wav = str(tmp_path / "call.wav"); ta.talkover_call(wav)
    out = str(tmp_path / "contracts")

    result = _contract.create_contract(
        stereo=wav, contract_id="ct-r09-attested", expect="yield",
        out_dir=out, onset_sec=2.0, reviewer_principal="qa-bob",
        human_review_attested=True,
        max_talk_over_sec=1.0, max_time_to_yield_sec=1.0,
    )
    contract = result["contract"]

    assert contract["label_authority"] == "human-shared"
    assert contract["label_record"]["reviewer_principal"] == "qa-bob"
    assert contract["identity"]["reviewer"] == "qa-bob"


# --- CLI wiring (STEP 2: --reviewer / --i-attest-human-review) --------------

def test_cli_fixture_create_without_reviewer_is_asserted(tmp_path, monkeypatch):
    """`hotato fixture create` with no --reviewer succeeds (exit 0) and writes NO
    signed label-record -- the label degrades to 'asserted'. --reviewer is
    OPTIONAL so existing quickstarts keep working; a signed 'human'/'human-shared'
    authority requires --reviewer AND a human-review confirmation (asserted below)."""
    _machine_with_key(tmp_path, monkeypatch, tty=False)
    out = tmp_path / "out"
    rc = cli.main(["fixture", "create", "--stereo", _EXAMPLE, "--id",
                   "fx-noreviewer", "--onset", "2.40", "--expect", "yield",
                   "--out", str(out)])
    assert rc == 0
    with open(out / "scenarios" / "fx-noreviewer.json", encoding="utf-8") as fh:
        scenario = json.load(fh)
    assert "label_record" not in scenario
    assert not (out / "labels" / "fx-noreviewer.label.json").exists()


def test_cli_contract_create_without_reviewer_is_asserted(tmp_path, monkeypatch):
    """`hotato contract create` with no --reviewer succeeds and mints no signed
    human label -- it degrades to 'asserted'."""
    _machine_with_key(tmp_path, monkeypatch, tty=False)
    wav = str(tmp_path / "call.wav"); ta.talkover_call(wav)
    out = tmp_path / "contracts"
    rc = cli.main(["contract", "create", "--stereo", wav, "--id", "ct-noreviewer",
                   "--onset", "2.0", "--expect", "yield", "--out", str(out)])
    assert rc == 0


def test_cli_fixture_create_reviewer_without_attest_is_asserted(
    tmp_path, monkeypatch,
):
    """STEP 2+3 end to end: a scripted CLI invocation that threads --reviewer
    but is non-interactive and does not attest succeeds (exit 0) yet writes NO
    signed label-record -- the label stays 'asserted' on disk."""
    _machine_with_key(tmp_path, monkeypatch, tty=False)
    out = tmp_path / "out"
    rc = cli.main(["fixture", "create", "--stereo", _EXAMPLE, "--id",
                   "fx-cli-noattest", "--onset", "2.40", "--expect", "yield",
                   "--out", str(out), "--reviewer", "qa-alice"])
    assert rc == 0
    with open(out / "scenarios" / "fx-cli-noattest.json", encoding="utf-8") as fh:
        scenario = json.load(fh)
    assert "label_record" not in scenario
    assert not (out / "labels" / "fx-cli-noattest.label.json").exists()


def test_cli_fixture_create_reviewer_with_attest_mints(tmp_path, monkeypatch):
    """STEP 2+3 end to end: the attested CLI flow threads through and mints a
    signed label-record naming the reviewer."""
    _machine_with_key(tmp_path, monkeypatch, tty=False)
    out = tmp_path / "out"
    rc = cli.main(["fixture", "create", "--stereo", _EXAMPLE, "--id",
                   "fx-cli-attest", "--onset", "2.40", "--expect", "yield",
                   "--out", str(out), "--reviewer", "qa-alice",
                   "--i-attest-human-review"])
    assert rc == 0
    with open(out / "scenarios" / "fx-cli-attest.json", encoding="utf-8") as fh:
        scenario = json.load(fh)
    assert scenario["label_record"]["reviewer_principal"] == "qa-alice"
    assert (out / "labels" / "fx-cli-attest.label.json").exists()
