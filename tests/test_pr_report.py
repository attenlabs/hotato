"""``hotato pr comment`` (src/hotato/pr_report.py) and the root Action's
opt-in poster (ci/action/pr_comment_post.py).

* rendering goldens: one per machine-result kind, byte-for-byte;
* determinism: the same inputs render the same bytes, in-process and via the
  CLI;
* the red-to-green comparison: fixed / newly failing / still failing / now
  inconclusive / removed, by case key;
* Markdown safety of values read off result files;
* the sticky-comment logic against a fake GitHub API: update in place by the
  hidden marker (including the older contract-verify marker), never a second
  comment, no write when the body is unchanged, pagination;
* the fork and no-permission fallbacks: the report lands on the job summary
  and the poster still exits 0;
* the gate renders the report leaf, passes the baseline, and never lets the
  report change the exit code.

To regenerate the goldens after a REVIEWED rendering change, run from the
repository root::

    PYTHONPATH=src python3 tests/test_pr_report.py --regen
"""

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import urllib.error
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from hotato import pr_report as R  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures", "pr-report")
RECORDED = os.path.join(ROOT, "tests", "fixtures", "action-consumer",
                        "recorded")
GOLDEN = os.path.join(ROOT, "tests", "golden", "pr_report")
ACTION_DIR = os.path.join(ROOT, "ci", "action")
RUN_URL = "https://github.com/octo/voice/actions/runs/123"


def _load(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _fix(name):
    return _load(os.path.join(FIX, name))


def _rec(name):
    return _load(os.path.join(RECORDED, name))


def _load_module(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(ACTION_DIR, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# goldens
# ---------------------------------------------------------------------------

def _golden_cases():
    """Each golden: (file name, rendered body). Paths stay relative so the
    bytes do not depend on where the checkout lives."""
    contract_records = {
        tid: os.path.relpath(path, FIX).replace(os.sep, "/")
        for tid, path in R.load_records_index(
            os.path.join(FIX, "records", "index.json")).items()
    }
    return [
        ("contract-verify.md", R.render_pr_report(
            _fix("contract-verify.head.json"),
            base_doc=_fix("contract-verify.base.json"), base_label="main",
            records=contract_records,
            cards={"greeting-hold": "cards/greeting-hold.svg"},
            run_url=RUN_URL,
            reproduce="hotato contract verify contracts --format json")),
        ("suite-run.md", R.render_pr_report(
            _rec("suite-run.mixed-fail.json"),
            base_doc=_rec("suite-run.pass.json"), base_label="main",
            run_url=RUN_URL)),
        ("test-run.md", R.render_pr_report(
            _rec("test-run.inconclusive.json"),
            base_doc=_rec("test-run.advisory-unavailable.json"))),
    ]


@pytest.mark.parametrize("name,body", _golden_cases(),
                         ids=[c[0] for c in _golden_cases()])
def test_rendering_matches_golden(name, body):
    with open(os.path.join(GOLDEN, name), "r", encoding="utf-8",
              newline="") as fh:
        assert body == fh.read(), (
            f"{name} drifted; regenerate after review with "
            "PYTHONPATH=src python3 tests/test_pr_report.py --regen")


def test_golden_contract_report_content():
    body = _golden_cases()[0][1]
    lines = body.splitlines()
    assert lines[0] == R.MARKER  # the sticky marker is the first line
    assert lines[1] == "## hotato: FAIL"
    # the not-passing table names the case, what failed, and the evidence
    assert ("| **FAIL** | `greeting-hold` | turn-taking regression \\(expect"
            " hold\\) | agent yielded after 0.36 s; talk-over 0.36 s |") in body
    assert "| NOT_SCORABLE | `callback-yield` |" in body
    # red to green against the base
    assert "Fixed: **1** · Newly failing: **1** · Still failing: **0**" in body
    assert "| newly failing | `greeting-hold` | PASS | **FAIL** |" in body
    assert "| fixed | `refund-yield` | FAIL | PASS |" in body
    assert "| now inconclusive | `callback-yield` | not on base |" in body
    assert "1 case(s) on the base are not in this run." in body
    # evidence files, linked through the run
    assert f"[record]({RUN_URL}) · [card]({RUN_URL})" in body
    assert "evidence card `cards/greeting-hold.svg`" in body
    assert "failure-record.md`" in body
    # the caught moment keeps the ASCII barge-in timeline
    assert "caller  " in body and "agent   " in body


def test_rendering_is_deterministic():
    first = [b for _, b in _golden_cases()]
    second = [b for _, b in _golden_cases()]
    assert first == second


def test_cli_renders_same_bytes_twice(tmp_path):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "src") + os.pathsep + env.get(
        "PYTHONPATH", "")
    argv = [sys.executable, "-m", "hotato", "pr", "comment",
            os.path.join(FIX, "contract-verify.head.json"),
            "--base", os.path.join(FIX, "contract-verify.base.json"),
            "--base-label", "main", "--run-url", RUN_URL]
    outs = [subprocess.run(argv, env=env, capture_output=True, timeout=120)
            for _ in range(2)]
    for p in outs:
        assert p.returncode == 0, p.stderr
    assert outs[0].stdout == outs[1].stdout
    assert outs[0].stdout.startswith(R.MARKER.encode())
    # the lab spelling dispatches to the same parser and bytes
    lab = subprocess.run(argv[:3] + ["lab"] + argv[3:], env=env,
                         capture_output=True, timeout=120)
    assert lab.returncode == 0 and lab.stdout == outs[0].stdout


# ---------------------------------------------------------------------------
# verdicts and units
# ---------------------------------------------------------------------------

def test_verdicts_follow_the_machine_result():
    cases = {
        "suite-run.pass.json": "pass",
        "suite-run.mixed-fail.json": "fail",
        "test-run.inconclusive.json": "inconclusive",
        "contract-verify.pass.json": "pass",
    }
    for name, want in cases.items():
        doc = _rec(name)
        assert R.verdict_of(doc, R.units_of(doc)) == want, name
    head = _fix("contract-verify.head.json")
    assert R.verdict_of(head, R.units_of(head)) == "fail"


def test_inconclusive_is_never_rendered_as_pass_or_fail():
    doc = _rec("test-run.inconclusive.json")
    units = R.units_of(doc)
    statuses = [u["status"] for u in units]
    assert statuses.count("INCONCLUSIVE") == 3 and "FAIL" not in statuses
    body = R.render_pr_report(doc)
    assert "## hotato: INCONCLUSIVE" in body
    assert "**FAIL**" not in body


def test_contract_embedded_assertions_are_a_separate_row():
    doc = _fix("contract-verify.head.json")
    doc["results"][2]["assertions"] = {
        "exit_code": 1,
        "results": [{"id": "said-refund", "status": "FAIL",
                     "reason": "phrase not found"}],
    }
    units = {u["key"]: u for u in R.units_of(doc)}
    # the timing axis is untouched; the assertion axis is its own row
    assert units["refund-yield"]["status"] == "PASS"
    row = units["refund-yield#assertions"]
    assert row["status"] == "FAIL"
    assert "said-refund: phrase not found" == row["evidence"]


def test_tampered_contract_is_red():
    doc = _fix("contract-verify.head.json")
    doc["results"][2]["authenticity"] = "tampered"
    doc["results"][2]["authenticity_reason"] = "canonical digest mismatch"
    units = {u["key"]: u for u in R.units_of(doc)}
    assert units["refund-yield"]["status"] == "TAMPERED"
    assert "| **TAMPERED** | `refund-yield` |" in R.render_pr_report(doc)


def test_unknown_kind_is_refused(tmp_path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"kind": "fix-plan"}), encoding="utf-8")
    doc, err = R.load_result(str(p))
    assert doc is None and "kind" in err
    with pytest.raises(ValueError):
        R.render_pr_report({"kind": "fix-plan"})
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "src")
    proc = subprocess.run([sys.executable, "-m", "hotato", "pr", "comment",
                           str(p)], env=env, capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 2


# ---------------------------------------------------------------------------
# red to green
# ---------------------------------------------------------------------------

def _u(key, status):
    return {"key": key, "case": key, "status": status}


def test_diff_categories():
    head = [_u("a", "FAIL"), _u("b", "FAIL"), _u("c", "PASS"),
            _u("d", "PASS"), _u("e", "INCONCLUSIVE"), _u("f", "FAIL"),
            _u("g", "PASS"), _u("h", "REFUSED")]
    base = [_u("a", "PASS"), _u("b", "FAIL"), _u("c", "FAIL"),
            _u("d", "PASS"), _u("e", "PASS"), _u("g", "INCONCLUSIVE"),
            _u("h", "NOT_SCORABLE"), _u("gone", "FAIL")]
    d = R.diff_units(head, base)
    keys = {k: [u["key"] for u, _ in v] for k, v in d.items()
            if k != "removed"}
    assert keys["newly_failing"] == ["a", "f"]   # f is new on this PR
    assert keys["still_failing"] == ["b"]
    assert keys["fixed"] == ["c", "g"]           # red or amber -> PASS
    assert keys["now_inconclusive"] == ["e"]     # h stays amber: no change
    assert [b["key"] for b in d["removed"]] == ["gone"]
    assert [bs for _, bs in d["newly_failing"]] == ["PASS", None]


def test_duplicate_case_ids_get_stable_unique_keys():
    doc = _fix("contract-verify.head.json")
    doc["results"].append(dict(doc["results"][1]))
    keys = [u["key"] for u in R.units_of(doc)]
    assert len(keys) == len(set(keys))
    assert "greeting-hold~2" in keys


def test_base_missing_unreadable_or_other_kind_is_a_line_not_an_error(
        tmp_path):
    head = _fix("contract-verify.head.json")
    body = R.render_pr_report(head)
    assert "No base result was supplied" in body
    other = R.render_pr_report(head, base_doc=_rec("suite-run.pass.json"))
    assert "they are not compared" in other
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "src")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    for base, want in ((tmp_path / "absent.json", "No base result is"
                        " available"), (bad, "could not be used")):
        proc = subprocess.run(
            [sys.executable, "-m", "hotato", "pr", "comment",
             os.path.join(FIX, "contract-verify.head.json"), "--base",
             str(base)], env=env, capture_output=True, text=True,
            timeout=120)
        assert proc.returncode == 0, proc.stderr
        assert want in proc.stdout
        assert str(tmp_path) not in proc.stdout  # no local path leaks


def test_no_change_against_identical_base():
    head = _fix("contract-verify.head.json")
    body = R.render_pr_report(head, base_doc=head)
    assert "Still failing: **1**" in body  # greeting-hold red on both
    assert "| still failing | `greeting-hold` | FAIL | **FAIL** |" in body


# ---------------------------------------------------------------------------
# safety and size
# ---------------------------------------------------------------------------

def test_hostile_values_cannot_break_the_table_or_mention():
    doc = _fix("contract-verify.head.json")
    r = doc["results"][0]
    r["id"] = "x|y`z"
    r["not_scorable_reason"] = "<script>alert(1)</script> | @octocat\nrow"
    body = R.render_pr_report(doc)
    row = next(line for line in body.splitlines() if "x\\|y'z" in line)
    assert row.count(" | ") == 4  # still exactly five cells
    assert "<script>" not in body and "&lt;script&gt;" in body
    assert "@octocat" not in body and "&#64;octocat" in body


def test_markdown_links_images_and_autolinks_render_inert():
    doc = _fix("contract-verify.head.json")
    r = doc["results"][0]
    r["not_scorable_reason"] = ("[click](http://evil.example)"
                                " ![i](http://e/x.png) **bold** _em_ ~~s~~"
                                " https://bare.example www.bare.example"
                                " back\\slash")
    body = R.render_pr_report(doc)
    row = next(line for line in body.splitlines()
               if "click" in line and line.startswith("|"))
    # no link, image, emphasis, or bare-URL autolink can survive the cell
    assert "](" not in row and "![" not in row
    assert "\\[click\\]\\(http&#58;//evil.example\\)" in row
    assert "\\!\\[i\\]" in row
    assert "\\*\\*bold\\*\\*" in row and "\\_em\\_" in row
    assert "\\~\\~s\\~\\~" in row
    assert "https://" not in row and "http://" not in row
    assert "www&#46;bare.example" in row
    assert "back\\\\slash" in row
    assert row.count(" | ") == 4  # still exactly five cells
    # the only links in the whole body are the ones the renderer built
    assert "evil.example)" not in body.replace("\\)", "")


def test_link_targets_must_be_plain_https_urls(tmp_path):
    assert R.safe_url(RUN_URL) == RUN_URL
    for bad in ("http://github.com/o/r/actions/runs/1",
                "javascript:alert(1)",
                "https://x.example/a) ![i](https://e/x.png",
                "https://x.example/a b", 'https://x.example/"q"',
                "https://x.example/[a]", None, 7, ""):
        assert R.safe_url(bad) is None, bad
    # the renderer drops a bad target rather than rendering it
    doc = _fix("contract-verify.head.json")
    body = R.render_pr_report(doc, run_url="https://x.example/a) ![i](y")
    assert "x.example" not in body
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "src")
    head = os.path.join(FIX, "contract-verify.head.json")
    for flags in (["--run-url", "https://x.example/a) ![i](y"],
                  ["--artifact-url", "http://x.example/a"],
                  ["--max-rows", "0"], ["--max-rows", "-3"]):
        proc = subprocess.run([sys.executable, "-m", "hotato", "pr",
                               "comment", head] + flags, env=env,
                              capture_output=True, text=True, timeout=120)
        assert proc.returncode == 2, (flags, proc.stdout, proc.stderr)
        assert proc.stdout == ""


def test_rows_are_capped_and_counted():
    doc = _fix("contract-verify.head.json")
    tmpl = doc["results"][1]
    doc["results"] = []
    for i in range(30):
        r = dict(tmpl)
        r["id"] = f"case-{i:02d}"
        doc["results"].append(r)
    body = R.render_pr_report(doc, max_rows=5)
    assert "### Not passing (30)" in body
    assert "+25 more not shown" in body
    assert "`case-05`" not in body.split("### Caught moment")[0]
    assert len(body) < 65536


# ---------------------------------------------------------------------------
# evidence files
# ---------------------------------------------------------------------------

def test_records_index_maps_test_ids_and_rejects_traversal(tmp_path):
    idx = tmp_path / "index.json"
    idx.write_text(json.dumps({
        "kind": "hotato.failure-record-index.v1",
        "records": [
            {"test_id": "ok", "directory": "sha256-abc"},
            {"test_id": "evil", "directory": "../../etc"},
            {"test_id": "dot", "directory": ".."},
        ]}), encoding="utf-8")
    got = R.load_records_index(str(idx))
    assert set(got) == {"ok"}
    assert got["ok"].endswith(os.path.join("sha256-abc", "failure-record.md"))
    assert R.load_records_index(str(tmp_path / "missing.json")) == {}


def _make_contract(tmp_path, cid, expect):
    from importlib import resources
    audio = str(resources.files("hotato").joinpath(
        "data", "audio", "01-hard-interruption.example.wav"))
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "src")
    proc = subprocess.run(
        [sys.executable, "-m", "hotato", "contract", "create", "--stereo",
         audio, "--onset", "2.39", "--expect", expect, "--id", cid, "--out",
         "contracts"], cwd=str(tmp_path), env=env, capture_output=True,
        text=True, timeout=300)
    assert proc.returncode == 0, proc.stderr


def test_cli_end_to_end_with_cards_and_records(tmp_path):
    _make_contract(tmp_path, "greeting-hold", "hold")
    _make_contract(tmp_path, "refund-yield", "yield")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "src")

    def run(*argv):
        return subprocess.run([sys.executable, "-m", "hotato", *argv],
                              cwd=str(tmp_path), env=env,
                              capture_output=True, text=True, timeout=300)

    v = run("contract", "verify", "contracts", "--format", "json")
    assert v.returncode == 1
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "contract-verify.json").write_text(
        v.stdout, encoding="utf-8")
    rec = run("record", "render", "results/contract-verify.json", "--all",
              "--out", "results/records")
    assert rec.returncode == 0, rec.stderr
    p = run("pr", "comment", "results/contract-verify.json",
            "--records-index", "results/records/index.json",
            "--cards", "results/cards", "--artifact-root", "results",
            "--run-url", RUN_URL, "--out", "results/pr-report.md")
    assert p.returncode == 0, p.stderr
    body = (tmp_path / "results" / "pr-report.md").read_text(
        encoding="utf-8")
    card = tmp_path / "results" / "cards" / "greeting-hold.svg"
    assert card.is_file() and card.read_text().startswith("<svg")
    # only the failing contract gets a card
    assert sorted(os.listdir(tmp_path / "results" / "cards")) == [
        "greeting-hold.svg"]
    assert "evidence card `cards/greeting-hold.svg`" in body
    assert "Failure Record `records/sha256-" in body
    assert str(tmp_path) not in body
    # json format carries the verdict and the same Markdown
    j = run("pr", "comment", "results/contract-verify.json", "--format",
            "json")
    payload = json.loads(j.stdout)
    assert payload["verdict"] == "fail"
    assert payload["markdown"].startswith(R.MARKER)


# ---------------------------------------------------------------------------
# the poster, against a fake GitHub API
# ---------------------------------------------------------------------------

class FakeGitHub:
    """An in-process stand-in for ``GET /user`` and the three issue-comment
    endpoints the poster uses. Records every call; ``fail`` makes every call
    raise an HTTPError with that status. ``login`` None is the workflow
    token, which ``/user`` refuses with 403; a string is a personal token."""

    def __init__(self, comments=None, fail=None, login=None):
        self.comments = list(comments or [])
        self.calls = []
        self.fail = fail
        self.login = login
        self.next_id = 1000

    def request(self, url, token, *, method="GET", payload=None):
        self.calls.append((method, url, payload))
        if self.fail:
            raise urllib.error.HTTPError(url, self.fail, "nope", {},
                                         io.BytesIO(b""))
        if method == "GET" and urlsplit(url).path == "/user":
            if self.login is None:
                raise urllib.error.HTTPError(url, 403, "integration", {},
                                             io.BytesIO(b""))
            return {"login": self.login}
        if method == "GET":
            page = int(parse_qs(urlsplit(url).query)["page"][0])
            return self.comments[(page - 1) * 100: page * 100]
        if method == "PATCH":
            cid = int(url.rsplit("/", 1)[1])
            for c in self.comments:
                if c["id"] == cid:
                    c["body"] = payload["body"]
            return {"id": cid}
        self.next_id += 1
        self.comments.append({"id": self.next_id, "body": payload["body"],
                              "user": {"login": self.login
                                       or "github-actions[bot]"}})
        return {"id": self.next_id}

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


@pytest.fixture
def poster():
    return _load_module("pr_comment_post_t", "pr_comment_post.py")


def _pr_env(monkeypatch, tmp_path, body, event=None):
    for name in ("HOTATO_PR_COMMENT_FILE", "HOTATO_PR_COMMENT_TOKEN",
                 "HOTATO_PR_COMMENT_SUMMARY", "GITHUB_TOKEN",
                 "GITHUB_REPOSITORY", "GITHUB_EVENT_PATH", "GITHUB_REF",
                 "GITHUB_API_URL", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(name, raising=False)
    leaf = tmp_path / "pr-report.md"
    leaf.write_text(body, encoding="utf-8")
    ev = tmp_path / "event.json"
    ev.write_text(json.dumps(event or {"pull_request": {
        "number": 5,
        "head": {"repo": {"full_name": "octo/voice"}},
        "base": {"repo": {"full_name": "octo/voice"}}}}), encoding="utf-8")
    summary = tmp_path / "step-summary.md"
    monkeypatch.setenv("HOTATO_PR_COMMENT_FILE", str(leaf))
    monkeypatch.setenv("HOTATO_PR_COMMENT_TOKEN", "tok")
    monkeypatch.setenv("HOTATO_PR_COMMENT_SUMMARY", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo/voice")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(ev))
    monkeypatch.setenv("GITHUB_API_URL", "https://api.example")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    return summary


def test_marker_lockstep(poster):
    import hotato.contract as contract_mod
    assert poster.REPORT_MARKER == R.MARKER
    assert poster.MARKERS == (R.MARKER,) + R.LEGACY_MARKERS
    assert R.LEGACY_MARKERS == (contract_mod._PR_COMMENT_MARKER,)


def test_poster_creates_once_then_updates_in_place(poster, monkeypatch,
                                                   tmp_path):
    body = _golden_cases()[0][1]
    summary = _pr_env(monkeypatch, tmp_path, body)
    gh = FakeGitHub([{"id": 1, "body": "a review comment",
                      "user": {"login": "reviewer"}}])
    monkeypatch.setattr(poster, "_request", gh.request)
    assert poster.main() == 0
    assert [c[0] for c in gh.writes()] == ["POST"]
    assert "/repos/octo/voice/issues/5/comments" in gh.writes()[0][1]
    # a second run with a new body PATCHes the same comment, no second POST
    (tmp_path / "pr-report.md").write_text(body + "more\n", encoding="utf-8")
    assert poster.main() == 0
    assert [c[0] for c in gh.writes()] == ["POST", "PATCH"]
    assert len([c for c in gh.comments if R.MARKER in c["body"]]) == 1
    # a third run with an identical body writes nothing at all
    assert poster.main() == 0
    assert [c[0] for c in gh.writes()] == ["POST", "PATCH"]
    # the job summary mirrors the body on every run, with no fallback note
    text = summary.read_text(encoding="utf-8")
    assert text.count(R.MARKER) == 3
    assert "Not posted" not in text


def test_poster_updates_the_legacy_contract_comment(poster, monkeypatch,
                                                    tmp_path):
    _pr_env(monkeypatch, tmp_path, R.MARKER + "\nnew report\n")
    legacy = "<!-- hotato-contract-verify -->\nold block"
    gh = FakeGitHub([{"id": 7, "body": legacy,
                      "user": {"login": "github-actions[bot]"}}])
    monkeypatch.setattr(poster, "_request", gh.request)
    assert poster.main() == 0
    assert gh.writes() == [("PATCH",
                            "https://api.example/repos/octo/voice/issues/"
                            "comments/7",
                            {"body": R.MARKER + "\nnew report\n"})]


def test_poster_only_ever_matches_its_own_comment(poster):
    comments = [
        {"id": 1, "body": "quoting " + R.MARKER, "user": {"login": "someone"}},
        {"id": 2, "body": R.MARKER + "\nx",
         "user": {"login": "github-actions[bot]"}},
        {"id": 3, "body": R.MARKER + "\ny", "user": {"login": "pat-owner"}},
    ]
    assert poster.find_existing(comments) == 2
    assert poster.find_existing(comments, author="pat-owner") == 3
    # someone else's comment quoting the marker is never the one to edit
    assert poster.find_existing(comments[:1]) is None
    assert poster.find_existing(comments, author="") is None
    assert poster.find_existing([{"id": 4, "body": R.MARKER}]) is None


def test_poster_with_a_personal_token_never_edits_another_comment(
        poster, monkeypatch, tmp_path):
    # A personal access token: /user names it. A human and another bot both
    # quote the marker; the poster must leave both alone and create its own.
    _pr_env(monkeypatch, tmp_path, R.MARKER + "\nnew\n")
    human = {"id": 1, "body": "> " + R.MARKER + "\nwhy is this red?",
             "user": {"login": "reviewer"}}
    other_bot = {"id": 2, "body": R.MARKER + "\nold",
                 "user": {"login": "github-actions[bot]"}}
    gh = FakeGitHub([human, other_bot], login="pat-owner")
    monkeypatch.setattr(poster, "_request", gh.request)
    assert poster.main() == 0
    assert [c[0] for c in gh.writes()] == ["POST"]
    assert human["body"].endswith("why is this red?")
    assert other_bot["body"] == R.MARKER + "\nold"
    # the next run finds the comment the token itself wrote and PATCHes it
    (tmp_path / "pr-report.md").write_text(R.MARKER + "\nnewer\n",
                                           encoding="utf-8")
    assert poster.main() == 0
    assert [c[0] for c in gh.writes()] == ["POST", "PATCH"]
    assert gh.writes()[-1][1].endswith(f"/issues/comments/{gh.next_id}")


def test_poster_user_lookup_error_other_than_auth_skips(poster, monkeypatch):
    def boom(url, token, *, method="GET", payload=None):
        raise urllib.error.HTTPError(url, 500, "down", {}, io.BytesIO(b""))
    monkeypatch.setattr(poster, "_request", boom)
    status = poster.post_or_update("https://api.example", "octo/voice", 5,
                                   "tok", "body")
    assert status.startswith("skipped")


def test_poster_walks_every_page_and_never_duplicates_past_the_cap(
        poster, monkeypatch, tmp_path):
    # the old 10-page cap: the bot's comment on page 12 must still be found
    _pr_env(monkeypatch, tmp_path, R.MARKER + "\nbody\n")
    filler = [{"id": i, "body": "chatter", "user": {"login": "u"}}
              for i in range(1, 1101)]
    filler.append({"id": 5000, "body": R.MARKER + "\nold",
                   "user": {"login": "github-actions[bot]"}})
    gh = FakeGitHub(filler)
    monkeypatch.setattr(poster, "_request", gh.request)
    assert poster.main() == 0
    assert [c[0] for c in gh.writes()] == ["PATCH"]
    assert gh.writes()[0][1].endswith("/issues/comments/5000")
    # past the page cap with no own comment found: skip, never a duplicate
    monkeypatch.setattr(poster, "_MAX_PAGES", 2)
    gh2 = FakeGitHub(filler[:-1])
    monkeypatch.setattr(poster, "_request", gh2.request)
    status = poster.post_or_update("https://api.example", "octo/voice", 5,
                                   "tok", "body")
    assert status.startswith("skipped") and gh2.writes() == []


def test_poster_paginates(poster, monkeypatch, tmp_path):
    _pr_env(monkeypatch, tmp_path, R.MARKER + "\nbody\n")
    filler = [{"id": i, "body": "chatter", "user": {"login": "u"}}
              for i in range(1, 151)]
    filler.append({"id": 999, "body": R.MARKER + "\nold",
                   "user": {"login": "github-actions[bot]"}})
    gh = FakeGitHub(filler)
    monkeypatch.setattr(poster, "_request", gh.request)
    assert poster.main() == 0
    assert [c[0] for c in gh.calls] == ["GET", "GET", "GET", "PATCH"]
    assert gh.calls[0][1] == "https://api.example/user"
    assert gh.calls[-1][1].endswith("/issues/comments/999")


def test_fork_pull_request_falls_back_to_job_summary(poster, monkeypatch,
                                                     tmp_path, capsys):
    summary = _pr_env(monkeypatch, tmp_path, R.MARKER + "\nreport\n", event={
        "pull_request": {"number": 5,
                         "head": {"repo": {"full_name": "fork/voice",
                                           "fork": True}},
                         "base": {"repo": {"full_name": "octo/voice"}}}})
    gh = FakeGitHub()
    monkeypatch.setattr(poster, "_request", gh.request)
    assert poster.main() == 0
    assert gh.calls == []  # no API call at all with a read-only token
    text = summary.read_text(encoding="utf-8")
    assert text.startswith(R.MARKER)
    assert "_Not posted to the pull request: a pull request from a fork" \
        in text
    assert "fork" in capsys.readouterr().err


def test_deleted_fork_is_a_fork(poster, tmp_path):
    ev = tmp_path / "e.json"
    ev.write_text(json.dumps({"pull_request": {
        "number": 1, "head": {"repo": None},
        "base": {"repo": {"full_name": "octo/voice"}}}}), encoding="utf-8")
    assert poster.is_fork(str(ev)) is True
    ev.write_text(json.dumps({"pull_request": {"number": 1}}),
                  encoding="utf-8")
    assert poster.is_fork(str(ev)) is False


@pytest.mark.parametrize("status", [403, 404])
def test_no_write_permission_falls_back_to_job_summary(poster, monkeypatch,
                                                       tmp_path, status):
    summary = _pr_env(monkeypatch, tmp_path, R.MARKER + "\nreport\n")
    gh = FakeGitHub(fail=status)
    monkeypatch.setattr(poster, "_request", gh.request)
    assert poster.main() == 0
    text = summary.read_text(encoding="utf-8")
    assert R.MARKER in text
    assert (f"_Not posted to the pull request: skipped: the token cannot"
            f" write to this pull request (HTTP {status})") in text


def test_no_token_still_mirrors_the_summary(poster, monkeypatch, tmp_path):
    summary = _pr_env(monkeypatch, tmp_path, R.MARKER + "\nreport\n")
    monkeypatch.delenv("HOTATO_PR_COMMENT_TOKEN")
    gh = FakeGitHub()
    monkeypatch.setattr(poster, "_request", gh.request)
    assert poster.main() == 0
    assert gh.calls == []
    assert "no token provided" in summary.read_text(encoding="utf-8")


def test_oversized_body_is_clipped_under_the_limit(poster):
    body = R.MARKER + "\n" + ("row\n" * 30000)
    clipped = poster._clip_body(body)
    assert len(clipped) < 65536 and clipped.startswith(R.MARKER)
    assert "Truncated" in clipped


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------

def _run_gate(tmp_path, inputs, extra_env=None):
    ws = tmp_path / "workspace"
    shutil.copytree(os.path.join(ROOT, "tests", "fixtures",
                                 "action-consumer"), ws / "qa")
    gh_summary = tmp_path / "step-summary.md"
    gh_output = tmp_path / "outputs.txt"
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("HOTATO_ACTION_", "GITHUB_"))}
    env["PYTHONPATH"] = os.path.join(ROOT, "src") + os.pathsep + env.get(
        "PYTHONPATH", "")
    env["HOTATO_HOME"] = str(tmp_path / "hotato-home")
    env["GITHUB_WORKSPACE"] = str(ws)
    env["GITHUB_STEP_SUMMARY"] = str(gh_summary)
    env["GITHUB_OUTPUT"] = str(gh_output)
    env["HOTATO_ACTION_VERSION"] = "preinstalled"
    env["HOTATO_ACTION_RENDER_RECORDS"] = "true"
    env.update(extra_env or {})
    for key, value in inputs.items():
        env["HOTATO_ACTION_" + key.upper().replace("-", "_")] = value
    proc = subprocess.run([sys.executable, os.path.join(ACTION_DIR,
                                                        "gate.py")],
                          env=env, capture_output=True, text=True,
                          cwd=str(ws), timeout=300)
    outputs = {}
    for line in gh_output.read_text().splitlines():
        key, _, value = line.partition("=")
        outputs[key] = value
    return proc, outputs, ws


def test_gate_renders_the_report_with_baseline_and_keeps_exit(tmp_path):
    proc, outputs, ws = _run_gate(tmp_path, {
        "suite": "qa/suite/consumer.suite.json", "agent": "consumer-agent",
        "baseline": "qa/recorded/suite-run.pass.json",
    }, {"GITHUB_REPOSITORY": "octo/voice", "GITHUB_RUN_ID": "77",
        "GITHUB_BASE_REF": "main"})
    assert proc.returncode == 1, proc.stderr  # the report never moves it
    assert outputs["exit-code"] == "1"
    leaf = outputs["pr-report"]
    assert leaf == ".hotato/results/pr-report.md"
    body = (ws / leaf).read_text(encoding="utf-8")
    assert body.startswith(R.MARKER)
    assert "## hotato: FAIL" in body
    assert "### Compared with the base branch (`main`)" in body
    assert "| newly failing | `escalate-not-handed-off-test` |" in body
    assert "https://github.com/octo/voice/actions/runs/77" in body
    assert "Failure Record `records/sha256-" in body
    assert str(ws) not in body


def test_gate_baseline_absent_is_a_note_and_escape_is_refused(tmp_path):
    proc, outputs, ws = _run_gate(tmp_path, {
        "suite": "qa/suite/pass.suite.json", "agent": "consumer-agent",
        "baseline": "base/suite-run.json",
    })
    assert proc.returncode == 0, proc.stderr
    body = (ws / outputs["pr-report"]).read_text(encoding="utf-8")
    assert "## hotato: PASS" in body and "No base result is available" in body

    proc, outputs, _ = _run_gate(tmp_path / "esc", {
        "suite": "qa/suite/pass.suite.json", "agent": "consumer-agent",
        "baseline": "../outside.json",
    })
    assert proc.returncode == 2
    assert "baseline" in proc.stdout


def test_gate_without_pr_comment_support_renders_no_report(monkeypatch):
    gate = _load_module("gate_t", "gate.py")
    monkeypatch.setattr(gate, "_pr_report_supported", lambda: False)
    leaf, warning = gate.render_pr_report(
        {"output": ".hotato/results", "workspace": "/nonexistent"},
        ".hotato/results/suite-run.json", {}, "")
    assert (leaf, warning) == ("", "")


def test_gate_report_render_survives_undecodable_output(monkeypatch):
    gate = _load_module("gate_t3", "gate.py")
    monkeypatch.setattr(gate, "_pr_report_supported", lambda: True)
    seen = {}

    def bad_decode(argv, **kwargs):
        seen.update(kwargs)
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    monkeypatch.setattr(gate, "_run", bad_decode)
    leaf, warning = gate.render_pr_report(
        {"output": ".hotato/results", "workspace": "/nonexistent"},
        ".hotato/results/suite-run.json", {}, "")
    assert leaf == "" and "gate result is unaffected" in warning
    assert seen.get("errors") == "replace"


def test_gate_report_argv_is_a_list_with_validated_run_url(monkeypatch):
    gate = _load_module("gate_t2", "gate.py")
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo/voice")
    monkeypatch.setenv("GITHUB_RUN_ID", "12")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    monkeypatch.delenv("GITHUB_SERVER_URL", raising=False)
    cfg = {"output": ".hotato/results", "baseline": "base/r.json"}
    argv = gate.pr_report_argv(cfg, ".hotato/results/r.json",
                               {"index_path": "x/index.json"}, "hotato x")
    assert argv[:3] == ["pr", "comment", ".hotato/results/r.json"]
    assert argv[argv.index("--run-url") + 1] == (
        "https://github.com/octo/voice/actions/runs/12")
    assert argv[argv.index("--base") + 1] == "base/r.json"
    assert argv[argv.index("--base-label") + 1] == "main"
    assert argv[argv.index("--records-index") + 1] == "x/index.json"
    # a malformed run id or repository drops the link instead of guessing
    monkeypatch.setenv("GITHUB_RUN_ID", "12; rm -rf /")
    assert "--run-url" not in gate.pr_report_argv(cfg, "r.json", {}, "")


def test_action_yaml_wires_baseline_report_and_summary():
    yaml = pytest.importorskip("yaml")
    with open(os.path.join(ROOT, "action.yml"), "r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    assert doc["inputs"]["baseline"]["default"] == ""
    assert "pr-report" in doc["outputs"]
    gate_step = next(s for s in doc["runs"]["steps"] if s.get("id") == "gate")
    assert gate_step["env"]["HOTATO_ACTION_BASELINE"] == "${{ inputs.baseline }}"
    step = next(s for s in doc["runs"]["steps"] if s.get("id") == "comment")
    assert "comment-pr == 'true'" in step["if"]
    assert step["env"]["HOTATO_PR_COMMENT_FILE"] == (
        "${{ steps.gate.outputs.pr-report || steps.gate.outputs.pr-comment }}")
    assert step["env"]["HOTATO_PR_COMMENT_SUMMARY"] == "true"


def test_renderer_imports_no_network_module():
    with open(os.path.join(ROOT, "src", "hotato", "pr_report.py"), "r",
              encoding="utf-8") as fh:
        src = fh.read()
    for banned in ("urllib", "http.client", "socket", "requests",
                   "subprocess"):
        assert f"import {banned}" not in src and f"from {banned}" not in src


def _regen():
    os.makedirs(GOLDEN, exist_ok=True)
    for name, body in _golden_cases():
        with open(os.path.join(GOLDEN, name), "w", encoding="utf-8",
                  newline="") as fh:
            fh.write(body)
        print("wrote", os.path.join("tests", "golden", "pr_report", name))


if __name__ == "__main__":
    if "--regen" in sys.argv:
        _regen()
