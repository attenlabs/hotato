"""Agent-facing docs: llms.txt / llms-full.txt build determinism, server.json
validity, and schema-URL / footgun consistency across every surface an agent
reads first (llms.txt, docs/MCP.md, mcp_server._TOOL_DESCRIPTION, `hotato
describe`).
"""

import importlib.util
import json
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ENVELOPE_SCHEMA_URL = "https://hotato.dev/schema/envelope.v1.json"
ERROR_SCHEMA_URL = "https://hotato.dev/schema/error.v1.json"
CORRECT_MCP_CMD = 'uvx --from "hotato[mcp]" hotato-mcp'
FOOTGUN_MCP_CMD = "uvx hotato-mcp"


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _load_build_script():
    spec = importlib.util.spec_from_file_location(
        "build_llms_full", os.path.join(ROOT, "scripts", "build_llms_full.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# llms-full.txt: deterministic build, checked-in copy up to date
# ---------------------------------------------------------------------------


def test_build_llms_full_is_deterministic():
    build_llms_full = _load_build_script()
    first = build_llms_full.build()
    second = build_llms_full.build()
    assert first == second


def test_llms_full_txt_committed_copy_is_up_to_date():
    build_llms_full = _load_build_script()
    fresh = build_llms_full.build()
    committed = _read("llms-full.txt")
    assert committed == fresh, (
        "llms-full.txt is stale; regenerate with "
        "python3 scripts/build_llms_full.py"
    )


def test_llms_full_txt_has_a_file_boundary_header_per_source_file():
    build_llms_full = _load_build_script()
    content = _read("llms-full.txt")
    headers = re.findall(r"^FILE: (.+)$", content, re.MULTILINE)
    assert "README.md" in headers
    assert "METHODOLOGY.md" in headers
    assert "docs/MCP.md" in headers
    assert "src/hotato/schema/envelope.v1.json" in headers
    # Every docs/*.md on disk is represented (the build globs docs/*.md, so
    # nothing added to docs/ later can be silently dropped).
    docs_dir = os.path.join(ROOT, "docs")
    on_disk = {f"docs/{f}" for f in os.listdir(docs_dir) if f.endswith(".md")}
    assert on_disk.issubset(set(headers))
    # schema file is last, README is first.
    assert headers[0] == "README.md"
    assert headers[-1] == "src/hotato/schema/envelope.v1.json"


def test_build_llms_full_check_flag(tmp_path):
    script = os.path.join(ROOT, "scripts", "build_llms_full.py")
    out = tmp_path / "llms-full.txt"
    r1 = subprocess.run(
        [sys.executable, script, "--out", str(out), "--check"],
        capture_output=True, text=True,
    )
    assert r1.returncode == 1  # does not exist yet -> stale

    r2 = subprocess.run(
        [sys.executable, script, "--out", str(out)],
        capture_output=True, text=True,
    )
    assert r2.returncode == 0
    assert out.exists()

    r3 = subprocess.run(
        [sys.executable, script, "--out", str(out), "--check"],
        capture_output=True, text=True,
    )
    assert r3.returncode == 0


# ---------------------------------------------------------------------------
# server.json
# ---------------------------------------------------------------------------


def test_server_json_is_valid_json():
    data = json.loads(_read("server.json"))
    assert data["name"] == "io.github.attenlabs/hotato"
    assert data["$schema"].startswith("https://static.modelcontextprotocol.io/")
    assert len(data["description"]) <= 100
    assert re.match(r"^[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+$", data["name"])


def test_server_json_version_matches_pyproject():
    pyproject = _read("pyproject.toml")
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', pyproject)
    assert m, "no version in pyproject.toml"
    pv = m.group(1)

    server = json.loads(_read("server.json"))
    assert server["version"] == pv
    for pkg in server["packages"]:
        assert pkg["version"] == pv


def test_server_json_package_wires_the_correct_uvx_invocation():
    server = json.loads(_read("server.json"))
    pkg = server["packages"][0]
    assert pkg["registryType"] == "pypi"
    assert pkg["identifier"] == "hotato"
    assert pkg["runtimeHint"] == "uvx"
    values = [a["value"] for a in pkg["packageArguments"]]
    assert values == ["--from", "hotato[mcp]", "hotato-mcp"]
    assert pkg["transport"]["type"] == "stdio"


def test_readme_carries_the_mcp_name_ownership_marker():
    # The MCP registry validates namespace ownership for io.github.* names via
    # a `mcp-name:` line in the README; keep it in lockstep with server.json.
    server = json.loads(_read("server.json"))
    assert f"mcp-name: {server['name']}" in _read("README.md")


# ---------------------------------------------------------------------------
# The uvx --from footgun: every doc must use the correct form, and any
# mention of the bare form must be inside an explicit self-correction note.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        ("llms.txt",),
        ("README.md",),
        ("docs", "MCP.md"),
        ("docs", "API.md"),
        ("src", "hotato", "mcp_server.py"),
    ],
)
def test_bare_uvx_hotato_mcp_never_appears_unwarned(path):
    text = _read(*path)
    for line in text.splitlines():
        if FOOTGUN_MCP_CMD in line and CORRECT_MCP_CMD not in line:
            # The bare form is only ever acceptable inside a line that is
            # explicitly warning about it (mentions "fail", "FAIL", "no
            # --from", "footgun", or "mistake").
            warned = any(
                w in line for w in ("fail", "FAIL", "footgun", "mistake", "--from")
            )
            assert warned, f"unwarned bare '{FOOTGUN_MCP_CMD}' in {path}: {line!r}"


def test_correct_uvx_command_present_in_every_mcp_surface():
    for path in [
        ("llms.txt",),
        ("README.md",),
        ("docs", "MCP.md"),
        ("docs", "API.md"),
        ("src", "hotato", "mcp_server.py"),
    ]:
        assert CORRECT_MCP_CMD in _read(*path), f"missing correct uvx form in {path}"


def test_mcp_server_tool_description_carries_schema_url_and_correct_command():
    from hotato.mcp_server import _TOOL_DESCRIPTION

    assert ENVELOPE_SCHEMA_URL in _TOOL_DESCRIPTION
    assert CORRECT_MCP_CMD in _TOOL_DESCRIPTION


# ---------------------------------------------------------------------------
# Schema URL consistency: envelope/error $id, `hotato describe`, and every doc
# that quotes a schema URL must all agree.
# ---------------------------------------------------------------------------


def test_envelope_and_error_schema_ids_match_the_hosted_urls():
    envelope = json.loads(_read("src", "hotato", "schema", "envelope.v1.json"))
    error = json.loads(_read("src", "hotato", "schema", "error.v1.json"))
    assert envelope["$id"] == ENVELOPE_SCHEMA_URL
    assert error["$id"] == ERROR_SCHEMA_URL


def test_describe_schema_urls_match_the_shipped_schema_files():
    from hotato.cli import main as cli_main

    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(["describe", "--format", "json"])
    assert rc == 0
    manifest = json.loads(buf.getvalue())
    assert manifest["schemas"]["envelope"] == ENVELOPE_SCHEMA_URL
    assert manifest["schemas"]["error"] == ERROR_SCHEMA_URL


def test_llms_txt_and_docs_mcp_quote_the_same_schema_urls():
    for path in [("llms.txt",), ("docs", "MCP.md")]:
        text = _read(*path)
        assert ENVELOPE_SCHEMA_URL in text, f"{path} missing envelope schema URL"


# ---------------------------------------------------------------------------
# ci/github_action.yml no longer instructs a blind copy; release.yml exists
# and its registry-publish job stays hard-gated.
# ---------------------------------------------------------------------------


def test_ci_github_action_yml_does_not_claim_to_be_a_template():
    text = _read("ci", "github_action.yml")
    assert "Copy this to .github/workflows/hotato.yml in your repository." not in text
    assert "NOT a template" in text
    assert ".github/workflows/hotato.yml" in text  # still points at the real one


def test_release_workflow_registry_publish_job_is_hard_gated():
    # Regex/text checks only (no PyYAML dependency; the core stays
    # dependency-light and this test must run in every CI matrix leg).
    text = _read(".github", "workflows", "release.yml")

    assert "workflow_dispatch:" in text
    assert "confirm_registry_publish:" in text

    m = re.search(r"(?ms)^  publish-mcp-registry:\n(.*?)(?=^  \S|\Z)", text)
    assert m, "publish-mcp-registry job not found"
    publish_block = m.group(1)
    assert re.search(r"^\s*if:\s*false\b", publish_block, re.MULTILINE), (
        "publish-mcp-registry job must be hardcoded if: false until an "
        "operator explicitly lifts the gate"
    )
    assert "mcp-publisher" in publish_block

    sm = re.search(r"(?ms)^  sanity:\n(.*?)(?=^  \S|\Z)", text)
    assert sm, "sanity job not found"
    assert "mcp-publisher" not in sm.group(1), (
        "mcp-publisher must only be invoked inside the gated job"
    )


# ---------------------------------------------------------------------------
# CITATION.cff
# ---------------------------------------------------------------------------


def test_citation_cff_is_valid_and_version_matches_pyproject():
    # Regex checks only (no PyYAML dependency; see the release.yml test above
    # for the same reasoning). CITATION.cff is a small flat mapping, so a
    # `^key: value$` scan is unambiguous.
    citation = _read("CITATION.cff")
    assert re.search(r"^cff-version:\s*\S+", citation, re.MULTILINE)
    assert re.search(r"^title:\s*\S", citation, re.MULTILINE)
    assert re.search(r"^license:\s*MIT\s*$", citation, re.MULTILINE)

    m = re.search(r'^version:\s*(\S+)\s*$', citation, re.MULTILINE)
    assert m, "no version: line in CITATION.cff"
    citation_version = m.group(1)

    pyproject = _read("pyproject.toml")
    pm = re.search(r'(?m)^version\s*=\s*"([^"]+)"', pyproject)
    assert citation_version == pm.group(1)
