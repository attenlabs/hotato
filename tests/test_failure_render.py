"""Failure Record renderers + `hotato record render` CLI.

Pinned here:

  * golden-file bytes for all four formats (regenerate intentionally with
    the snippet in ``_build_golden_record``'s docstring after a reviewed
    contract change);
  * every format derives from the ONE canonical record and carries the same
    content-addressed record_id;
  * hostile strings (script tags, quotes, unicode, very long labels) are
    escaped/truncated, and the HTML/SVG stay inert (no script, no remote
    asset);
  * rendering is offline (a network attempt fails the test) and
    byte-deterministic (double render identical);
  * the installed CLI renders end to end on a temp source, refuses an
    all-pass source with a no-failure diagnostic, and reports zero/multiple
    selector matches as distinct exit-2 errors.
"""

import json
import os
import re
import socket
import subprocess
import sys

import pytest

from hotato import cli
from hotato import failure_record as FR
from hotato import failure_render as FRR
from tests._failure_sources import det_row, make_test_run

GOLDEN_DIR = os.path.join(os.path.dirname(__file__), "golden", "failure_record")
FORMATS = ("failure-record.json", "failure-record.md",
           "failure-record.html", "failure-record.svg")


def _build_golden_record():
    """The pinned golden record. To regenerate the goldens after a REVIEWED
    contract change, run from the repo root::

        PYTHONPATH=src:. python3 -c "
        import tests.test_failure_render as t, os
        from hotato import failure_render as FRR
        r = t._build_golden_record()
        for n, c in FRR.render_all(r).items():
            open(os.path.join(t.GOLDEN_DIR, n), 'w', encoding='utf-8',
                 newline='\\n').write(c)
        "
    """
    original = FR.__version__
    FR.__version__ = "0.0.0-golden"
    try:
        rows = [
            det_row("refund-issued", "tool_call", "FAIL", dimension="outcome",
                    reason="expected a refund.create tool call; none was "
                           "found in the trace"),
            det_row("disclosure-present", "policy", "PASS",
                    dimension="policy"),
            det_row("yield-latency", "latency", "PASS",
                    dimension="conversation"),
            det_row("speech-latency", "latency", "INCONCLUSIVE",
                    dimension="speech",
                    reason="no timing context was provided for a latency "
                           "field"),
        ]
        return FR.project(make_test_run(rows))
    finally:
        FR.__version__ = original


@pytest.fixture()
def golden_record():
    return _build_golden_record()


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """No renderer may touch the network."""
    def guard(*args, **kwargs):
        raise AssertionError("network attempted while rendering a record")
    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket, "create_connection", guard)


# --------------------------------------------------------------------------
# golden files
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", FORMATS)
def test_golden_bytes(golden_record, name):
    with open(os.path.join(GOLDEN_DIR, name), encoding="utf-8") as fh:
        expected = fh.read()
    assert FRR.render_all(golden_record)[name] == expected


def test_double_render_is_byte_identical(golden_record):
    assert FRR.render_all(golden_record) == FRR.render_all(golden_record)


# --------------------------------------------------------------------------
# cross-format consistency
# --------------------------------------------------------------------------

def test_every_format_carries_the_same_record_id(golden_record):
    outputs = FRR.render_all(golden_record)
    for name in FORMATS:
        assert golden_record["record_id"] in outputs[name], name
    parsed = json.loads(outputs["failure-record.json"])
    assert parsed["record_id"] == golden_record["record_id"]
    assert parsed == golden_record


def test_headline_is_the_first_visible_sentence(golden_record):
    outputs = FRR.render_all(golden_record)
    headline = golden_record["headline"]
    assert re.match(r"^\S+ failed: ", headline)
    assert outputs["failure-record.md"].splitlines()[0] == f"# {headline}"
    html_out = outputs["failure-record.html"]
    assert f"<h1>{headline}</h1>" in html_out
    body_text = re.sub(r"<[^>]+>", "\n", html_out.split("<main>", 1)[1])
    sentences = [ln for ln in (s.strip() for s in body_text.splitlines())
                 if ln.endswith(".") or " failed: " in ln]
    assert sentences and sentences[0] == headline


def test_html_and_svg_are_inert_and_offline(golden_record):
    outputs = FRR.render_all(golden_record)
    svg_ns = 'xmlns="http://www.w3.org/2000/svg"'
    remote = re.compile(r"(?:https?:)?//|url\s*\(\s*['\"]?\s*https?:",
                        re.IGNORECASE)
    for name in ("failure-record.html", "failure-record.svg"):
        content = outputs[name]
        assert not re.search(r"<script\b|javascript:|\son\w+=", content,
                             re.IGNORECASE), name
        assert not remote.search(content.replace(svg_ns, "")), name


def test_rendering_refuses_an_invalid_record(golden_record):
    broken = json.loads(json.dumps(golden_record))
    broken["overall_score"] = 0.9
    broken["record_id"] = FR.compute_record_id(broken)
    with pytest.raises(ValueError) as err:
        FRR.render_all(broken)
    assert "aggregate score is forbidden" in str(err.value)


# --------------------------------------------------------------------------
# hostile strings
# --------------------------------------------------------------------------

HOSTILE = (
    "<script>alert(\"x\")</script> & \"double\" 'single' <img src=x> "
    "καλημέρα ✓ " + "long-label-" * 60
)


def _hostile_record():
    rows = [det_row("hostile-check", "tool_call", "FAIL", dimension="outcome",
                    reason=HOSTILE)]
    return FR.project(make_test_run(rows))


def test_hostile_strings_are_escaped_everywhere():
    outputs = FRR.render_all(_hostile_record())
    for name in ("failure-record.html", "failure-record.svg"):
        content = outputs[name]
        assert "<script" not in content, name
        assert "<img" not in content, name
        assert "&lt;script&gt;" in content, name
    md = outputs["failure-record.md"]
    assert "<script" not in md
    assert "&lt;script&gt;" in md


def test_very_long_labels_are_truncated_deterministically():
    outputs = FRR.render_all(_hostile_record())
    svg = outputs["failure-record.svg"]
    for line in svg.splitlines():
        text_runs = re.findall(r">([^<>]*)</text>", line)
        for run in text_runs:
            assert len(run) <= 240, "unbounded text run in the SVG"
    assert FRR.render_all(_hostile_record()) == outputs


def test_quotes_cannot_escape_svg_attributes():
    svg = FRR.render_all(_hostile_record())["failure-record.svg"]
    for match in re.finditer(r'"([^"]*)"', svg):
        assert "<script" not in match.group(1)
    assert "&quot;double&quot;" in svg


# --------------------------------------------------------------------------
# CLI: hotato record render SOURCE[#SELECTOR] --out DIR
# --------------------------------------------------------------------------

def _write_source(tmp_path, doc, name="result.json"):
    path = tmp_path / name
    path.write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


def test_installed_cli_renders_end_to_end(tmp_path):
    src = _write_source(tmp_path, make_test_run())
    out = tmp_path / "record"
    env = dict(os.environ)
    src_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
    env["PYTHONPATH"] = src_dir + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-m", "hotato", "record", "render", src,
         "--out", str(out)],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "record_id: sha256:" in result.stdout
    files = sorted(os.listdir(out))
    assert files == sorted(FORMATS)
    record = json.loads((out / "failure-record.json").read_text("utf-8"))
    FR.validate_record(record)
    for name in FORMATS:
        assert record["record_id"] in (out / name).read_text("utf-8")
    # second render over the same source is byte-identical
    before = {name: (out / name).read_bytes() for name in FORMATS}
    rerun = subprocess.run(
        [sys.executable, "-m", "hotato", "record", "render", src,
         "--out", str(out)],
        capture_output=True, text=True, env=env,
    )
    assert rerun.returncode == 0, rerun.stderr
    assert {name: (out / name).read_bytes() for name in FORMATS} == before


def test_cli_refuses_an_all_pass_source(tmp_path, capsys):
    rows = [det_row("refund-issued", "tool_call", "PASS", dimension="outcome")]
    src = _write_source(tmp_path, make_test_run(rows, exit_code=0))
    code = cli.main(["record", "render", src, "--out", str(tmp_path / "o")])
    assert code == 2
    err = capsys.readouterr().err
    assert "no failure" in err
    assert not (tmp_path / "o").exists()


def test_cli_zero_selector_matches_is_exit_2(tmp_path, capsys):
    src = _write_source(tmp_path, make_test_run())
    code = cli.main(["record", "render", f"{src}#missing-test",
                     "--out", str(tmp_path / "o")])
    assert code == 2
    assert "matches no" in capsys.readouterr().err


def test_cli_multiple_matches_is_exit_2(tmp_path, capsys):
    from tests._failure_sources import make_suite_run, make_suite_test
    suite = make_suite_run([
        make_suite_test(tid, exit_code=1,
                        dim_counts={"conversation":
                                    {"pass": 0, "fail": 1, "inconclusive": 0}},
                        dim_reason={"conversation": "latency: too slow"})
        for tid in ("t-one", "t-two")
    ])
    src = _write_source(tmp_path, suite, name="suite-run.json")
    code = cli.main(["record", "render", src, "--out", str(tmp_path / "o")])
    assert code == 2
    err = capsys.readouterr().err
    assert "2 failing" in err and "t-one" in err


def test_cli_suite_selector_renders_the_selected_test(tmp_path, capsys):
    from tests._failure_sources import make_suite_run, make_suite_test
    suite = make_suite_run([
        make_suite_test("t-one", exit_code=1,
                        dim_counts={"conversation":
                                    {"pass": 0, "fail": 1, "inconclusive": 0}},
                        dim_reason={"conversation": "latency: too slow"}),
        make_suite_test("t-two", exit_code=0),
    ])
    src = _write_source(tmp_path, suite, name="suite-run.json")
    out = tmp_path / "o"
    code = cli.main(["record", "render", f"{src}#t-one", "--out", str(out)])
    assert code == 0
    record = json.loads((out / "failure-record.json").read_text("utf-8"))
    assert record["subject"]["test_id"] == "t-one"
    assert record["reproduction"]["argv"][3] == "suite-run.json#t-one"


def test_cli_empty_selector_is_exit_2(tmp_path, capsys):
    src = _write_source(tmp_path, make_test_run())
    code = cli.main(["record", "render", f"{src}#", "--out",
                     str(tmp_path / "o")])
    assert code == 2
    assert "empty selector" in capsys.readouterr().err


def test_cli_not_a_result_file_is_exit_2(tmp_path, capsys):
    path = tmp_path / "nope.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    code = cli.main(["record", "render", str(path),
                     "--out", str(tmp_path / "o")])
    assert code == 2
    assert "not a JSON object" in capsys.readouterr().err


def test_cli_printed_output_is_paste_safe(tmp_path, capsys):
    src = _write_source(tmp_path, make_test_run())
    code = cli.main(["record", "render", src, "--out", str(tmp_path / "o")])
    assert code == 0
    out = capsys.readouterr().out
    for line in out.splitlines():
        assert "<" not in line and ">" not in line
        assert " # " not in line
        assert not line.rstrip().endswith("\\")
