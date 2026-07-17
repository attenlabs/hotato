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
    assert "src/hotato/schema/counterexample-oracle.v1.json" in headers
    assert "src/hotato/schema/reduction-certificate.v1.json" in headers
    assert "src/hotato/schema/counterexample.v1.json" in headers
    assert "src/hotato/schema/envelope.v1.json" in headers
    # Every doc the build script itself resolves via `_tracked_docs_md()` is
    # represented. Reuse that helper (git ls-files in a checkout, filesystem
    # glob in a git-less extracted sdist tree) rather than reimplementing its
    # environment detection here.
    on_disk = set(build_llms_full._tracked_docs_md())
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
    import io
    from contextlib import redirect_stdout

    from hotato.cli import main as cli_main

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(["describe", "--format", "json"])
    assert rc == 0
    manifest = json.loads(buf.getvalue())
    assert manifest["schemas"]["envelope"] == ENVELOPE_SCHEMA_URL
    assert manifest["schemas"]["error"] == ERROR_SCHEMA_URL
    for name in (
        "counterexample", "counterexample_oracle", "reduction_certificate",
    ):
        assert manifest["schemas"][name].startswith("https://hotato.dev/schema/")


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
    # Gate (since 2026-07-16): the job fires ONLY on a manual dispatch that
    # sets confirm_registry_publish=true. On a tag push the `inputs` context
    # is empty, so the condition is false and the job never runs there. The
    # original one-way first publish happened under a hardcoded `if: false`;
    # this test now holds the dispatch-input invariant instead.
    assert re.search(
        r"^\s*if:\s*\$\{\{\s*inputs\.confirm_registry_publish\s*==\s*true\s*\}\}",
        publish_block, re.MULTILINE,
    ), (
        "publish-mcp-registry must be gated on the confirm_registry_publish "
        "dispatch input (and therefore inert on tag pushes)"
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


# ---------------------------------------------------------------------------
# fix trial honesty: "what this does not stop" is real doc content, not just
# in-memory strings, and it survives into the single-fetch agent context dump.
# ---------------------------------------------------------------------------

FIX_TRIAL_HONEST_SENTENCE = (
    "A green fix trial does not prove the audio was freshly captured for "
    "the scenario claimed, that the same policy and labels were used "
    "throughout, that any omitted fixture was safe to omit, that the named "
    "revision or clone existed, that the patch was applied to it, or that "
    "the deployed agent improved."
)


def _collapse_ws(text: str) -> str:
    # Markdown source wraps prose across lines at ~80 columns like every
    # other paragraph in these docs; a sentence checked for verbatim
    # presence has to be compared whitespace-insensitively, not line-by-line.
    return re.sub(r"\s+", " ", text)


def test_fix_trial_honest_sentence_is_in_the_doc():
    assert FIX_TRIAL_HONEST_SENTENCE in _collapse_ws(
        _read("docs", "FIX-TRIAL.md"))


def test_fix_trial_honest_sentence_survives_into_llms_full_txt():
    # llms-full.txt is the single-fetch agent context dump (built by
    # scripts/build_llms_full.py from README + docs/*.md); an agent that only
    # ever reads that one file must still see the same honest boundary a
    # human reading docs/FIX-TRIAL.md directly would see.
    assert FIX_TRIAL_HONEST_SENTENCE in _collapse_ws(_read("llms-full.txt"))


def test_fix_trial_and_recapture_docs_both_name_the_same_residual_limits():
    # The three concrete residuals (fabricated-but-fresh stimulus, manifest
    # integrity != authenticity, transcode-changes-PCM) and the
    # signatures-not-implemented note must appear in both docs, not just one:
    # a reader who only opens RECAPTURE.md (the manual path) must not miss
    # the same boundaries a reader of FIX-TRIAL.md (the automated path) sees.
    fix_trial = _read("docs", "FIX-TRIAL.md")
    recapture = _read("docs", "RECAPTURE.md")
    for doc in (fix_trial, recapture):
        assert "What this does not stop" in doc
        assert "integrity" in doc and "authenticity" in doc
        assert "signature" in doc.lower()
        assert "resample" in doc.lower() or "re-encode" in doc.lower()


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
