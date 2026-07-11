"""The typed fix catalogue (plan 9.3): every entry is complete, typed, and
traces field-for-field back to the fixplan/fixmap source tables -- it invents
no knob of its own."""

from hotato import fixmap, fixplan
from hotato.fleet import catalogue
from hotato.fleet.catalogue import (
    SCHEMA_VERSION,
    build_catalogue,
    catalogue_for,
    lookup,
)

_REQUIRED = catalogue._REQUIRED_FIELDS


def _entries():
    return build_catalogue()["entries"]


def test_catalogue_is_versioned():
    cat = build_catalogue()
    assert cat["schema_version"] == SCHEMA_VERSION == "1"
    assert isinstance(cat["entries"], list) and cat["entries"]


def test_one_entry_per_fixplan_knob():
    # The catalogue covers exactly the fixplan knob table, nothing more.
    expected = {
        (stack, intent)
        for stack, intents in fixplan._KNOBS.items()
        for intent in intents
    }
    got = {(e["stack"], e["intent"]) for e in _entries()}
    assert got == expected


def test_every_entry_has_all_required_fields():
    for e in _entries():
        for field in _REQUIRED:
            assert field in e, f"{e['stack']}/{e['intent']} missing {field}"


def test_version_range_type_and_flags_present():
    for e in _entries():
        # supported version range: best-effort string, "unverified" today.
        assert isinstance(e["supported_version_range"], str)
        assert e["supported_version_range"]
        # data type inferred from the step.
        assert e["data_type"] in ("int", "float")
        # booleans present and actually boolean.
        for flag in (
            "inspection_required",
            "clone_application_supported",
            "rollback_supported",
            "adjacent_steps_safe",
        ):
            assert isinstance(e[flag], bool)
        # inspection is always required (the from/to needs the current value).
        assert e["inspection_required"] is True


def test_data_type_matches_step():
    for e in _entries():
        step = e["safe_discrete_step"]
        if isinstance(step, int) and not isinstance(step, bool):
            assert e["data_type"] == "int"
        else:
            assert e["data_type"] == "float"


def test_values_trace_back_to_fixplan_no_invented_knobs():
    for e in _entries():
        knob = fixplan._KNOBS[e["stack"]][e["intent"]]
        # vendor field path, bounds, step, direction: verbatim from fixplan.
        assert e["vendor_field_path"] == knob["field"]
        assert e["documented_bounds"] == list(knob["bounds"])
        assert e["safe_discrete_step"] == knob["step"]
        assert e["directional_effect"]["direction"] == knob["direction"]
        # canonical semantic family == the fixplan source tuple.
        section, key = knob["source"]
        assert e["canonical_semantic_family"] == {"section": section, "key": key}
        # provenance is the fixplan basis, verbatim.
        assert e["documentation_provenance"] == knob["basis"]
        # opposite-risk effect == fixplan._RISKS, verbatim (no paraphrase).
        assert e["expected_opposite_risk"] == fixplan._RISKS[e["intent"]]


def test_vendor_parameter_reference_traces_to_fixmap_or_is_none():
    for e in _entries():
        ref = e["vendor_parameter_reference"]
        fixmap_entry = (fixmap._KNOBS.get(e["stack"]) or {}).get(e["intent"])
        if fixmap_entry is not None:
            assert ref == fixmap_entry["parameter"]
        else:
            assert ref is None  # no fabrication where fixmap has no counterpart


def test_clone_application_flag_by_stack():
    # Hosted platforms (vapi/retell) support clone application + rollback;
    # source-config frameworks (livekit/pipecat) do not.
    by_stack = {}
    for e in _entries():
        by_stack.setdefault(e["stack"], set()).add(e["clone_application_supported"])
    assert by_stack["vapi"] == {True}
    assert by_stack["retell"] == {True}
    assert by_stack["livekit"] == {False}
    assert by_stack["pipecat"] == {False}
    # rollback tracks clone application in this release.
    for e in _entries():
        assert e["rollback_supported"] == e["clone_application_supported"]


def test_last_verified_date_and_adjacent_safe_track_documented_bounds():
    for e in _entries():
        if e["stack"] in ("vapi", "retell"):
            # dated docs basis -> a last-verified date and adjacent-safe.
            assert e["last_verified_date"] == "2026-07-06"
            assert e["adjacent_steps_safe"] is True
        else:  # livekit / pipecat: undated working ranges.
            assert e["last_verified_date"] is None
            assert e["adjacent_steps_safe"] is False


def test_lookup_and_catalogue_for():
    e = lookup("vapi", "more_sensitive")
    assert e is not None
    assert e["stack"] == "vapi" and e["intent"] == "more_sensitive"
    assert lookup("VAPI", "more_sensitive") == e  # case-insensitive
    assert lookup("nope", "more_sensitive") is None

    vapi = catalogue_for("vapi")
    assert vapi and all(x["stack"] == "vapi" for x in vapi)
    assert len(vapi) == len(fixplan._KNOBS["vapi"])


def test_build_catalogue_is_deterministic():
    assert build_catalogue() == build_catalogue()
