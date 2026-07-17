"""`hotato pr create --fixtures DIR --repo OWNER/REPO --title T`: render a
directory of promoted fixtures into a pull request, dry-run by default, git and
`gh` only under --yes.

Pinned here: the pure offline renderer's output on a REAL promoted fixtures
directory (one candidate promoted from a `hotato sweep --demo --format json`
result), the git/gh command plan, the two honesty boundaries (the body renderer
is pure and offline; the default is a dry run that prints the body and the exact
commands and NEVER shells out; only --yes with an explicit --repo runs git and
gh), the two safety invariants (the change lands on a NEW feature branch, never
the default branch directly; the push is never a force-push), the required
--repo / --fixtures, the reused fixture schema, and candidate-moments language
throughout.

Also pinned: the second accepted --fixtures shape, a `<id>.hotato` contract
bundle from `hotato investigate label` (or a directory of them), detected by
shape. The bundle is staged WHOLE under tests/hotato/contracts/,
byte-identical (it is content-addressed), and the refusal on a directory
that is neither shape names both.
"""

import json
import os
import shutil
import stat
import sys
from importlib import resources

import pytest

from hotato import cli, prcmd

# --- a real promoted fixtures directory on disk ------------------------------

@pytest.fixture()
def promoted_fx(tmp_path, capsys, monkeypatch):
    """A real fixtures directory: promote the first overlap candidate from a
    `hotato sweep --demo --format json` result into scenarios/ + audio/. Exactly
    the --out DIR a user builds up with `hotato fixture promote`."""
    monkeypatch.setenv("HOTATO_HOME", str(tmp_path / "home"))
    assert cli.main(["sweep", "--demo", "--format", "json"]) == 0
    sweep = tmp_path / "hotato-sweep.json"
    sweep.write_text(capsys.readouterr().out, encoding="utf-8")
    doc = json.loads(sweep.read_text(encoding="utf-8"))
    rank = next(i for i, c in enumerate(doc["candidates"], 1)
                if c["kind"] == "overlap_while_agent_talking")
    fx = tmp_path / "tests" / "hotato"
    assert cli.main([
        "fixture", "promote", f"{sweep}#{rank}",
        "--expect", "yield", "--id", "sweep-overlap-001", "--out", str(fx),
    ]) == 0
    capsys.readouterr()  # drain the promote output
    return fx


# --- fake git + gh on PATH, so the ONLY side effect is observable ------------

@pytest.fixture()
def fake_scm(tmp_path, monkeypatch):
    """Put a fake `git` and `gh` first on PATH that record their argv (and, for
    gh, the piped stdin) to one marker file. The marker exists IFF something
    ran, so a dry run is proven by its ABSENCE and a create by its presence +
    contents. Neither fake touches the real repo: they record and exit 0."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    marker = tmp_path / "scm-was-called.txt"
    git = bindir / "git"
    git.write_text(
        "#!/usr/bin/env bash\n"
        '{ printf "GIT:"; for a in "$@"; do printf " %s" "$a"; done; '
        'printf "\\n"; } >> "$HOTATO_TEST_SCM_MARKER"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    gh = bindir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        '{ printf "GH:"; for a in "$@"; do printf " %s" "$a"; done; '
        'printf "\\nSTDIN-START\\n"; cat; printf "\\nSTDIN-END\\n"; } '
        '>> "$HOTATO_TEST_SCM_MARKER"\n'
        'echo "https://github.com/owner/repo/pull/7"\n',
        encoding="utf-8",
    )
    for script in (git, gh):
        script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP
                     | stat.S_IXOTH)
    monkeypatch.setenv("HOTATO_TEST_SCM_MARKER", str(marker))
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    return marker


# --- pure renderer records (no filesystem needed) ----------------------------

def _record(fid, *, expect="yield", source="fd-01.wav",
            ref="hotato-sweep.json#1", kind="overlap_while_agent_talking",
            onset=1.5):
    """A fixture record in the exact shape `prcmd.load_fixtures` returns, so the
    pure renderer can be exercised without touching disk."""
    return {
        "id": fid,
        "title": fid.replace("-", " "),
        "category": "should_yield" if expect == "yield" else "should_not_yield",
        "expect": expect,
        "caller_onset_sec": onset,
        "duration_sec": 8.0,
        "source": source,
        "source_onset_sec": onset,
        "candidate_ref": ref,
        "candidate_kind": kind,
        "created_by": "hotato fixture promote",
        "scenario_path": f"tests/hotato/scenarios/{fid}.json",
        "audio_path": f"tests/hotato/audio/{fid}.example.wav",
    }


_FORCE_TOKENS = ("--force", "-f", "--force-with-lease", "--force-if-includes")


# --- the pure renderer -------------------------------------------------------

def test_body_lists_every_fixture_and_the_run_command():
    recs = [_record("alpha-001"), _record("beta-002", expect="hold")]
    env = prcmd.build_pr(recs, fixtures_dir="tests/hotato", repo="o/r",
                         title="T")
    body = env["body"]
    assert "`alpha-001`" in body and "`beta-002`" in body
    assert "expect yield" in body and "expect hold" in body
    assert "## Fixtures added (2)" in body
    assert env["run_command"] in body
    assert ("hotato run --scenarios tests/hotato/scenarios "
            "--audio tests/hotato/audio") in body


def test_body_no_em_or_en_dashes():
    env = prcmd.build_pr([_record("a-001")], fixtures_dir="tests/hotato",
                         repo="o/r", title="Add fixtures")
    assert "—" not in env["body"] and "–" not in env["body"]


def test_body_uses_candidate_moments_language_never_a_verdict():
    env = prcmd.build_pr([_record("a-001")], fixtures_dir="tests/hotato",
                         repo="o/r", title="T")
    body = env["body"].lower()
    assert "candidate" in body
    assert "not verdicts" in body
    assert "does not infer intent" in body
    for verdict in ("the agent failed", "confirmed bug", "this is a bug",
                    "definitely", "pass/fail"):
        assert verdict not in body


def test_feature_branch_is_never_the_default_branch():
    env = prcmd.build_pr([_record("a-001")], fixtures_dir="tests/hotato",
                         repo="o/r", title="Add turn-taking fixtures")
    # The change lands on a NEW namespaced feature branch, cut with checkout -b.
    assert env["branch"].startswith("hotato/")
    assert env["branch"] not in prcmd.PROTECTED_BRANCHES
    assert env["git_commands"][0] == ["git", "checkout", "-b", env["branch"]]


def test_protected_branch_is_refused():
    for name in ("main", "master"):
        with pytest.raises(ValueError):
            prcmd.build_pr([_record("a-001")], fixtures_dir="d", repo="o/r",
                           title="T", branch=name)


def test_branch_equal_to_base_is_refused():
    with pytest.raises(ValueError):
        prcmd.build_pr([_record("a-001")], fixtures_dir="d", repo="o/r",
                       title="T", branch="dev", base="dev")


def test_the_plan_never_force_pushes():
    env = prcmd.build_pr([_record("a-001"), _record("b-002")],
                         fixtures_dir="tests/hotato", repo="o/r", title="T")
    pushes = [c for c in env["git_commands"] if c[:2] == ["git", "push"]]
    assert pushes, "the plan pushes the feature branch"
    for cmd in env["git_commands"]:
        for tok in cmd:
            assert tok not in _FORCE_TOKENS, f"force token in {cmd!r}"
            # no + refspec (git push origin +branch is a forced update)
            assert not tok.startswith("+"), f"force refspec in {cmd!r}"


def test_git_add_stages_every_scenario_and_audio_file():
    recs = [_record("a-001"), _record("b-002")]
    env = prcmd.build_pr(recs, fixtures_dir="tests/hotato", repo="o/r",
                         title="T")
    add = next(c for c in env["git_commands"] if c[:2] == ["git", "add"])
    for r in recs:
        assert r["scenario_path"] in add
        assert r["audio_path"] in add


def test_gh_command_pipes_body_on_stdin_and_targets_head_branch():
    env = prcmd.build_pr([_record("a-001")], fixtures_dir="tests/hotato",
                         repo="owner/repo", title="T")
    argv = env["gh_command"]
    assert argv[:5] == ["gh", "pr", "create", "--repo", "owner/repo"]
    assert "--head" in argv and env["branch"] in argv
    # Body is piped on stdin, never inlined on the command line.
    assert argv[-2:] == ["--body-file", "-"]


def test_gh_command_adds_base_only_when_pinned():
    no_base = prcmd.build_pr([_record("a-001")], fixtures_dir="d",
                             repo="o/r", title="T")
    assert "--base" not in no_base["gh_command"]
    with_base = prcmd.build_pr([_record("a-001")], fixtures_dir="d",
                               repo="o/r", title="T", base="main")
    argv = with_base["gh_command"]
    assert "--base" in argv and argv[argv.index("--base") + 1] == "main"


def test_renderer_is_pure_and_offline(monkeypatch):
    """build_pr must not shell out: no git, no gh, no network. If it touched
    subprocess this would raise."""
    def boom(*a, **k):
        raise AssertionError("build_pr shelled out")

    monkeypatch.setattr(prcmd.subprocess, "run", boom)
    env = prcmd.build_pr([_record("a-001")], fixtures_dir="tests/hotato",
                         repo="o/r", title="T")
    assert isinstance(env["body"], str) and env["body"]


def test_no_fixtures_is_refused():
    with pytest.raises(ValueError) as exc:
        prcmd.build_pr([], fixtures_dir="tests/hotato", repo="o/r", title="T")
    assert "nothing to open a pull request" in str(exc.value)


# --- the filesystem loader ---------------------------------------------------

def test_load_fixtures_reads_the_promoted_dir(promoted_fx):
    recs = prcmd.load_fixtures(str(promoted_fx))
    assert len(recs) == 1
    r = recs[0]
    assert r["id"] == "sweep-overlap-001"
    assert r["expect"] == "yield"
    assert r["source"] and r["candidate_ref"]
    assert r["scenario_path"].endswith("scenarios/sweep-overlap-001.json")
    assert r["audio_path"].endswith("audio/sweep-overlap-001.example.wav")


def test_load_fixtures_missing_scenarios_dir_is_refused(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError) as exc:
        prcmd.load_fixtures(str(tmp_path / "empty"))
    assert "not a hotato fixtures directory" in str(exc.value)


def test_load_fixtures_missing_audio_is_refused(promoted_fx):
    (promoted_fx / "audio" / "sweep-overlap-001.example.wav").unlink()
    with pytest.raises(ValueError) as exc:
        prcmd.load_fixtures(str(promoted_fx))
    assert "no example audio" in str(exc.value)


def test_renderer_output_on_a_real_promoted_dir(promoted_fx):
    recs = prcmd.load_fixtures(str(promoted_fx))
    env = prcmd.build_pr(recs, fixtures_dir=str(promoted_fx),
                         repo="owner/repo",
                         title="Add turn-taking regression fixtures")
    assert "`sweep-overlap-001`" in env["body"]
    assert "expect yield" in env["body"]
    assert env["run_command"] in env["body"]
    assert "—" not in env["body"]
    assert env["branch"] == "hotato/add-turn-taking-regression-fixtures"


# --- the CLI: dry run by default, git+gh only under --yes --------------------

def test_dry_run_prints_body_and_commands_and_touches_nothing(
        promoted_fx, fake_scm, capsys):
    rc = cli.main([
        "pr", "create", "--fixtures", str(promoted_fx), "--repo", "owner/repo",
        "--title", "Add turn-taking regression fixtures",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "## Fixtures added" in out
    assert "Dry run: nothing was created." in out
    assert "git checkout -b hotato/add-turn-taking-regression-fixtures" in out
    assert "gh pr create --repo owner/repo" in out
    # The one thing that must NOT happen: neither git nor gh was invoked.
    assert not fake_scm.exists(), "the dry run must not call git or gh"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="fake_scm's git/gh shims are bash scripts with a POSIX shebang, "
           "invoked by argv0 with no extension; Windows subprocess dispatch "
           "cannot execute them the way a real git/gh would be found on PATH",
)
def test_yes_runs_git_then_gh_with_the_body_piped_and_no_force_push(
        promoted_fx, fake_scm, capsys):
    rc = cli.main([
        "pr", "create", "--fixtures", str(promoted_fx), "--repo", "owner/repo",
        "--title", "Add turn-taking regression fixtures", "--yes",
    ])
    assert rc == 0
    assert fake_scm.exists(), "git and gh must run under --yes"
    rec = fake_scm.read_text(encoding="utf-8")
    branch = "hotato/add-turn-taking-regression-fixtures"
    # The feature branch is cut, the fixture files staged, committed, pushed.
    assert f"GIT: checkout -b {branch}" in rec
    assert "GIT: add " in rec
    assert "sweep-overlap-001.json" in rec
    assert "sweep-overlap-001.example.wav" in rec
    assert "GIT: commit -m" in rec
    assert f"GIT: push -u origin {branch}" in rec
    # Never a force-push, on any recorded line.
    assert "force" not in rec.lower()
    # gh opened the PR, with the rendered body piped on stdin, not inlined.
    assert "GH: pr create --repo owner/repo" in rec
    assert "## Fixtures added" in rec
    assert "https://github.com/owner/repo/pull/7" in capsys.readouterr().out


def test_json_dry_run_shape(promoted_fx, capsys):
    rc = cli.main([
        "pr", "create", "--fixtures", str(promoted_fx), "--repo", "owner/repo",
        "--title", "Add fixtures", "--format", "json",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tool"] == "hotato"
    assert payload["kind"] == "pr"
    assert payload["dry_run"] is True
    assert payload["created"] is False
    assert payload["repo"] == "owner/repo"
    assert payload["branch"].startswith("hotato/")
    assert payload["git_commands"][0][:3] == ["git", "checkout", "-b"]
    assert payload["gh_command"][:3] == ["gh", "pr", "create"]
    assert len(payload["fixtures"]) == 1


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the fake git/gh here are bash scripts with a POSIX shebang, "
           "invoked by argv0 with no extension; Windows subprocess dispatch "
           "cannot execute them the way a real git/gh would be found on PATH",
)
def test_git_failure_is_a_clean_usage_error_and_gh_never_runs(
        promoted_fx, tmp_path, monkeypatch, capsys):
    # A fake git that exits non-zero on the branch cut: the create must surface
    # a clean exit-2 usage error carrying git's own message, never a traceback,
    # and gh must never run after git fails.
    bindir = tmp_path / "failbin"
    bindir.mkdir()
    gh_marker = tmp_path / "gh-marker.txt"
    git = bindir / "git"
    git.write_text(
        "#!/usr/bin/env bash\n>&2 echo 'fatal: not a git repository'\nexit 1\n",
        encoding="utf-8",
    )
    gh = bindir / "gh"
    gh.write_text(
        '#!/usr/bin/env bash\necho called >> "$HOTATO_TEST_GH_MARKER"\n',
        encoding="utf-8",
    )
    for script in (git, gh):
        script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP
                     | stat.S_IXOTH)
    monkeypatch.setenv("HOTATO_TEST_GH_MARKER", str(gh_marker))
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    rc = cli.main([
        "pr", "create", "--fixtures", str(promoted_fx), "--repo", "owner/repo",
        "--title", "T", "--yes",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "git checkout -b" in err
    assert "failed" in err
    assert "not a git repository" in err
    assert not gh_marker.exists(), "gh must not run after git fails"


# --- required --repo / --fixtures / --title ----------------------------------

def test_missing_repo_is_a_clean_usage_error(promoted_fx, capsys):
    rc = cli.main([
        "pr", "create", "--fixtures", str(promoted_fx), "--title", "T",
    ])
    assert rc == 2
    assert "--repo" in capsys.readouterr().err


def test_missing_fixtures_is_a_clean_usage_error(capsys):
    rc = cli.main(["pr", "create", "--repo", "owner/repo", "--title", "T"])
    assert rc == 2
    assert "--fixtures" in capsys.readouterr().err


def test_missing_title_is_a_clean_usage_error(promoted_fx, capsys):
    rc = cli.main([
        "pr", "create", "--fixtures", str(promoted_fx), "--repo", "owner/repo",
    ])
    assert rc == 2
    assert "--title" in capsys.readouterr().err


def test_fixtures_dir_that_is_not_a_fixtures_dir_is_refused(tmp_path, capsys):
    (tmp_path / "notfx").mkdir()
    rc = cli.main([
        "pr", "create", "--fixtures", str(tmp_path / "notfx"),
        "--repo", "owner/repo", "--title", "T",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    # The refusal names BOTH accepted shapes: the fixtures directory and the
    # .hotato contract bundle.
    assert "not a hotato fixtures directory" in err
    assert "not a contract bundle" in err
    assert ".hotato" in err


# --- the second accepted shape: <id>.hotato contract bundles -----------------

@pytest.fixture()
def contract_bundle(tmp_path, monkeypatch):
    """A real `<id>.hotato` contract bundle in `contracts/` under the test
    cwd: label the top `hotato investigate` candidate from the bundled example
    call -- exactly the artifact `investigate label` hands a user, with its
    MANIFEST.sha256.json and attestation.json intact."""
    from hotato import investigate as _investigate

    monkeypatch.setenv("HOTATO_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    src = resources.files("hotato").joinpath(
        "data", "audio", "01-hard-interruption.example.wav")
    with resources.as_file(src) as p:
        shutil.copyfile(str(p), str(tmp_path / "call.wav"))
    state = str(tmp_path / "state.json")
    result, code = _investigate.run_investigate(
        str(tmp_path / "call.wav"), state_path=state)
    assert code == 0
    ref = result["next"][0]["ref"]
    label_result = _investigate.run_investigate_label(
        ref, expect="yield", out_dir="contracts")
    return tmp_path / label_result["dir"]


def _bundle_files(bundle_dir):
    out = {}
    for root, _dirs, files in os.walk(bundle_dir):
        for fn in files:
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, bundle_dir)
            with open(p, "rb") as fh:
                out[rel] = fh.read()
    return out


def test_dry_run_on_a_directory_of_bundles_builds_the_contract_body(
        contract_bundle, fake_scm, capsys):
    cid = contract_bundle.name[: -len(".hotato")]
    rc = cli.main([
        "pr", "create", "--fixtures", "contracts", "--repo", "owner/repo",
        "--title", "Add hotato contracts",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "## Contracts added (1)" in out
    assert f"`{cid}`" in out
    assert "expect yield" in out
    # the PR body carries the contract.json replay command and the verify gate
    assert "hotato run --stereo audio/event.wav --expect yield" in out
    assert "hotato contract verify tests/hotato/contracts/" in out
    # the plan stages the bundle WHOLE at its tests/hotato/contracts/ path
    assert f"git add tests/hotato/contracts/{cid}.hotato" in out
    assert f"copy contracts/{cid}.hotato -> tests/hotato/contracts/" in out
    # dry run: nothing ran, nothing was copied
    assert not fake_scm.exists(), "the dry run must not call git or gh"
    assert not os.path.exists("tests"), "the dry run must not copy the bundle"


def test_dry_run_on_a_single_bundle_path(contract_bundle, fake_scm, capsys):
    rc = cli.main([
        "pr", "create", "--fixtures", str(contract_bundle),
        "--repo", "owner/repo", "--title", "Add one hotato contract",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "## Contracts added (1)" in out
    assert f"`{contract_bundle.name[: -len('.hotato')]}`" in out
    assert not fake_scm.exists()


def test_json_dry_run_carries_the_contracts_list(contract_bundle, capsys):
    rc = cli.main([
        "pr", "create", "--fixtures", str(contract_bundle),
        "--repo", "owner/repo", "--title", "Add one hotato contract",
        "--format", "json",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "pr"
    assert payload["dry_run"] is True
    assert len(payload["contracts"]) == 1
    rec = payload["contracts"][0]
    assert rec["expect"] == "yield"
    assert rec["bundle_repo_path"].startswith("tests/hotato/contracts/")


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="fake_scm's git/gh shims are bash scripts with a POSIX shebang, "
           "invoked by argv0 with no extension; Windows subprocess dispatch "
           "cannot execute them the way a real git/gh would be found on PATH",
)
def test_yes_stages_the_bundle_byte_identical(contract_bundle, fake_scm,
                                              capsys):
    """--yes copies the bundle to tests/hotato/contracts/ and git-adds the
    directory whole. Every file inside the staged copy is byte-identical to
    the source bundle: the bundle is content-addressed, so a rewritten byte
    would break its attestation digest, its bundled-audio sha256 binding,
    and the sha256 manifest `contract pack` derives from it."""
    before = _bundle_files(contract_bundle)
    rc = cli.main([
        "pr", "create", "--fixtures", str(contract_bundle),
        "--repo", "owner/repo", "--title", "Add one hotato contract", "--yes",
    ])
    assert rc == 0
    capsys.readouterr()
    staged = os.path.join("tests", "hotato", "contracts", contract_bundle.name)
    assert os.path.isdir(staged)
    assert _bundle_files(staged) == before
    for required in ("contract.json", "attestation.json"):
        assert required in before
    rec = fake_scm.read_text(encoding="utf-8")
    assert f"GIT: add tests/hotato/contracts/{contract_bundle.name}" in rec
    assert "GH: pr create --repo owner/repo" in rec
    assert "## Contracts added" in rec


def test_existing_staged_bundle_is_refused_never_overwritten(
        contract_bundle, capsys):
    dest = os.path.join("tests", "hotato", "contracts", contract_bundle.name)
    os.makedirs(dest)
    rc = cli.main([
        "pr", "create", "--fixtures", str(contract_bundle),
        "--repo", "owner/repo", "--title", "T",
    ])
    assert rc == 2
    assert "already exists" in capsys.readouterr().err
