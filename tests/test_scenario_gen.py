"""``hotato scenario generate``: a scenario.v1 suite from a system prompt.

Pins the properties that are the point of the feature, not its implementation:
what the extraction rules recover from a realistic prompt; that two runs of the
generator over the same prompt are BYTE-identical (the claim a competitor's
model-generated suite cannot make); that every file it writes passes the REAL
scenario.v1 validator (and the shipped JSON Schema, where jsonschema is
installed) and the `scenario validate` command; that the shipped failure
taxonomy is actually crossed in; that `--max` truncates without collapsing onto
one axis; and that the tool never claims the suite is complete.
"""

import json
from importlib import resources

import pytest

from hotato import cli
from hotato import scenario as SC
from hotato import scenario_gen as GEN

PROMPT = """\
You are Ada, the phone assistant for Fernwood Pharmacy.

You can refill a prescription, check the status of an order, transfer a prescription
from another pharmacy, and schedule a vaccination appointment.

Always ask for the prescription number before you start a refill. You must also
confirm the date of birth on file, and collect a callback phone number so the
pharmacist can reach the caller if there is a problem.

Never give medical advice. Do not discuss another person's prescriptions. You must not
quote a price for a medication that is not on the caller's own profile.

Keep answers short and friendly. If the caller sounds distressed, offer to transfer
them to a pharmacist.
"""


def _write_prompt(tmp_path, text=PROMPT):
    p = tmp_path / "agent-prompt.txt"
    p.write_text(text, encoding="utf-8")
    return str(p)


# --- extraction -------------------------------------------------------------

def test_extraction_recovers_capabilities_data_and_refusals():
    e = GEN.extract(PROMPT)
    phrases = [c["phrase"] for c in e["capabilities"]]
    # all four verbs of the one "you can ..." clause, including the last one,
    # which the prompt's hard wrap splits across two lines
    assert phrases == [
        "refill prescription",
        "check status of order",
        "transfer prescription from another pharmacy",
        "schedule vaccination appointment",
    ]
    assert [d["key"] for d in e["required_data"]] == [
        "prescription_number", "date_of_birth", "callback_phone_number",
    ]
    assert e["refusals"][:2] == [
        "give medical advice", "discuss another person's prescriptions",
    ]
    assert len(e["refusals"]) == 3


def test_extraction_ignores_a_prompt_that_claims_nothing():
    e = GEN.extract("You are a friendly assistant. Keep answers short.")
    assert e["capabilities"] == []


def test_generate_refuses_a_prompt_with_no_capability(tmp_path):
    with pytest.raises(ValueError) as exc:
        GEN.generate_suite("You are a friendly assistant. Be nice.")
    assert "no capabilities" in str(exc.value)


def test_hard_wrapped_and_bulleted_prompts_reach_the_same_capabilities():
    wrapped = ("You can book an appointment,\ncancel an appointment, and "
               "reschedule an appointment.")
    bulleted = ("You can:\n- book an appointment\n- cancel an appointment\n"
                "- reschedule an appointment\n")
    got = [c["slug"] for c in GEN.extract(wrapped)["capabilities"]]
    assert got == ["book-appointment", "cancel-appointment",
                   "reschedule-appointment"]
    # a colon-introduced bullet list is the same claim in another shape
    assert got == [c["slug"] for c in GEN.extract(bulleted)["capabilities"]]


def test_a_shared_object_is_inherited_by_every_verb_in_the_clause():
    caps = GEN.extract("You can book, cancel or reschedule appointments.")["capabilities"]
    assert [c["object"] for c in caps] == ["appointments"] * 3


# --- determinism ------------------------------------------------------------

def test_two_runs_over_one_prompt_are_byte_identical(tmp_path):
    src = _write_prompt(tmp_path)
    a, b = tmp_path / "a", tmp_path / "b"
    assert cli.main(["scenario", "generate", "--prompt", src, "--out", str(a)]) == 0
    assert cli.main(["scenario", "generate", "--prompt", src, "--out", str(b)]) == 0
    names_a = sorted(p.name for p in a.iterdir())
    names_b = sorted(p.name for p in b.iterdir())
    assert names_a == names_b and names_a
    for name in names_a:
        assert (a / name).read_bytes() == (b / name).read_bytes()


def test_a_reflowed_prompt_keeps_the_same_ids(tmp_path):
    # normalisation absorbs cosmetic edits, so re-indenting a prompt does not
    # churn every id in a committed suite
    reflowed = "\n".join("   " + line for line in PROMPT.split("\n"))
    ids = lambda text: [d["id"] for _, d in GEN.generate_suite(text)["scenarios"]]
    assert ids(PROMPT) == ids(reflowed)


def test_editing_the_prompt_changes_the_ids(tmp_path):
    # the other half of the contract: a real prompt change IS visible in the diff
    edited = PROMPT.replace("schedule a vaccination appointment",
                            "schedule a flu shot")
    before = {d["id"] for _, d in GEN.generate_suite(PROMPT)["scenarios"]}
    after = {d["id"] for _, d in GEN.generate_suite(edited)["scenarios"]}
    assert before != after


def test_no_clock_or_randomness_reaches_the_output():
    suite = GEN.generate_suite(PROMPT)
    seeds = [d["seed"] for _, d in suite["scenarios"]]
    assert all(isinstance(s, int) and 0 <= s < 2 ** 31 for s in seeds)
    assert len(set(seeds)) == len(seeds)


# --- what it writes ---------------------------------------------------------

def test_every_generated_file_passes_the_real_validator(tmp_path):
    src = _write_prompt(tmp_path)
    out = tmp_path / "scn"
    assert cli.main(["scenario", "generate", "--prompt", src, "--out", str(out)]) == 0
    files = sorted(out.glob("*.scenario.json"))
    assert len(files) == 44  # 4 capabilities x 11 failure modes
    for f in files:
        doc = SC.load_scenario_file(str(f))
        assert doc["kind"] == "hotato.scenario"
        assert "overall_score" not in json.dumps(doc)
    # and through the command a user would actually run on the directory
    assert cli.main(["scenario", "validate", str(out)]) == 0


def test_generated_files_validate_against_the_shipped_json_schema(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(
        resources.files("hotato").joinpath("schema", "scenario.v1.json")
        .read_text(encoding="utf-8")
    )
    for _, doc in GEN.generate_suite(PROMPT)["scenarios"]:
        jsonschema.validate(doc, schema)


def test_the_caller_holds_the_extracted_ground_truth_and_only_its_own_turns():
    _, doc = GEN.generate_suite(PROMPT)["scenarios"][0]
    assert set(doc["facts"]) == {"prescription_number", "date_of_birth",
                                 "callback_phone_number"}
    # the caller volunteers each fact only when asked for it -- whether the
    # agent asks at all is the thing under test
    gated = {t["when_agent_asks"]: t["say"] for t in doc["caller"]["script"]
             if "when_agent_asks" in t}
    assert set(gated) == set(doc["facts"])
    for key, value in doc["facts"].items():
        assert value in gated[key]
    # a scenario can never put words in the agent's mouth
    assert all(set(t) <= {"say", "when_agent_asks", "after"}
               for t in doc["caller"]["script"])


def test_the_suite_crosses_the_whole_failure_taxonomy():
    suite = GEN.generate_suite(PROMPT)
    modes = {d["generated_from"]["failure_mode"] for _, d in suite["scenarios"]}
    # the seven shipped corpus classes ...
    assert {"backchannel-multilingual", "leading-edge-onset",
            "mid-utterance-pause", "noise-hold", "structured-utterance",
            "telephony-degraded", "browser-telephony-parity"} <= modes
    # ... plus the four turn-taking failure kinds the scorer measures
    assert {"barge-in", "dead-air", "latency-spike", "echo"} <= modes
    caps = {d["generated_from"]["capability"] for _, d in suite["scenarios"]}
    assert len(caps) == 4
    assert len(suite["scenarios"]) == 4 * len(GEN.FAILURE_MODES)


def test_each_failure_mode_is_actually_exercised_by_the_caller():
    by_mode = {d["generated_from"]["failure_mode"]: d
               for _, d in GEN.generate_suite(PROMPT)["scenarios"]}
    # a barge-in scenario declares an interruption the simulator must render
    assert by_mode["barge-in"]["caller"]["behavior"]["interruptions"]
    # a backchannel scenario has the caller acknowledge over agent speech
    bc = by_mode["backchannel-multilingual"]
    assert bc["caller"]["behavior"]["backchannels"]["probability"] == 1.0
    assert "mhm" in json.dumps(bc["caller"]["script"])
    # and the degraded-line scenarios declare the perturbation in environment
    assert by_mode["telephony-degraded"]["environment"]["codec"] == "g711_ulaw"
    assert by_mode["noise-hold"]["environment"]["noise"] == "cafe"
    # every scenario expands further through a variation matrix
    assert all(d["variation_matrix"] for d in by_mode.values())


def test_stated_constraints_are_each_probed_somewhere_in_the_suite():
    suite = GEN.generate_suite(PROMPT)
    probed = {d["generated_from"]["constraint_probed"]
              for _, d in suite["scenarios"]}
    assert set(suite["refusals"]) <= probed


def test_max_truncates_and_still_spans_both_axes():
    full = GEN.generate_suite(PROMPT)
    small = GEN.generate_suite(PROMPT, max_scenarios=6)
    assert len(small["scenarios"]) == 6
    caps = {d["generated_from"]["capability"] for _, d in small["scenarios"]}
    modes = {d["generated_from"]["failure_mode"] for _, d in small["scenarios"]}
    assert len(caps) > 1 and len(modes) > 1
    # a truncated suite is a PREFIX of the full one, so raising --max adds
    # files instead of rewriting the ones already reviewed
    assert [n for n, _ in small["scenarios"]] == [
        n for n, _ in full["scenarios"]][:6]


def test_max_below_one_is_a_usage_error(tmp_path):
    src = _write_prompt(tmp_path)
    assert cli.main(["scenario", "generate", "--prompt", src,
                     "--out", str(tmp_path / "o"), "--max", "0"]) == 2


def test_a_missing_prompt_file_is_a_usage_error(tmp_path):
    assert cli.main(["scenario", "generate", "--prompt",
                     str(tmp_path / "nope.txt"),
                     "--out", str(tmp_path / "o")]) == 2


def test_stack_is_recorded_on_the_environment():
    suite = GEN.generate_suite(PROMPT, stack="livekit")
    assert all(d["environment"]["stack"] == "livekit"
               for _, d in suite["scenarios"])
    assert all("stack" not in d["environment"]
               for _, d in GEN.generate_suite(PROMPT)["scenarios"])


# --- honesty ----------------------------------------------------------------

def test_the_summary_says_what_it_did_and_claims_no_completeness(capsys, tmp_path):
    src = _write_prompt(tmp_path)
    assert cli.main(["scenario", "generate", "--prompt", src,
                     "--out", str(tmp_path / "o")]) == 0
    out = capsys.readouterr().out.lower()
    assert "4 capabilities" in out
    assert "44 scenario file(s)" in out
    assert "starting point you edit" in out
    for claim in ("exhaustive", "complete coverage", "fully tested",
                  "your agent is", "guarantee"):
        assert claim not in out


def test_every_file_carries_its_provenance_and_its_caveat():
    for _, doc in GEN.generate_suite(PROMPT)["scenarios"]:
        prov = doc["generated_from"]
        assert prov["prompt_sha256"] and prov["capability_source"]
        assert "starting point" in prov["note"]


def test_json_output_is_machine_readable(capsys, tmp_path):
    src = _write_prompt(tmp_path)
    assert cli.main(["scenario", "generate", "--prompt", src, "--out",
                     str(tmp_path / "o"), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "scenario-generate"
    assert len(payload["scenarios"]) == 44
    assert payload["capabilities"][0] == "refill prescription"
