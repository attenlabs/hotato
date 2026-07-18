"""``hotato init --agents``: register hotato with the project's coding agents.

One flag on the existing ``init`` group that writes the same core-loop
registration into every agent config surface present in the project:
AGENTS.md (always), a Claude Code skill or CLAUDE.md section, a Cursor rule
or .cursorrules block, and the ``.mcp.json`` server entry. The contract under
test: created when absent, appended/refreshed idempotently when present,
never a destroyed byte of user content, and a second run changes nothing.

Hermetic: every test runs in its own tmp project directory; nothing network,
nothing global.
"""

import json
import os

import pytest

from hotato import cli, initcmd
from hotato.cli import _CORE_LOOP_STEPS


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _by_surface(result):
    return {s["surface"]: s for s in result["surfaces"]}


# ---------------------------------------------------------------------------
# AGENTS.md: always written, delimited, idempotent
# ---------------------------------------------------------------------------


def test_agents_md_created_when_absent_with_the_core_loop(tmp_path):
    result = initcmd.register_agents(str(tmp_path))
    assert _by_surface(result)["agents-md"]["action"] == "created"
    text = _read(tmp_path / "AGENTS.md")
    assert initcmd.AGENTS_BLOCK_BEGIN in text
    assert initcmd.AGENTS_BLOCK_END in text
    # The registration reuses the CLI's own core loop, byte-congruent.
    for cmd, _blurb in _CORE_LOOP_STEPS:
        assert cmd in text
    assert "hotato describe --format json" in text
    assert initcmd.MCP_COMMAND in text


def test_agents_md_appended_when_present_never_destroys_user_content(tmp_path):
    user = "# My project\n\nExisting agent notes the user wrote.\n"
    (tmp_path / "AGENTS.md").write_text(user, encoding="utf-8")
    result = initcmd.register_agents(str(tmp_path))
    assert _by_surface(result)["agents-md"]["action"] == "updated"
    text = _read(tmp_path / "AGENTS.md")
    assert text.startswith("# My project")
    assert "Existing agent notes the user wrote." in text
    assert text.count(initcmd.AGENTS_BLOCK_BEGIN) == 1


def test_agents_md_second_run_is_a_no_op(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Mine\n", encoding="utf-8")
    initcmd.register_agents(str(tmp_path))
    first = _read(tmp_path / "AGENTS.md")
    result = initcmd.register_agents(str(tmp_path))
    assert _by_surface(result)["agents-md"]["action"] == "unchanged"
    assert _read(tmp_path / "AGENTS.md") == first
    assert first.count(initcmd.AGENTS_BLOCK_BEGIN) == 1


def test_agents_md_stale_block_refreshed_in_place(tmp_path):
    initcmd.register_agents(str(tmp_path))
    fresh = _read(tmp_path / "AGENTS.md")
    stale = fresh.replace("turn-taking regression checks",
                          "an old block body from a previous version")
    tail = "\n## User section below the block\n\nkept intact.\n"
    (tmp_path / "AGENTS.md").write_text(stale + tail, encoding="utf-8")
    result = initcmd.register_agents(str(tmp_path))
    assert _by_surface(result)["agents-md"]["action"] == "updated"
    text = _read(tmp_path / "AGENTS.md")
    assert "an old block body" not in text
    assert "turn-taking regression checks" in text
    assert "## User section below the block" in text
    assert text.count(initcmd.AGENTS_BLOCK_BEGIN) == 1


def test_agents_md_broken_marker_pair_refused_not_guessed(tmp_path):
    (tmp_path / "AGENTS.md").write_text(
        f"notes\n{initcmd.AGENTS_BLOCK_BEGIN}\nhalf a block, no end marker\n",
        encoding="utf-8")
    before = _read(tmp_path / "AGENTS.md")
    with pytest.raises(initcmd.InitError):
        initcmd.register_agents(str(tmp_path))
    assert _read(tmp_path / "AGENTS.md") == before  # untouched


# ---------------------------------------------------------------------------
# Claude Code: .claude/skills/hotato/SKILL.md, or a CLAUDE.md section
# ---------------------------------------------------------------------------


def test_claude_skill_written_when_claude_dir_exists(tmp_path):
    (tmp_path / ".claude").mkdir()
    result = initcmd.register_agents(str(tmp_path))
    assert _by_surface(result)["claude-skill"]["action"] == "created"
    skill = tmp_path / ".claude" / "skills" / "hotato" / "SKILL.md"
    text = _read(skill)
    assert text.startswith("---\nname: hotato\n")
    assert "description:" in text
    for cmd, _blurb in _CORE_LOOP_STEPS:
        assert cmd in text
    # idempotent
    second = initcmd.register_agents(str(tmp_path))
    assert _by_surface(second)["claude-skill"]["action"] == "unchanged"
    assert _read(skill) == text


def test_no_claude_surface_is_not_invented(tmp_path):
    result = initcmd.register_agents(str(tmp_path))
    assert "claude-skill" not in _by_surface(result)
    assert "claude-md" not in _by_surface(result)
    assert not (tmp_path / ".claude").exists()
    assert not (tmp_path / "CLAUDE.md").exists()


def test_claude_md_section_when_only_claude_md_exists(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# CLAUDE.md\n\nHouse rules.\n",
                                        encoding="utf-8")
    result = initcmd.register_agents(str(tmp_path))
    assert _by_surface(result)["claude-md"]["action"] == "updated"
    text = _read(tmp_path / "CLAUDE.md")
    assert "House rules." in text
    assert initcmd.AGENTS_BLOCK_BEGIN in text
    assert not (tmp_path / ".claude").exists()


def test_user_owned_skill_file_is_kept_byte_for_byte(tmp_path):
    skill = tmp_path / ".claude" / "skills" / "hotato" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    user = "---\nname: hotato\n---\n\nThe user rewrote this skill.\n"
    skill.write_text(user, encoding="utf-8")
    result = initcmd.register_agents(str(tmp_path))
    assert _by_surface(result)["claude-skill"]["action"] == "kept"
    assert _read(skill) == user


# ---------------------------------------------------------------------------
# Cursor: .cursor/rules/hotato.mdc, or a .cursorrules block
# ---------------------------------------------------------------------------


def test_cursor_rule_written_when_cursor_dir_exists(tmp_path):
    (tmp_path / ".cursor").mkdir()
    result = initcmd.register_agents(str(tmp_path))
    assert _by_surface(result)["cursor-rule"]["action"] == "created"
    rule = tmp_path / ".cursor" / "rules" / "hotato.mdc"
    text = _read(rule)
    assert text.startswith("---\ndescription:")
    assert "alwaysApply: false" in text
    for cmd, _blurb in _CORE_LOOP_STEPS:
        assert cmd in text
    second = initcmd.register_agents(str(tmp_path))
    assert _by_surface(second)["cursor-rule"]["action"] == "unchanged"
    assert _read(rule) == text


def test_cursorrules_file_gains_a_delimited_block(tmp_path):
    (tmp_path / ".cursorrules").write_text("use tabs\n", encoding="utf-8")
    result = initcmd.register_agents(str(tmp_path))
    assert _by_surface(result)["cursor-rules-file"]["action"] == "updated"
    text = _read(tmp_path / ".cursorrules")
    assert text.startswith("use tabs\n")
    assert initcmd.AGENTS_BLOCK_BEGIN in text
    assert not (tmp_path / ".cursor").exists()


# ---------------------------------------------------------------------------
# MCP: .mcp.json entry merged when the file exists; the one-liner always
# ---------------------------------------------------------------------------


def test_mcp_json_entry_added_preserving_other_servers(tmp_path):
    (tmp_path / ".mcp.json").write_text(json.dumps({
        "mcpServers": {"other": {"command": "npx", "args": ["-y", "other-mcp"]}}
    }), encoding="utf-8")
    result = initcmd.register_agents(str(tmp_path))
    assert _by_surface(result)["mcp"]["action"] == "created"
    doc = json.loads(_read(tmp_path / ".mcp.json"))
    assert doc["mcpServers"]["other"] == {"command": "npx",
                                          "args": ["-y", "other-mcp"]}
    assert doc["mcpServers"]["hotato"] == {
        "command": "uvx", "args": ["--from", "hotato[mcp]", "hotato-mcp"]}
    second = initcmd.register_agents(str(tmp_path))
    assert _by_surface(second)["mcp"]["action"] == "unchanged"


def test_mcp_json_absent_is_not_created_but_command_is_reported(tmp_path):
    result = initcmd.register_agents(str(tmp_path))
    assert _by_surface(result)["mcp"]["action"] == "absent"
    assert not (tmp_path / ".mcp.json").exists()
    assert result["mcp_command"] == 'uvx --from "hotato[mcp]" hotato-mcp'


def test_mcp_json_unparseable_is_kept_byte_for_byte(tmp_path):
    (tmp_path / ".mcp.json").write_text("{not json", encoding="utf-8")
    result = initcmd.register_agents(str(tmp_path))
    assert _by_surface(result)["mcp"]["action"] == "kept"
    assert _read(tmp_path / ".mcp.json") == "{not json"


# ---------------------------------------------------------------------------
# Whole-project idempotency + generated-copy hygiene
# ---------------------------------------------------------------------------


def _full_project(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".cursor").mkdir()
    (tmp_path / "AGENTS.md").write_text("# Mine\n\nnotes.\n", encoding="utf-8")
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")


def test_second_run_changes_no_byte_anywhere(tmp_path):
    _full_project(tmp_path)
    initcmd.register_agents(str(tmp_path))
    snapshot = {}
    for dirpath, _dirs, files in os.walk(tmp_path):
        for name in files:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as fh:
                snapshot[path] = fh.read()
    result = initcmd.register_agents(str(tmp_path))
    assert all(s["action"] == "unchanged" for s in result["surfaces"])
    for path, data in snapshot.items():
        with open(path, "rb") as fh:
            assert fh.read() == data, f"{path} changed on the second run"


def test_generated_copy_has_no_em_dash_and_no_absence_copy(tmp_path):
    _full_project(tmp_path)
    initcmd.register_agents(str(tmp_path))
    for rel in ("AGENTS.md",
                os.path.join(".claude", "skills", "hotato", "SKILL.md"),
                os.path.join(".cursor", "rules", "hotato.mdc")):
        text = _read(tmp_path / rel)
        assert "—" not in text, f"em dash in generated {rel}"
        for banned in ("coming soon", "roadmap", "not yet", "TODO"):
            assert banned not in text, f"{banned!r} in generated {rel}"


# ---------------------------------------------------------------------------
# CLI wiring: the flag on the existing init group
# ---------------------------------------------------------------------------


def test_cli_init_agents_runs_in_cwd(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", "--agents"]) == 0
    out = capsys.readouterr().out
    assert "AGENTS.md" in out
    assert 'uvx --from "hotato[mcp]" hotato-mcp' in out
    # exactly one next step, and it is step 1 of the core loop
    assert out.rstrip().endswith("hotato start --demo")
    assert (tmp_path / "AGENTS.md").exists()


def test_cli_init_agents_format_json(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", "--agents", "--format", "json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["kind"] == "init-agents"
    assert doc["next"] == ["hotato start --demo"]
    assert {s["surface"] for s in doc["surfaces"]} == {"agents-md", "mcp"}


def test_cli_bare_init_is_still_a_usage_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init"]) == 2
    assert "subcommand" in capsys.readouterr().err
    assert not (tmp_path / "AGENTS.md").exists()


def test_cli_init_subcommands_still_dispatch(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", "starter", "--stack", "generic",
                     "--out", str(tmp_path / "kit")]) == 0
    assert (tmp_path / "kit" / "HOTATO.md").exists()
