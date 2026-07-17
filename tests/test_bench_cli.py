"""``hotato bench run/verify``: the frozen, byte-reproducible bench.

Pinned here:

  - `bench run` on the packaged battery emits the documented result shape:
    pass counts, per-signal ms-error distributions, the four confusion cells,
    the suite content hash (the freeze pin), and the result's own canonical
    sha256 address, with NO `overall_score` key anywhere at any depth;
  - two runs of the same battery are byte-reproducible (identical content
    hashes);
  - `bench verify` re-executes the pinned battery and exits 0 on an
    untampered result;
  - a result edited in place (body changed, stored hash kept) is REFUSED
    (exit 2, tampered), and a result whose body AND hash were both rewritten
    is caught by re-execution instead (exit 1, does not reproduce);
  - a result pinning an unknown suite, or a different frozen battery, is
    refused (exit 2);
  - the open spec's contract schema (spec/contract.schema.json) is
    byte-identical to the shipped schema of record
    (src/hotato/schema/contract.v1.json).
"""

import json
import os

from hotato import bench, cli

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ERROR_SIGNALS = ("onset_sec", "time_to_yield_sec", "response_gap_sec")
CONFUSION_CELLS = ("correct_yield", "missed_yield", "false_yield", "correct_hold")


def _run_bundled(tmp_path, name="result.json"):
    out = tmp_path / name
    assert cli.main(["bench", "run", "--out", str(out)]) == 0
    with open(out, encoding="utf-8") as fh:
        return out, json.load(fh)


def _walk_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_keys(v)


def _rewrite(path, doc):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


# --- bench run: the documented result shape --------------------------------

def test_bench_run_bundled_emits_the_documented_result_shape(tmp_path):
    _, result = _run_bundled(tmp_path)

    assert result["tool"] == "hotato"
    assert result["kind"] == "hotato.bench-result"
    assert result["schema_version"] == "1"
    assert result["bench_version"] == bench.BENCH_VERSION

    suite = result["suite"]
    assert suite["name"] == "bundled"
    assert suite["source"] == "package"
    assert suite["scenarios"] == 8
    assert suite["content_hash"].startswith("sha256:")

    pc = result["pass_counts"]
    assert set(pc) == {"scenarios", "passed", "failed", "not_scorable"}
    assert pc["scenarios"] == pc["passed"] + pc["failed"] + pc["not_scorable"]

    for sig in ERROR_SIGNALS:
        stats = result["error_stats_ms"][sig]
        assert set(stats) == {"n", "median_ms", "mean_ms", "max_ms", "min_ms"}
    assert set(result["confusion"]) == set(CONFUSION_CELLS)
    assert result["confusion_off_diagonal"] == (
        result["confusion"]["missed_yield"] + result["confusion"]["false_yield"]
    )

    # the honesty invariant, at every depth: no blended score, ever
    assert "overall_score" not in set(_walk_keys(result))

    # the embedded address is the canonical hash of the body it rides on
    assert result["content_hash"] == bench.result_content_hash(result)


def test_bench_run_is_byte_reproducible(tmp_path):
    _, first = _run_bundled(tmp_path, "a.json")
    _, second = _run_bundled(tmp_path, "b.json")
    assert first == second
    assert first["content_hash"] == second["content_hash"]


def test_bench_run_stdout_is_the_result_json(tmp_path, capsys):
    assert cli.main(["bench", "run"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["kind"] == "hotato.bench-result"


def test_bench_run_unknown_suite_refuses(capsys):
    assert cli.main(["bench", "run", "--suite", "no-such-battery"]) == 2
    err = capsys.readouterr().err
    assert "error:" in err and "no-such-battery" in err


# --- bench verify: re-execution + hash comparison --------------------------

def test_bench_verify_passes_on_an_untampered_result(tmp_path, capsys):
    out, _ = _run_bundled(tmp_path)
    assert cli.main(["bench", "verify", str(out)]) == 0
    assert "verified" in capsys.readouterr().out


def test_bench_verify_refuses_a_tampered_result(tmp_path, capsys):
    out, result = _run_bundled(tmp_path)
    result["pass_counts"]["passed"] = 99  # edit the body, keep the stored hash
    _rewrite(out, result)
    assert cli.main(["bench", "verify", str(out)]) == 2
    err = capsys.readouterr().err
    assert "error:" in err and "tampered" in err


def test_bench_verify_catches_a_rehashed_edit_by_reexecution(tmp_path, capsys):
    out, result = _run_bundled(tmp_path)
    result["pass_counts"]["passed"] = 7
    result["pass_counts"]["failed"] = 1
    result["content_hash"] = bench.result_content_hash(result)
    _rewrite(out, result)
    assert cli.main(["bench", "verify", str(out)]) == 1
    text = capsys.readouterr().out
    assert "MISMATCH" in text and "pass_counts" in text


def test_bench_verify_refuses_an_unknown_suite(tmp_path, capsys):
    out, result = _run_bundled(tmp_path)
    result["suite"]["name"] = "no-such-battery"
    result["content_hash"] = bench.result_content_hash(result)
    _rewrite(out, result)
    assert cli.main(["bench", "verify", str(out)]) == 2
    assert "no-such-battery" in capsys.readouterr().err


def test_bench_verify_refuses_a_different_frozen_battery(tmp_path, capsys):
    out, result = _run_bundled(tmp_path)
    result["suite"]["content_hash"] = "sha256:" + "0" * 64
    result["content_hash"] = bench.result_content_hash(result)
    _rewrite(out, result)
    assert cli.main(["bench", "verify", str(out)]) == 2
    assert "pins" in capsys.readouterr().err


def test_bench_verify_refuses_a_smuggled_overall_score(tmp_path, capsys):
    out, result = _run_bundled(tmp_path)
    result["pass_counts"]["overall_score"] = 0.99
    result["content_hash"] = bench.result_content_hash(result)
    _rewrite(out, result)
    assert cli.main(["bench", "verify", str(out)]) == 2
    assert "overall_score" in capsys.readouterr().err


def test_bench_verify_refuses_a_non_result_file(tmp_path, capsys):
    path = tmp_path / "not-a-result.json"
    path.write_text('{"kind": "something-else"}', encoding="utf-8")
    assert cli.main(["bench", "verify", str(path)]) == 2
    assert "error:" in capsys.readouterr().err


# --- the open spec stays in lockstep with the shipped schema ---------------

def test_spec_contract_schema_is_byte_identical_to_the_shipped_schema():
    def _read(*parts):
        with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
            return fh.read()

    shipped = _read("src", "hotato", "schema", "contract.v1.json")
    spec = _read("spec", "contract.schema.json")
    assert spec == shipped, (
        "spec/contract.schema.json must be a byte-identical copy of the "
        "shipped schema of record (src/hotato/schema/contract.v1.json); "
        "update them together"
    )
