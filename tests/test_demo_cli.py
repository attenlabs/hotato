"""The demo-first conversion surface.

Pins: ``python -m hotato`` works as a module entry point; ``hotato demo`` runs
the packaged, intentionally failing battery (exit 0 by default because the
failures are the point, exit 1 with --fail), prints both fix classes, writes
the self-contained HTML report; and the packaged demo data resolves via
importlib.resources from a real installed (wheel-extracted) layout, which is
what exercises the pyproject package-data globs. Also pins the new
human-readable default for ``hotato run`` (json stays one flag away).
"""

import json
import os
import subprocess
import sys
import zipfile

import pytest

from hotato import cli

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")


def _env_with_path(path):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(path) + os.pathsep + env.get("PYTHONPATH", "")
    return env


# --- python -m hotato -------------------------------------------------------

def test_python_dash_m_hotato_version_exits_0():
    proc = subprocess.run(
        [sys.executable, "-m", "hotato", "--version"],
        capture_output=True, text=True, env=_env_with_path(SRC), cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    assert "hotato" in proc.stdout


# --- hotato demo ------------------------------------------------------------

def test_demo_exits_0_and_prints_failures_and_both_fix_classes(tmp_path, capsys):
    out = tmp_path / "demo.html"
    code = cli.main(["demo", "--no-open", "--out", str(out)])
    # Failures are intentional, so the default exit never breaks a script.
    assert code == 0
    text = capsys.readouterr().out
    assert "hotato demo: recorded calls a provider's default agent fails" in text
    assert "0/2 events pass  (failed=2)" in text
    assert "[FAIL]" in text
    assert "fd-01-missed-interruption" in text
    assert "fd-02-backchannel-yielded" in text
    assert "fix[config]" in text
    assert "fix[engagement-control]" in text
    assert "recorded calls" in text
    assert f"report: {out}" in text
    html = out.read_text(encoding="utf-8")
    assert "fd-01-missed-interruption" in html
    assert "fd-02-backchannel-yielded" in html


def test_demo_fail_flag_exits_with_the_real_regression_code(tmp_path):
    out = tmp_path / "demo.html"
    assert cli.main(["demo", "--no-open", "--fail", "--out", str(out)]) == 1


def test_demo_format_json_emits_the_envelope(tmp_path, capsys):
    out = tmp_path / "demo.html"
    code = cli.main(["demo", "--no-open", "--format", "json", "--out", str(out)])
    assert code == 0
    env = json.loads(capsys.readouterr().out)  # stdout is the pure envelope
    assert env["tool"] == "hotato"
    assert env["summary"] == {"events": 2, "passed": 0, "failed": 2,
                              "regression": True}
    assert env["exit_code"] == 1
    assert {e["scenario_id"] for e in env["events"]} == {
        "fd-01-missed-interruption", "fd-02-backchannel-yielded"}
    assert {f["fix_class"] for f in env["fix_map"]} == {
        "config", "engagement-control"}
    assert env["funnel"] is not None  # both axes fail, the pointer fires


# --- packaged data from an installed layout ---------------------------------

@pytest.fixture(scope="module")
def wheel_extract_dir(tmp_path_factory):
    """Build the wheel and unzip it: the closest offline stand-in for a real
    ``pip install`` layout, and the only thing that exercises the pyproject
    package-data globs."""
    tmp = tmp_path_factory.mktemp("wheel")
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps",
         "--no-build-isolation", "-w", str(tmp), ROOT],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    wheels = sorted(tmp.glob("hotato-*.whl"))
    assert wheels, list(tmp.iterdir())
    extract = tmp / "unpacked"
    with zipfile.ZipFile(wheels[-1]) as zf:
        zf.extractall(extract)
    return extract


def test_demo_data_ships_in_the_wheel(wheel_extract_dir):
    demo = wheel_extract_dir / "hotato" / "data" / "demo" / "failing"
    assert sorted(p.name for p in (demo / "scenarios").iterdir()) == [
        "fd-01-missed-interruption.json", "fd-02-backchannel-yielded.json"]
    assert sorted(p.name for p in (demo / "audio").iterdir()) == [
        "fd-01-missed-interruption.example.wav",
        "fd-02-backchannel-yielded.example.wav"]


def test_demo_resolves_packaged_data_from_installed_layout(wheel_extract_dir,
                                                           tmp_path):
    out = tmp_path / "demo.html"
    # cwd is neutral so the repo checkout cannot shadow the wheel layout.
    proc = subprocess.run(
        [sys.executable, "-m", "hotato", "demo", "--no-open", "--out", str(out)],
        capture_output=True, text=True,
        env=_env_with_path(wheel_extract_dir), cwd=str(tmp_path),
    )
    assert proc.returncode == 0, proc.stderr
    assert "fix[config]" in proc.stdout
    assert "fix[engagement-control]" in proc.stdout
    html = out.read_text(encoding="utf-8")
    assert "fd-01-missed-interruption" in html
    assert "fd-02-backchannel-yielded" in html


# --- run: human-readable default, json one flag away -------------------------

def test_run_default_output_is_human_readable_text(capsys):
    code = cli.main(["run", "--suite", "barge-in"])
    assert code == 0
    out = capsys.readouterr().out
    assert out.lstrip().startswith("hotato [suite]")
    assert "events pass" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_run_format_json_still_emits_the_envelope(capsys):
    code = cli.main(["run", "--suite", "barge-in", "--format", "json"])
    assert code == 0
    env = json.loads(capsys.readouterr().out)
    assert env["tool"] == "hotato"
    assert env["summary"]["events"] == 8
