"""Scenario dynamic variables + branch enumeration (an additive expansion axis
on the deterministic scripted-caller simulator).

Pins the invariants that are the point of the slice:

* `variables` cross-product + `{name}` template substitution into caller `say`
  lines expand into concrete matrix cells (expansion counts);
* `branches` enumerates EVERY root-to-leaf path deterministically and in a fixed
  order (path enumeration exactness), and a diamond is NOT a cycle;
* cycles / unknown nodes / unbound variables are REFUSED up front (ValueError,
  the CLI's exit-2 path);
* the derived per-run seeds are byte-stable (two expansions identical) and
  distinct per (binding, path); a scenario with NEITHER axis is byte-identical
  to before the axes existed (its variation dict keeps exactly its 5 keys and
  never gains a `variables`/`path` key);
* e2e `hotato simulate --matrix` over a branched + variabled scenario produces
  attributable origin=simulated cells.
"""

import json

import pytest

from hotato import cli
from hotato import scenario as SC
from hotato import simulate as SIM


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

def _base(**over):
    doc = {
        "kind": "hotato.scenario", "version": 1, "id": "brancher",
        "goal": {"type": "get_refund", "target": "an order"},
        "caller": {
            "script": [{"say": "Hi, I have a question."}],
            "behavior": {"backchannels": {"probability": 0.0}},
        },
    }
    doc.update(over)
    return doc


def _write(tmp_path, doc, name="s.json"):
    p = tmp_path / name
    p.write_text(json.dumps(doc), encoding="utf-8")
    return str(p)


# --------------------------------------------------------------------------
# pure helpers: substitution, combinations, path enumeration
# --------------------------------------------------------------------------

def test_variable_references_finds_only_identifier_braces():
    assert SC.variable_references("call from {city} re {order_id}") == {"city",
                                                                        "order_id"}
    # literal braces around non-identifier text are NOT references
    assert SC.variable_references('json is {"a": 1}') == set()
    assert SC.variable_references("no refs here") == set()


def test_substitute_variables_replaces_all_refs():
    out = SC.substitute_variables("from {city} order {order_id}",
                                  {"city": "Austin", "order_id": "A-1"})
    assert out == "from Austin order A-1"
    # stray non-identifier braces are left verbatim, never mangled or raised
    assert SC.substitute_variables('raw {"k": 1}', {"k": "x"}) == 'raw {"k": 1}'


def test_variable_combinations_is_sorted_and_deterministic():
    combos = SC.variable_combinations({"city": ["A", "B"], "size": ["S", "M"]})
    # names sorted (city, size); values in declared order
    assert combos == [
        {"city": "A", "size": "S"}, {"city": "A", "size": "M"},
        {"city": "B", "size": "S"}, {"city": "B", "size": "M"},
    ]
    # empty/absent -> a single pass-through empty binding
    assert SC.variable_combinations({}) == [{}]


def test_enumerate_branch_paths_exact_order():
    branches = {
        "root": "a",
        "nodes": {
            "a": {"say": "a", "next": ["b", "c"]},
            "b": {"say": "b", "next": ["d"]},
            "c": {"say": "c"},
            "d": {"say": "d"},
        },
    }
    assert SC.enumerate_branch_paths(branches) == [["a", "b", "d"], ["a", "c"]]


def test_enumerate_branch_paths_diamond_is_not_a_cycle():
    branches = {
        "root": "a",
        "nodes": {
            "a": {"say": "a", "next": ["b", "c"]},
            "b": {"say": "b", "next": ["d"]},
            "c": {"say": "c", "next": ["d"]},
            "d": {"say": "d"},
        },
    }
    # a shared child reached by two parents yields one path per route
    assert SC.enumerate_branch_paths(branches) == [["a", "b", "d"],
                                                   ["a", "c", "d"]]


# --------------------------------------------------------------------------
# refusals: cycles / unknown nodes / unbound variables (exit 2 path)
# --------------------------------------------------------------------------

def test_branch_cycle_is_refused():
    doc = _base(branches={
        "root": "a",
        "nodes": {"a": {"say": "a", "next": ["b"]},
                  "b": {"say": "b", "next": ["a"]}},
    })
    with pytest.raises(ValueError, match="cycle"):
        SC.validate_scenario_doc(doc)


def test_branch_self_loop_is_refused():
    doc = _base(branches={
        "root": "a", "nodes": {"a": {"say": "a", "next": ["a"]}},
    })
    with pytest.raises(ValueError, match="cycle"):
        SC.validate_scenario_doc(doc)


def test_branch_unknown_node_is_refused():
    doc = _base(branches={
        "root": "a", "nodes": {"a": {"say": "a", "next": ["ghost"]}},
    })
    with pytest.raises(ValueError, match="unknown node"):
        SC.validate_scenario_doc(doc)


def test_branch_unknown_root_is_refused():
    doc = _base(branches={
        "root": "nope", "nodes": {"a": {"say": "a"}},
    })
    with pytest.raises(ValueError, match="root"):
        SC.validate_scenario_doc(doc)


def test_unbound_variable_reference_is_refused():
    doc = _base(caller={
        "script": [{"say": "calling from {city}"}],
        "behavior": {"backchannels": {"probability": 0.0}},
    })
    with pytest.raises(ValueError, match="city"):
        SC.validate_scenario_doc(doc)


def test_unbound_variable_in_branch_line_is_refused():
    doc = _base(
        variables={"city": ["Austin"]},
        branches={"root": "a",
                  "nodes": {"a": {"say": "from {city} in {region}"}}},
    )
    # region is referenced in a branch line but never declared
    with pytest.raises(ValueError, match="region"):
        SC.validate_scenario_doc(doc)


def test_malformed_variables_are_refused():
    with pytest.raises(ValueError, match="variables"):
        SC.validate_scenario_doc(_base(variables={"city": []}))  # empty list
    with pytest.raises(ValueError, match="variables"):
        SC.validate_scenario_doc(_base(variables={"bad name": ["x"]}))  # not id
    with pytest.raises(ValueError, match="strings or numbers"):
        SC.validate_scenario_doc(_base(variables={"city": [{"nested": 1}]}))


# --------------------------------------------------------------------------
# expansion counts
# --------------------------------------------------------------------------

def test_variables_expand_cross_product_count():
    doc = _base(
        caller={"script": [{"say": "from {city} size {size}"}],
                "behavior": {"backchannels": {"probability": 0.0}}},
        variables={"city": ["A", "B"], "size": ["S", "M", "L"]},
    )
    runs = SIM.expand(doc)
    assert len(runs) == 2 * 3
    # every run carries its binding and NO path (no branches declared)
    for r in runs:
        assert set(r["variation"]["variables"]) == {"city", "size"}
        assert "path" not in r["variation"]


def test_branches_expand_one_cell_per_path():
    doc = _base(branches={
        "root": "a",
        "nodes": {"a": {"say": "a", "next": ["b", "c"]},
                  "b": {"say": "b"}, "c": {"say": "c"}},
    })
    runs = SIM.expand(doc)
    assert len(runs) == 2
    assert [r["variation"]["path"] for r in runs] == [["a", "b"], ["a", "c"]]


def test_variables_and_branches_and_matrix_multiply():
    doc = _base(
        caller={"script": [{"say": "from {city}"}],
                "behavior": {"backchannels": {"probability": 0.0}}},
        variables={"city": ["A", "B"]},
        branches={"root": "r",
                  "nodes": {"r": {"say": "r", "next": ["x", "y"]},
                            "x": {"say": "x"}, "y": {"say": "y"}}},
        variation_matrix={"locale": ["en-US", "es-ES"]},
    )
    runs = SIM.expand(doc)
    # 2 cities x 2 paths x 2 locales
    assert len(runs) == 2 * 2 * 2
    # each cell is a full (binding, path, matrix) tuple
    for r in runs:
        assert set(r["variation"]) == {
            "locale", "speaking_rate", "noise", "behavior", "repetition",
            "variables", "path"}


# --------------------------------------------------------------------------
# substitution + branch lines actually land in the produced script
# --------------------------------------------------------------------------

def test_substituted_value_lands_in_transcript_and_template_does_not():
    doc = _base(
        caller={"script": [{"say": "calling from {city}"}],
                "behavior": {"backchannels": {"probability": 0.0}}},
        variables={"city": ["Austin", "Denver"]},
    )
    runs = SIM.expand(doc)
    texts = []
    for r in runs:
        produced = SIM.render(r["scenario"], r["seed"])
        joined = " ".join(s["text"] for s in produced["transcript"]["segments"])
        texts.append(joined)
        # the template brace never survives into the rendered caller line
        assert "{city}" not in joined
        # and the simulation validates as faithful (no invented agent turn etc.)
        assert SIM.validate_simulation(r["scenario"], produced)["ok"]
    assert any("Austin" in t for t in texts)
    assert any("Denver" in t for t in texts)


def test_branch_path_lines_append_to_base_script_in_order():
    doc = _base(
        caller={"script": [{"say": "base line"}],
                "behavior": {"backchannels": {"probability": 0.0}}},
        branches={"root": "a",
                  "nodes": {"a": {"say": ["a1", "a2"], "next": ["b"]},
                            "b": {"say": "b1"}}},
    )
    runs = SIM.expand(doc)
    assert len(runs) == 1  # single leaf path a>b
    script = runs[0]["scenario"]["caller"]["script"]
    assert [t["say"] for t in script] == ["base line", "a1", "a2", "b1"]
    # the concrete cell is a plain single-path scenario (axes consumed away)
    assert "branches" not in runs[0]["scenario"]
    assert "variables" not in runs[0]["scenario"]


# --------------------------------------------------------------------------
# byte-stable seeds + plain-scenario backward compatibility
# --------------------------------------------------------------------------

def test_expansion_seeds_are_byte_stable_and_distinct_per_cell():
    doc = _base(
        caller={"script": [{"say": "from {city}"}],
                "behavior": {"backchannels": {"probability": 0.0}}},
        variables={"city": ["A", "B"]},
        branches={"root": "r",
                  "nodes": {"r": {"say": "r", "next": ["x", "y"]},
                            "x": {"say": "x"}, "y": {"say": "y"}}},
    )
    a = SIM.expand(doc)
    b = SIM.expand(doc)
    # two expansions are byte-identical (seeds + variation tuples)
    assert [r["seed"] for r in a] == [r["seed"] for r in b]
    assert [r["variation"] for r in a] == [r["variation"] for r in b]
    # every (binding, path) cell got a distinct seed
    assert len({r["seed"] for r in a}) == len(a)


def test_plain_scenario_unaffected_by_new_axes():
    # a scenario with NEITHER variables NOR branches keeps exactly its 5-key
    # variation dict and never gains a variables/path key -- the byte-identical
    # backward-compat guarantee.
    doc = _base(variation_matrix={"locale": ["en-US", "es-ES"],
                                  "repetitions": 2})
    runs = SIM.expand(doc)
    assert len(runs) == 4
    for r in runs:
        assert set(r["variation"]) == {
            "locale", "speaking_rate", "noise", "behavior", "repetition"}


# --------------------------------------------------------------------------
# e2e: hotato simulate --matrix over a branched + variabled scenario
# --------------------------------------------------------------------------

def test_cli_matrix_over_branched_variabled_scenario(tmp_path, capsys):
    doc = _base(
        id="brancher-e2e",
        caller={"script": [{"say": "Hi, from {city}."}],
                "behavior": {"backchannels": {"probability": 0.0}}},
        variables={"city": ["Austin", "Denver"]},
        branches={"root": "ask",
                  "nodes": {"ask": {"say": "Refund?", "next": ["insist", "ok"]},
                            "insist": {"say": ["No.", "Money back."]},
                            "ok": {"say": "Credit is fine."}}},
    )
    s = _write(tmp_path, doc, "e2e.json")
    out = tmp_path / "matrix"
    code = cli.main(["simulate", "--matrix", s, "--out", str(out),
                     "--format", "json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "simulate-matrix"
    # 2 cities x 2 paths = 4 cells, all valid, all simulated
    assert payload["counts"]["runs"] == 4
    assert payload["counts"]["valid"] == 4
    assert payload["all_simulated"] is True
    assert "overall_score" not in json.dumps(payload)
    # every cell is attributable to its binding + path
    for cell in payload["variation_cells"]:
        assert "variables" in cell["cell"] and "path" in cell["cell"]
    paths = {tuple(c["cell"]["path"]) for c in payload["variation_cells"]}
    assert paths == {("ask", "insist"), ("ask", "ok")}

    # each produced conversation is a verifiable origin=simulated artifact
    run_dirs = sorted(p for p in out.iterdir() if p.is_dir())
    assert len(run_dirs) == 4
    manifest = json.loads(
        (run_dirs[0] / "conversation.json").read_text(encoding="utf-8"))
    assert manifest["origin"]["kind"] == "simulated"


def test_cli_matrix_cycle_scenario_is_exit_2(tmp_path):
    doc = _base(branches={
        "root": "a",
        "nodes": {"a": {"say": "a", "next": ["b"]},
                  "b": {"say": "b", "next": ["a"]}},
    })
    s = _write(tmp_path, doc, "cycle.json")
    assert cli.main(["simulate", "--matrix", s]) == 2


def test_cli_simulate_unbound_variable_is_exit_2(tmp_path):
    doc = _base(caller={"script": [{"say": "from {city}"}],
                        "behavior": {"backchannels": {"probability": 0.0}}})
    s = _write(tmp_path, doc, "unbound.json")
    assert cli.main(["simulate", s]) == 2
