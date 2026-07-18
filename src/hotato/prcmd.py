"""``hotato pr create --fixtures DIR --repo OWNER/REPO --title T``: open a
pull request that adds promoted regression fixtures or contract bundles.

``--fixtures`` accepts BOTH artifact shapes hotato produces, detected by
shape, never a flag:

  * a fixtures directory -- the ``--out DIR`` that ``hotato fixture promote``
    (or ``fixture create``) writes, with a ``scenarios/`` folder of scenario
    JSON and an ``audio/`` folder of two-channel example WAVs. The PR body is
    a line per fixture (its id, the label a maintainer chose, the call it was
    promoted from, and the onset) plus the exact ``hotato run`` command that
    scores every added fixture. These are MEASURED CANDIDATE moments saved as
    tests, never verdicts and never intent.
  * a ``<id>.hotato`` contract bundle (what ``hotato investigate label`` /
    ``hotato contract create`` write), or a directory of them. The PR body is
    a line per contract (its id, the expected behavior, the measured outcome,
    and the replay command from its own ``contract.json``) plus the exact
    ``hotato contract verify`` command that re-scores every added bundle. The
    bundle is content-addressed (the attestation digest and the bundled-audio
    sha256 bindings in ``contract.json``), so it is staged WHOLE under
    ``tests/hotato/contracts/`` byte-identical -- nothing inside a bundle is
    ever rewritten.

Two honesty boundaries are structural, not prose:

  1. :func:`build_pr` is a PURE, OFFLINE renderer. It reads the already-loaded
     fixture records and emits the title, the body, and the exact ``git`` and
     ``gh`` argv it *would* run. It touches no network and shells out to
     nothing. The one filesystem read -- loading the scenarios off disk -- is
     isolated in :func:`load_fixtures`, exactly as ``issuecmd`` isolates its
     sweep-result read in ``load_sweep_result``.
  2. The only side effect, :func:`create_via_git_gh`, runs solely from the
     CLI's ``pr create`` path AND only when the caller passes ``--yes`` with an
     explicit ``--repo``. The default is a dry run that prints the body and the
     exact commands, changing nothing. Two invariants hold even under ``--yes``:
     the change lands on a NEW feature branch, never the default branch
     directly, and the push is never a force-push.

The fixture record shape is the SAME one ``hotato fixture create`` /
``fixture promote`` writes (:mod:`hotato.fixture`): ``scenarios/<id>.json``
carries the label under ``expected.yield`` and the promote provenance under
``provenance``; the example audio is ``audio/<id>.example.wav``. So the PR that
lands the fixtures and the command that scores them read the same files.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from typing import List, Optional, Sequence

from .errors import open_regular as _open_regular

__all__ = [
    "load_fixtures",
    "contract_bundle_dirs",
    "load_contracts",
    "build_pr",
    "build_contract_pr",
    "stage_bundle_copies",
    "branch_for",
    "render_gh_command",
    "create_via_git_gh",
    "DEFAULT_BRANCH_PREFIX",
    "PROTECTED_BRANCHES",
    "CONTRACTS_REPO_DIR",
]

# The feature branch a PR lands on is namespaced under this prefix so it is
# never the repo's default branch (main/master). The change is committed there,
# then the PR merges it into the base.
DEFAULT_BRANCH_PREFIX = "hotato/"

# Branch names we refuse to commit onto directly: committing fixtures straight
# onto the default branch is exactly what "open a pull request" avoids.
PROTECTED_BRANCHES = frozenset({"main", "master", "trunk", "develop", "HEAD"})

_SCENARIOS_SUBDIR = "scenarios"
_AUDIO_SUBDIR = "audio"

# The repo path a contract bundle is committed under: the same tests/hotato/
# home promoted fixtures use, one namespace deeper so scenario JSON and
# bundle directories never collide. `hotato contract verify
# tests/hotato/contracts/` is the CI gate over everything committed here.
CONTRACTS_REPO_DIR = "tests/hotato/contracts"


def _repo_path(path: str) -> str:
    """PR-facing form of a fixture path: "/" separators on every OS. A no-op
    on POSIX (``os.sep`` IS "/"); on Windows it keeps the rendered body, the
    ``git add`` plan, and the reproduce command in the separator git and
    GitHub use, and git on Windows accepts "/" pathspecs as-is."""
    return path.replace(os.sep, "/")


def _slug(text: str) -> str:
    """A lowercase hyphen slug for a branch name: letters and digits kept,
    every other run collapsed to a single hyphen, ends trimmed."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "fixtures"


def branch_for(title: str, *, prefix: str = DEFAULT_BRANCH_PREFIX) -> str:
    """The default feature branch derived from the PR title. Namespaced under
    :data:`DEFAULT_BRANCH_PREFIX` so it is a feature branch, not the default
    branch. Deterministic: the same title yields the same branch."""
    return prefix + _slug(title)


def _label(scenario: dict) -> str:
    """The maintainer's label for the fixture: 'yield' when the agent should
    stop for the caller, 'hold' when it should keep the floor. Read from the
    scenario the promote/create step wrote, never inferred here."""
    expected = scenario.get("expected") or {}
    return "yield" if expected.get("yield") else "hold"


def load_fixtures(fixtures_dir: str) -> List[dict]:
    """Read a hotato fixtures directory (``scenarios/<id>.json`` +
    ``audio/<id>.example.wav``) into a list of fixture records in id order.

    Filesystem read only; no network and no subprocess (the pure rendering
    happens in :func:`build_pr`). A directory with no ``scenarios/`` subfolder
    raises ValueError with the honest reason (exit 2). A scenario whose example
    audio is missing raises ValueError naming it, so the PR never claims to add
    a fixture that cannot be scored. A present-but-empty ``scenarios/`` returns
    ``[]``; the empty PR is refused later by :func:`build_pr`, mirroring the way
    ``issuecmd`` refuses an empty issue."""
    scenarios_dir = os.path.join(fixtures_dir, _SCENARIOS_SUBDIR)
    audio_dir = os.path.join(fixtures_dir, _AUDIO_SUBDIR)
    if not os.path.isdir(scenarios_dir):
        raise ValueError(
            f"{fixtures_dir!r} is not a hotato fixtures directory (no "
            f"{_SCENARIOS_SUBDIR}/ subfolder) and not a contract bundle (no "
            "contract.json, and no <id>.hotato bundle directly inside); "
            "point --fixtures at the --out DIR that hotato fixture promote "
            "wrote, e.g. tests/hotato, or at the <id>.hotato bundle that "
            "hotato investigate label wrote (or a directory of them)"
        )
    records: List[dict] = []
    for name in sorted(os.listdir(scenarios_dir)):
        if not name.endswith(".json"):
            continue
        scenario_path = os.path.join(scenarios_dir, name)
        with _open_regular(scenario_path, "r", encoding="utf-8") as fh:
            try:
                scenario = json.load(fh)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{scenario_path!r} is not JSON ({exc}); it is not a "
                    "hotato scenario file"
                ) from exc
        fid = scenario.get("id") or os.path.splitext(name)[0]
        audio_path = os.path.join(audio_dir, fid + ".example.wav")
        if not os.path.isfile(audio_path):
            raise ValueError(
                f"fixture {fid!r} has no example audio at {audio_path!r}; a "
                "fixture without its recording cannot be scored, so it will "
                "not be added"
            )
        provenance = scenario.get("provenance") or {}
        records.append({
            "id": fid,
            "title": scenario.get("title") or fid.replace("-", " "),
            "category": scenario.get("category"),
            "expect": _label(scenario),
            "caller_onset_sec": scenario.get("caller_onset_sec"),
            "duration_sec": scenario.get("duration_sec"),
            "source": provenance.get("source"),
            "source_onset_sec": provenance.get("source_onset_sec"),
            "candidate_ref": provenance.get("candidate_ref"),
            "candidate_kind": provenance.get("candidate_kind"),
            "created_by": provenance.get("created_by"),
            "scenario_path": _repo_path(scenario_path),
            "audio_path": _repo_path(audio_path),
        })
    return records


def contract_bundle_dirs(path: str) -> List[str]:
    """Contract-bundle directories at ``path``: ``path`` itself when it IS a
    ``<id>.hotato`` bundle (it has a ``contract.json``), else every immediate
    bundle subdirectory, resolved by :func:`hotato.contract.discover_bundles`
    -- the SAME resolver ``contract verify`` scans with, so what ``pr create``
    stages and what CI later verifies can never disagree. ``[]`` when the
    shape is neither, so the CLI falls through to :func:`load_fixtures` and
    its refusal names both accepted shapes."""
    if not os.path.isdir(path):
        return []
    from . import contract as _contract

    return _contract.discover_bundles(path)


def _verdict_text(measurement: dict) -> str:
    """The contract's measured outcome at creation, in the SAME honest
    vocabulary the contract itself records: PASS/FAIL when a verdict exists,
    NOT SCORABLE or 'verdict withheld' when the contract refuses one."""
    if not measurement.get("scorable"):
        return "NOT SCORABLE"
    if not measurement.get("verdict_eligible", True):
        return "verdict withheld"
    return "PASS" if measurement.get("passed") else "FAIL"


def load_contracts(bundle_dirs: Sequence[str]) -> List[dict]:
    """Read ``<id>.hotato`` contract bundles (what ``hotato investigate
    label`` / ``hotato contract create`` write) into contract records in id
    order.

    Filesystem read only, mirroring :func:`load_fixtures`; the pure rendering
    happens in :func:`build_contract_pr`. Each ``contract.json`` is loaded and
    schema-checked by the SAME loader ``contract verify`` uses. The record
    carries the bundle directory to stage WHOLE at
    ``tests/hotato/contracts/<name>``: the bundle is content-addressed (the
    attestation digest and the bundled-audio sha256 bindings), so it travels
    byte-identical and nothing inside it is ever rewritten. A destination
    that already exists and is not this bundle raises ValueError (exit 2),
    never an overwrite."""
    from . import contract as _contract

    records: List[dict] = []
    for bundle_dir in bundle_dirs:
        contract = _contract._load_contract(bundle_dir)
        cid = contract.get("id") or os.path.basename(
            os.path.normpath(bundle_dir))
        name = os.path.basename(os.path.normpath(bundle_dir))
        if not name.endswith(_contract.BUNDLE_SUFFIX):
            name = str(cid) + _contract.BUNDLE_SUFFIX
        dest = os.path.join(CONTRACTS_REPO_DIR, name)
        needs_copy = os.path.abspath(bundle_dir) != os.path.abspath(dest)
        if needs_copy and os.path.exists(dest):
            raise ValueError(
                f"{_repo_path(dest)!r} already exists and is not the bundle "
                f"at {bundle_dir!r}; remove it, or point --fixtures at it "
                "directly"
            )
        measurement = contract.get("measurement") or {}
        replay = contract.get("replay") or {}
        records.append({
            "id": cid,
            "expect": (contract.get("label") or {}).get("expected_behavior"),
            "reviewer": (contract.get("identity") or {}).get("reviewer"),
            "stack": (contract.get("source") or {}).get("stack"),
            "onset_sec": (contract.get("event") or {}).get("onset_sec"),
            "verdict": _verdict_text(measurement),
            "replay_command": replay.get("command"),
            "ci_command": replay.get("ci_command"),
            "bundle_src": bundle_dir,
            "bundle_repo_path": _repo_path(dest),
            "needs_copy": needs_copy,
        })
    records.sort(key=lambda r: str(r["id"]))
    return records


def render_gh_command(repo: str, title: str, branch: str,
                      base: Optional[str] = None) -> List[str]:
    """The exact ``gh pr create`` argv this would run. The body is piped on
    stdin (``--body-file -``) so the printed command and the opened PR carry
    byte-identical text. ``--head`` names the feature branch; ``--base`` is
    added only when the caller pins one (otherwise gh targets the repo
    default)."""
    argv = ["gh", "pr", "create", "--repo", repo, "--title", title,
            "--head", branch, "--body-file", "-"]
    if base:
        argv += ["--base", base]
    return argv


def _git_commands(branch: str, file_paths: Sequence[str],
                  commit_message: str) -> List[List[str]]:
    """The git argv sequence the create path runs: cut a NEW feature branch,
    stage exactly the fixture files, commit, and push that branch. The push is
    plain ``git push -u origin BRANCH`` -- never a force-push and never a
    ``+`` refspec, so it can only fast-forward the new branch it just made."""
    return [
        ["git", "checkout", "-b", branch],
        ["git", "add", *file_paths],
        ["git", "commit", "-m", commit_message],
        ["git", "push", "-u", "origin", branch],
    ]


def _fixture_line(fx: dict) -> str:
    onset = fx.get("caller_onset_sec")
    onset_txt = f"{onset:.2f}s" if isinstance(onset, (int, float)) else "n/a"
    src = fx.get("source") or "a real call"
    ref = fx.get("candidate_ref")
    from_txt = f"`{src}`"
    if ref:
        from_txt += f" ({fx['candidate_kind']} candidate `{ref}`)" if \
            fx.get("candidate_kind") else f" (candidate `{ref}`)"
    return (f"- `{fx['id']}` (expect {fx['expect']}): from {from_txt} at "
            f"clip onset {onset_txt}")


def _run_command(fixtures_dir: str) -> str:
    scenarios = _repo_path(os.path.join(fixtures_dir, _SCENARIOS_SUBDIR))
    audio = _repo_path(os.path.join(fixtures_dir, _AUDIO_SUBDIR))
    return (f"hotato run --scenarios {scenarios} --audio {audio} "
            "--format text")


def _validated_branch(branch: Optional[str], title: str,
                      base: Optional[str]) -> str:
    """The feature branch a PR lands on, defaulted from the title and refused
    when it is a protected/default branch or equals ``base`` -- the change
    always lands on a NEW branch, never the default branch directly."""
    branch = branch or branch_for(title)
    if branch in PROTECTED_BRANCHES:
        raise ValueError(
            f"--branch {branch!r} is a default/protected branch; open the "
            "pull request from a feature branch (the change is committed "
            "there, never onto the default branch directly)"
        )
    if base and branch == base:
        raise ValueError(
            f"--branch and --base are both {branch!r}; the feature branch must "
            "differ from the base it merges into"
        )
    return branch


def build_pr(
    fixtures: Sequence[dict],
    *,
    fixtures_dir: str,
    repo: str,
    title: str,
    branch: Optional[str] = None,
    base: Optional[str] = None,
) -> dict:
    """Render the pull request from the loaded fixture records. PURE and
    OFFLINE: no network, no subprocess, no filesystem read. Returns the title,
    the feature branch, the base, the markdown body, the exact ``git`` argv
    sequence and the ``gh pr create`` argv (with shell-quoted display forms),
    the commit message, and the machine list of fixtures.

    The feature branch defaults to :func:`branch_for` (``hotato/<title slug>``)
    and is refused if it is a protected/default branch or equals ``base`` --
    the change always lands on a NEW branch, never the default branch directly.
    Raises ValueError when there are no fixtures to add (never opens an empty
    PR)."""
    fixtures = list(fixtures)
    if not fixtures:
        raise ValueError(
            f"{fixtures_dir} has no fixtures to add (its scenarios/ folder is "
            "empty); there is nothing to open a pull request about"
        )

    branch = _validated_branch(branch, title, base)

    file_paths: List[str] = []
    for fx in fixtures:
        file_paths.append(fx["scenario_path"])
        file_paths.append(fx["audio_path"])

    n = len(fixtures)
    noun = "fixture" if n == 1 else "fixtures"
    commit_message = f"Add {n} hotato turn-taking regression {noun}"

    run_cmd = _run_command(fixtures_dir)
    intro = [
        f"This pull request adds {n} turn-taking regression {noun} promoted "
        "from candidate moments in real calls.",
        "",
        "Each fixture pins a measured timing moment with the label a "
        "maintainer chose: `yield` means the agent should have stopped for the "
        "caller, `hold` means it should have kept the floor through a "
        "backchannel or noise. Hotato measures whether the timing matched that "
        "label; it does not infer intent. These are measured candidates, not "
        "verdicts.",
        "",
        f"## Fixtures added ({n})",
        "",
    ]
    lines = [_fixture_line(fx) for fx in fixtures]
    reproduce = [
        "",
        "## Run them",
        "",
        "Score every added fixture (a promoted fixture is allowed to fail; "
        "that is the regression it pins):",
        "",
        "```",
        run_cmd,
        "```",
    ]
    footer = [
        "",
        "---",
        "",
        "Promoted candidate moments from a hotato sweep, saved as permanent "
        "tests. Energy is not intent and Hotato infers none. The audio was "
        "clipped and scored offline; nothing left the machine it was promoted "
        "on.",
    ]
    body = "\n".join(intro + lines + reproduce + footer) + "\n"

    git_commands = _git_commands(branch, file_paths, commit_message)
    gh_command = render_gh_command(repo, title, branch, base)

    return {
        "tool": "hotato",
        "kind": "pr",
        "schema_version": "1",
        "repo": repo,
        "title": title,
        "branch": branch,
        "base": base,
        "commit_message": commit_message,
        "run_command": run_cmd,
        "fixtures": [
            {
                "id": fx["id"],
                "expect": fx["expect"],
                "category": fx["category"],
                "source": fx["source"],
                "candidate_ref": fx["candidate_ref"],
                "candidate_kind": fx["candidate_kind"],
                "caller_onset_sec": fx["caller_onset_sec"],
                "scenario_path": fx["scenario_path"],
                "audio_path": fx["audio_path"],
            }
            for fx in fixtures
        ],
        "body": body,
        "git_commands": git_commands,
        "git_commands_display": [
            " ".join(shlex.quote(a) for a in cmd) for cmd in git_commands
        ],
        "gh_command": gh_command,
        "gh_command_display": " ".join(shlex.quote(a) for a in gh_command),
    }


def _contract_line(rec: dict) -> str:
    onset = rec.get("onset_sec")
    onset_txt = f"{onset:.2f}s" if isinstance(onset, (int, float)) else "n/a"
    line = (f"- `{rec['id']}` (expect {rec['expect']}): measured "
            f"{rec['verdict']} at onset {onset_txt}")
    if rec.get("replay_command"):
        line += (f"; replay `{rec['replay_command']}` from "
                 f"`{rec['bundle_repo_path']}/`")
    return line


def _verify_command() -> str:
    return f"hotato contract verify {CONTRACTS_REPO_DIR}/ --junit hotato.xml"


def build_contract_pr(
    contracts: Sequence[dict],
    *,
    contracts_src: str,
    repo: str,
    title: str,
    branch: Optional[str] = None,
    base: Optional[str] = None,
) -> dict:
    """Render the pull request from loaded contract records. PURE and
    OFFLINE, exactly like :func:`build_pr`: no network, no subprocess, no
    filesystem read or write. Returns the same envelope shape with a
    ``contracts`` list in place of ``fixtures``, plus the ``stage_copies``
    plan the CLI executes (only under ``--yes``, via
    :func:`stage_bundle_copies`) to place each bundle byte-identical under
    ``tests/hotato/contracts/`` before ``git add`` stages it whole.

    The same invariants as :func:`build_pr` hold: a NEW namespaced feature
    branch (never a protected/default branch, never equal to ``base``), a
    plain push that is never a force-push, and a refusal when there is
    nothing to add."""
    contracts = list(contracts)
    if not contracts:
        raise ValueError(
            f"{contracts_src} has no contracts to add; there is nothing to "
            "open a pull request about"
        )

    branch = _validated_branch(branch, title, base)

    file_paths = [rec["bundle_repo_path"] for rec in contracts]
    stage_copies = [
        {"src": rec["bundle_src"], "dest": rec["bundle_repo_path"]}
        for rec in contracts if rec.get("needs_copy")
    ]

    n = len(contracts)
    noun = "contract" if n == 1 else "contracts"
    commit_message = f"Add {n} hotato turn-taking failure {noun}"

    run_cmd = _verify_command()
    intro = [
        f"This pull request adds {n} turn-taking failure {noun}: the signed, "
        "content-addressed `<id>.hotato` bundle a maintainer created with "
        "`hotato investigate label` (or `hotato contract create`).",
        "",
        "Each contract pins one measured timing moment with the label a "
        "maintainer chose: `yield` means the agent should have stopped for "
        "the caller, `hold` means it should have kept the floor through a "
        "backchannel or noise. The bundle carries the clipped audio, the "
        "frame-level evidence, the policy, and its own manifest and "
        "attestation, and it is committed byte-identical, so "
        "`hotato contract verify` re-scores the exact audio it shipped "
        "with and refuses a bundle edited after creation. Hotato measures "
        "whether the timing matched the label; it does not infer intent.",
        "",
        f"## Contracts added ({n})",
        "",
    ]
    lines = [_contract_line(rec) for rec in contracts]
    reproduce = [
        "",
        "## Verify them",
        "",
        "Re-score every added contract against its own policy (a contract "
        "created from a bad call is allowed to measure FAIL; that is the "
        "regression it pins):",
        "",
        "```",
        run_cmd,
        "```",
    ]
    footer = [
        "",
        "---",
        "",
        "Contract bundles created from measured timing moments in real "
        "calls, saved as permanent, re-verifiable tests. Energy is not "
        "intent and Hotato infers none. The audio was clipped and scored "
        "offline; nothing left the machine it was labeled on.",
    ]
    body = "\n".join(intro + lines + reproduce + footer) + "\n"

    git_commands = _git_commands(branch, file_paths, commit_message)
    gh_command = render_gh_command(repo, title, branch, base)

    return {
        "tool": "hotato",
        "kind": "pr",
        "schema_version": "1",
        "repo": repo,
        "title": title,
        "branch": branch,
        "base": base,
        "commit_message": commit_message,
        "run_command": run_cmd,
        "contracts": [
            {
                "id": rec["id"],
                "expect": rec["expect"],
                "verdict": rec["verdict"],
                "stack": rec["stack"],
                "onset_sec": rec["onset_sec"],
                "replay_command": rec["replay_command"],
                "bundle_src": rec["bundle_src"],
                "bundle_repo_path": rec["bundle_repo_path"],
            }
            for rec in contracts
        ],
        "body": body,
        "stage_copies": stage_copies,
        "stage_copies_display": [
            f"copy {_repo_path(cp['src'])} -> {_repo_path(cp['dest'])} (byte-identical)"
            for cp in stage_copies
        ],
        "git_commands": git_commands,
        "git_commands_display": [
            " ".join(shlex.quote(a) for a in cmd) for cmd in git_commands
        ],
        "gh_command": gh_command,
        "gh_command_display": " ".join(shlex.quote(a) for a in gh_command),
    }


def stage_bundle_copies(copies: Sequence[dict]) -> None:
    """Copy each bundle directory to its repo path, whole and byte-identical
    (``shutil.copytree``; file bytes are never rewritten, since the bundle is
    content-addressed and any mutation would break its attestation digest
    and audio-hash bindings). A filesystem side effect, so the CLI calls it only
    under ``--yes`` -- the dry run prints the copy plan and changes nothing.
    An existing destination is refused, never overwritten (the loader applies
    the same check earlier, at read time)."""
    for cp in copies:
        dest = cp["dest"]
        if os.path.exists(dest):
            raise ValueError(
                f"{dest!r} already exists; remove it, or point --fixtures at "
                "it directly"
            )
        parent = os.path.dirname(dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
        shutil.copytree(cp["src"], dest)


def create_via_git_gh(
    git_commands: Sequence[Sequence[str]],
    gh_command: Sequence[str],
    body: str,
    *,
    cwd: Optional[str] = None,
) -> dict:
    """Run the git argv sequence, then ``gh pr create`` piping ``body`` on
    stdin. The ONLY side effect in this module; the CLI calls it solely under
    ``--yes`` with an explicit ``--repo``. Runs the git steps in order and
    stops at the first non-zero one, so a failed branch cut never proceeds to a
    commit. Returns ``{"ok", "failed_command", "returncode", "stdout",
    "stderr", "pr_url"}``. A missing ``git``/``gh`` binary raises
    FileNotFoundError, which the CLI surfaces as the standard exit-2 structured
    error."""
    for argv in git_commands:
        proc = subprocess.run(
            list(argv), cwd=cwd, text=True, capture_output=True,
        )
        if proc.returncode != 0:
            return {
                "ok": False,
                "failed_command": " ".join(shlex.quote(a) for a in argv),
                "returncode": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "pr_url": None,
            }
    proc = subprocess.run(
        list(gh_command), input=body, cwd=cwd, text=True, capture_output=True,
    )
    if proc.returncode != 0:
        return {
            "ok": False,
            "failed_command": " ".join(shlex.quote(a) for a in gh_command),
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "pr_url": None,
        }
    return {
        "ok": True,
        "failed_command": None,
        "returncode": 0,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "pr_url": proc.stdout.strip(),
    }
