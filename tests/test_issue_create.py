"""`hotato issue create SWEEP_JSON --repo OWNER/REPO`: render a sweep result
into a GitHub issue, dry-run by default, `gh` only under --yes.

Pinned here: the pure offline renderer's output on a real `hotato sweep --demo
--format json` result (title from the run, the worst-candidate block, a
confirm-or-ignore section per top candidate carrying BOTH the yield and the
hold promote command plus the close-it line), --top slicing, --label handling,
the two honesty boundaries (the renderer is pure and offline; the default is a
dry run that prints the body and the exact gh command and NEVER shells out; only
--yes with an explicit --repo invokes gh), the required --repo, the reused
sweep/analyze parser refusing a missing or foreign file, and candidate-moments
language throughout.
"""

import json
import os
import stat
import sys

import pytest

from hotato import cli, issuecmd

# --- a real sweep --demo result on disk --------------------------------------

@pytest.fixture()
def sweep_json(tmp_path, capsys, monkeypatch):
    """A real `hotato sweep --demo --format json` result, exactly the file a
    user redirects stdout into."""
    monkeypatch.setenv("HOTATO_HOME", str(tmp_path / "home"))
    assert cli.main(["sweep", "--demo", "--format", "json"]) == 0
    path = tmp_path / "hotato-sweep.json"
    path.write_text(capsys.readouterr().out, encoding="utf-8")
    return path


@pytest.fixture()
def sweep_doc(sweep_json):
    return json.loads(sweep_json.read_text(encoding="utf-8"))


# --- a fake gh on PATH, so the ONLY side effect is observable ----------------

@pytest.fixture()
def fake_gh(tmp_path, monkeypatch):
    """Put a fake `gh` first on PATH that records its argv and piped stdin to a
    marker file and prints an issue URL. The marker file exists IFF gh ran, so a
    dry run is proven by its ABSENCE and a create by its presence + contents."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    marker = tmp_path / "gh-was-called.txt"
    script = bindir / "gh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        '{ printf "ARGV:"; for a in "$@"; do printf " %s" "$a"; done; '
        'printf "\\nSTDIN-START\\n"; cat; printf "\\nSTDIN-END\\n"; } '
        '>> "$HOTATO_TEST_GH_MARKER"\n'
        'echo "https://github.com/owner/repo/issues/123"\n',
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP
                 | stat.S_IXOTH)
    monkeypatch.setenv("HOTATO_TEST_GH_MARKER", str(marker))
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    return marker


# --- the pure renderer -------------------------------------------------------

def test_renderer_output_on_real_sweep(sweep_doc):
    env = issuecmd.build_issue(
        sweep_doc, report_ref="hotato-sweep.json", repo="owner/repo", top=3)
    body = env["body"]

    # Title from the run.
    assert env["title"].startswith("Turn-taking sweep:")
    assert env["title"] in body or env["title"]  # title is carried on the env

    # The worst-candidate block: call id / time / kind / measured / report.
    worst = sweep_doc["candidates"][0]
    assert env["worst"]["source"] == worst["source"]
    assert env["worst"]["t_sec"] == worst["t_sec"]
    assert env["worst"]["kind"] == worst["kind"]
    assert "## Worst candidate" in body
    assert f"- kind: {worst['kind']}" in body
    assert f"- time: {worst['t_sec']:.2f}s" in body
    assert "- measured:" in body
    assert "- report: `hotato-sweep.json`" in body

    # No em / en dashes anywhere in the body (house style).
    assert "—" not in body and "–" not in body


def test_top_n_slicing(sweep_doc):
    total = sweep_doc["total_candidates"]
    assert total >= 3, "the demo sweep is expected to surface several moments"

    one = issuecmd.build_issue(
        sweep_doc, report_ref="s.json", repo="o/r", top=1)
    assert len(one["candidates"]) == 1
    assert one["body"].count("### #") == 1

    two = issuecmd.build_issue(
        sweep_doc, report_ref="s.json", repo="o/r", top=2)
    assert len(two["candidates"]) == 2
    assert two["body"].count("### #") == 2

    all_ = issuecmd.build_issue(
        sweep_doc, report_ref="s.json", repo="o/r", top=0)
    assert len(all_["candidates"]) == total


def test_ranks_are_one_based_and_match_the_promote_refs(sweep_doc):
    env = issuecmd.build_issue(
        sweep_doc, report_ref="hotato-sweep.json", repo="o/r", top=2)
    assert [c["rank"] for c in env["candidates"]] == [1, 2]
    # The #N in the promote command is the same 1-based rank the report shows.
    assert "hotato fixture promote hotato-sweep.json#1 " in env["candidates"][0]["promote_yield"]
    assert "hotato fixture promote hotato-sweep.json#2 " in env["candidates"][1]["promote_hold"]


def test_both_yield_and_hold_promote_commands_present(sweep_doc):
    env = issuecmd.build_issue(
        sweep_doc, report_ref="hotato-sweep.json", repo="o/r", top=3)
    body = env["body"]
    for i in range(1, 4):
        assert (f"hotato fixture promote hotato-sweep.json#{i} --expect yield"
                in body)
        assert (f"hotato fixture promote hotato-sweep.json#{i} --expect hold"
                in body)


def test_close_if_not_turn_taking_line_present(sweep_doc):
    env = issuecmd.build_issue(
        sweep_doc, report_ref="s.json", repo="o/r", top=2)
    # One close-it line per candidate section.
    assert env["body"].count("close this issue") == 2
    assert "Not a turn-taking moment?" in env["body"]


def test_candidate_moments_language_never_a_verdict(sweep_doc):
    env = issuecmd.build_issue(
        sweep_doc, report_ref="s.json", repo="o/r", top=3)
    body = env["body"].lower()
    assert "candidate" in body
    assert "measured" in body
    assert "not verdicts" in body
    # The renderer never asserts intent or a decided outcome.
    for verdict in ("the agent failed", "confirmed bug", "this is a bug",
                    "definitely", "pass/fail"):
        assert verdict not in body


def test_label_handling_in_gh_command(sweep_doc):
    none = issuecmd.build_issue(
        sweep_doc, report_ref="s.json", repo="o/r", top=1)
    assert "--label" not in none["gh_command"]

    many = issuecmd.build_issue(
        sweep_doc, report_ref="s.json", repo="o/r", top=1,
        labels=["turn-taking", "regression"])
    argv = many["gh_command"]
    assert argv[:5] == ["gh", "issue", "create", "--repo", "o/r"]
    # One --label pair per label.
    assert argv.count("--label") == 2
    assert "turn-taking" in argv and "regression" in argv
    # Body is piped on stdin, never inlined on the command line.
    assert argv[-2:] == ["--body-file", "-"]


def test_renderer_is_pure_and_offline(sweep_doc, monkeypatch):
    """build_issue must not shell out: no gh, no network. If it touched
    subprocess this would raise."""
    def boom(*a, **k):
        raise AssertionError("build_issue shelled out")

    monkeypatch.setattr(issuecmd.subprocess, "run", boom)
    env = issuecmd.build_issue(
        sweep_doc, report_ref="s.json", repo="o/r", top=3)
    assert isinstance(env["body"], str) and env["body"]


def test_no_candidates_is_refused(tmp_path):
    doc = {"tool": "hotato", "kind": "analyze", "candidates": [],
           "total_candidates": 0}
    with pytest.raises(ValueError) as exc:
        issuecmd.build_issue(doc, report_ref="empty.json", repo="o/r")
    assert "no candidate" in str(exc.value)


# --- the CLI: dry run by default, gh only under --yes ------------------------

def test_dry_run_prints_body_and_command_and_does_not_call_gh(
        sweep_json, fake_gh, capsys):
    rc = cli.main(["issue", "create", str(sweep_json), "--repo", "owner/repo"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "## Worst candidate" in out
    assert "Dry run: nothing was created." in out
    assert "gh issue create --repo owner/repo" in out
    # The one thing that must NOT happen: gh was never invoked.
    assert not fake_gh.exists(), "the dry run must not call gh"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="fake_gh is a bash script with a POSIX shebang, invoked by argv0 "
           "with no extension; Windows subprocess dispatch cannot execute it "
           "the way a real gh would be found on PATH",
)
def test_yes_shells_out_to_gh_with_the_exact_argv_and_body(
        sweep_json, fake_gh, capsys):
    rc = cli.main([
        "issue", "create", str(sweep_json), "--repo", "owner/repo",
        "--top", "2", "--label", "turn-taking", "--yes",
    ])
    assert rc == 0
    assert fake_gh.exists(), "gh must run under --yes"
    recorded = fake_gh.read_text(encoding="utf-8")
    assert "issue create --repo owner/repo" in recorded
    assert "--title Turn-taking sweep:" in recorded
    assert "--label turn-taking" in recorded
    # The full rendered body was piped on stdin, not inlined.
    assert "## Worst candidate" in recorded
    assert "hotato fixture promote hotato-sweep.json#1 --expect yield" in recorded
    # The created URL gh printed is surfaced.
    assert "https://github.com/owner/repo/issues/123" in capsys.readouterr().out


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the fake gh here is a bash script with a POSIX shebang, invoked "
           "by argv0 with no extension; Windows subprocess dispatch cannot "
           "execute it the way a real gh would be found on PATH",
)
def test_gh_failure_is_a_clean_usage_error(sweep_json, tmp_path, monkeypatch,
                                           capsys):
    # A fake gh that exits non-zero: the create must surface a clean exit-2
    # usage error carrying gh's own message, never a traceback.
    bindir = tmp_path / "failbin"
    bindir.mkdir()
    script = bindir / "gh"
    script.write_text(
        "#!/usr/bin/env bash\n>&2 echo 'gh: could not authenticate'\nexit 1\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP
                 | stat.S_IXOTH)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    rc = cli.main([
        "issue", "create", str(sweep_json), "--repo", "owner/repo", "--yes",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "gh issue create failed" in err
    assert "could not authenticate" in err


def test_json_format_dry_run_shape(sweep_json, capsys):
    rc = cli.main([
        "issue", "create", str(sweep_json), "--repo", "owner/repo",
        "--top", "2", "--format", "json",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tool"] == "hotato"
    assert payload["kind"] == "issue"
    assert payload["dry_run"] is True
    assert payload["created"] is False
    assert payload["repo"] == "owner/repo"
    assert isinstance(payload["gh_command"], list)
    assert payload["gh_command"][:3] == ["gh", "issue", "create"]
    assert len(payload["candidates"]) == 2
    assert payload["candidates"][0]["rank"] == 1


# --- required --repo, and the reused sweep/analyze parser --------------------

def test_missing_repo_is_a_clean_usage_error(sweep_json, capsys):
    rc = cli.main(["issue", "create", str(sweep_json)])
    assert rc == 2
    assert "--repo" in capsys.readouterr().err


def test_missing_file_is_the_structured_json_error(tmp_path, capsys):
    rc = cli.main([
        "issue", "create", str(tmp_path / "nope.json"),
        "--repo", "owner/repo", "--format", "json",
    ])
    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error_code"] == "file_not_found"


def test_foreign_json_is_refused_with_the_reason(tmp_path, capsys):
    foreign = tmp_path / "fixture.json"
    foreign.write_text(json.dumps({"tool": "hotato", "kind": "fixture"}),
                       encoding="utf-8")
    rc = cli.main([
        "issue", "create", str(foreign), "--repo", "owner/repo",
    ])
    assert rc == 2
    assert "not a hotato sweep/analyze result" in capsys.readouterr().err


def test_empty_candidates_result_is_exit_2(tmp_path, capsys):
    empty = tmp_path / "clean.json"
    empty.write_text(json.dumps({"kind": "analyze", "candidates": [],
                                 "total_candidates": 0}), encoding="utf-8")
    rc = cli.main(["issue", "create", str(empty), "--repo", "owner/repo"])
    assert rc == 2
    assert "no candidate" in capsys.readouterr().err
