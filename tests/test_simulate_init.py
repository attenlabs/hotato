"""``hotato simulate --init`` / ``--example``: the onboarding path to a runnable
simulation from a fresh install.

The reported dead end: ``hotato scenario init`` writes a conversation-test file
that ``hotato simulate`` rejects (the two "scenario" concepts collide by name),
and the only runnable scenarios lived in ``examples/`` (NOT packaged). These
pins guarantee a new user has a working path:

* ``scenario.build_starter`` produces a MINIMAL valid ``hotato.scenario.v1`` doc
  that round-trips through the loader AND renders to a faithful (non
  SIMULATOR_INVALID) origin=simulated conversation, with no overall_score;
* the scenario bundled INSIDE the package (``--example``) is byte-identical to
  the builder and is reachable as an installed resource (ships in the wheel);
* ``simulate --init X && simulate X`` runs end to end and produces an
  origin=simulated artifact (exit 0);
* feeding a conversation-test doc to ``simulate`` raises an ACTIONABLE error that
  names both concepts and points at ``--init`` / ``--example`` (never the opaque
  ``'kind' must be 'hotato.scenario'``).
"""

import json
import os

from hotato import cli
from hotato import conversation_test as CT
from hotato import scenario as SC
from hotato import simulate as SIM

# --- the builder + packaged example -----------------------------------------

def test_build_starter_round_trips_validates_and_renders_faithfully():
    text = SC.build_starter("demo")
    # loads + validates through the REAL loader, not just parses
    doc = SC.validate_scenario_doc(SC.parse_scenario(text))
    assert doc["kind"] == "hotato.scenario"
    assert doc["id"] == "demo"
    # the caller declares only its OWN turns; no overall_score anywhere
    assert doc["caller"]["script"]
    assert "overall_score" not in text
    # and it RENDERS to a faithful origin=simulated conversation (not just valid)
    rendered = SIM.render(doc, seed=int(doc.get("seed", 0)))
    assert rendered["origin"]["kind"] == "simulated"
    verdict = SIM.validate_simulation(doc, rendered)
    assert verdict["ok"], verdict


def test_build_starter_id_defaults_when_blank():
    doc = SC.validate_scenario_doc(SC.parse_scenario(SC.build_starter("")))
    assert doc["id"] == "demo"


def test_packaged_example_matches_builder_and_is_installed():
    path = SC.example_scenario_path()
    assert os.path.isfile(path), path
    with open(path, encoding="utf-8") as fh:
        on_disk = fh.read()
    # ONE source of truth: the packaged file is exactly build_starter's bytes
    assert on_disk == SC.build_starter(SC.EXAMPLE_SCENARIO_ID)
    doc = SC.load_scenario_file(path)
    assert doc["id"] == SC.EXAMPLE_SCENARIO_ID
    rendered = SIM.render(doc, seed=int(doc.get("seed", 0)))
    assert SIM.validate_simulation(doc, rendered)["ok"]


# --- the actionable error (collision resolution) ----------------------------

def test_conversation_test_doc_rejected_with_actionable_error():
    # the exact doc `hotato scenario init` writes -- fed to the scenario loader
    ct_text = CT.build_scenario_starter("myct")
    doc = SC.parse_scenario(ct_text)
    try:
        SC.validate_scenario_doc(doc)
        assert False, "expected ValueError on a conversation-test doc"
    except ValueError as exc:
        msg = str(exc)
    # names both concepts and points at the working path -- not the opaque form
    assert "conversation-test" in msg
    assert "--init" in msg
    assert "--example" in msg
    assert "SIMULATE.md" in msg


# --- the CLI end to end ------------------------------------------------------

def test_cli_init_then_simulate_runs_end_to_end(tmp_path):
    scn = tmp_path / "demo.scenario.json"
    assert cli.main(["simulate", "--init", str(scn)]) == 0
    assert scn.is_file()
    # the init output loads through the REAL scenario loader
    doc = SC.load_scenario_file(str(scn))
    assert doc["kind"] == "hotato.scenario"
    assert doc["id"] == "demo"
    # ... and simulate renders it into an origin=simulated artifact (exit 0)
    out = tmp_path / "sim"
    code = cli.main(["simulate", str(scn), "--out", str(out)])
    assert code == 0
    manifest = json.loads((out / "conversation.json").read_text(encoding="utf-8"))
    assert manifest["origin"]["kind"] == "simulated"


def test_cli_init_id_derived_from_filename_stem(tmp_path):
    scn = tmp_path / "refund.scenario.json"
    assert cli.main(["simulate", "--init", str(scn)]) == 0
    # ".scenario" + extension stripped from the stem
    assert SC.load_scenario_file(str(scn))["id"] == "refund"


def test_cli_init_refuses_overwrite_without_force(tmp_path):
    scn = tmp_path / "demo.scenario.json"
    assert cli.main(["simulate", "--init", str(scn)]) == 0
    assert cli.main(["simulate", "--init", str(scn)]) == 2  # usage error, no clobber
    assert cli.main(["simulate", "--init", str(scn), "--force"]) == 0


def test_cli_example_runs_from_the_package(tmp_path):
    out = tmp_path / "ex"
    code = cli.main(["simulate", "--example", "--out", str(out)])
    assert code == 0
    manifest = json.loads((out / "conversation.json").read_text(encoding="utf-8"))
    assert manifest["origin"]["kind"] == "simulated"


def test_cli_example_refuses_a_stray_scenario(tmp_path):
    scn = tmp_path / "demo.scenario.json"
    cli.main(["simulate", "--init", str(scn)])
    # --example and a positional scenario together is a usage error, not a silent pick
    assert cli.main(["simulate", "--example", str(scn)]) == 2


def test_cli_simulate_on_conversation_test_is_actionable(tmp_path, capsys):
    ct = tmp_path / "ct.yaml"
    cli.main(["scenario", "init", "myct", "--out", str(ct)])
    capsys.readouterr()
    code = cli.main(["simulate", str(ct)])
    assert code == 2
    err = capsys.readouterr().err
    assert "--init" in err and "conversation-test" in err
