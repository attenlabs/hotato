"""The promote copy-command actions on the analyze/sweep HTML dashboard.

Every candidate card carries three actions: 'Promote as yield fixture' and
'Promote as hold fixture' copy the exact ``hotato fixture promote
REPORT_JSON#RANK --expect yield|hold --id SUGGESTED --out tests/hotato``
command from their ``data-cmd`` attribute; 'Ignore' hides the card on the
page only, client side, no state. Pinned here at the DOM level (attributes
parsed with the stdlib HTML parser, never substring-matched):

  * three actions per shown candidate, in order, with the exact copied
    payload in ``data-cmd``;
  * the ref number is the card's own #N rank chip and parses with
    ``fixture.parse_candidate_ref``;
  * the suggested id is call id + kind + rank, kebab-cased, and passes the
    same slug rule ``fixture create --id`` enforces;
  * the report-json name is the producing command's DEFAULT json name
    (``hotato-analyze.json`` / ``hotato-sweep-STACK.json``), never the --out
    path, so the dashboard stays byte-identical whatever it was saved as;
  * the clipboard JS ships both paths (navigator.clipboard + the execCommand
    fallback) and the dismiss JS + [hidden] CSS are wired;
  * one copied command runs verbatim against the json file it names.
"""

import json
import os
import re
import shlex
import shutil
from html.parser import HTMLParser
from importlib import resources

import pytest

from hotato import analyze as analyze_mod
from hotato import cli
from hotato import fixture as fixture_mod


def _bundled_dual_channel_wav():
    d = resources.files("hotato").joinpath("data", "audio")
    for p in sorted(d.iterdir(), key=lambda x: x.name):
        if p.name.endswith(".example.wav"):
            return str(p)
    raise RuntimeError("no bundled .example.wav fixture found")


@pytest.fixture()
def folder(tmp_path):
    """A folder of two copies of a bundled dual-channel recording, so the
    dashboard shows candidates from more than one source file."""
    d = tmp_path / "calls"
    d.mkdir()
    src = _bundled_dual_channel_wav()
    shutil.copy(src, d / "alpha_call.wav")
    shutil.copy(src, d / "beta_call.wav")
    return str(d)


# --- DOM-level extraction ----------------------------------------------------

class _Dashboard(HTMLParser):
    """One record per candidate card: the #N rank chip text and every button
    (class, label, data-cmd, title), read from parsed attributes."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cards = []
        self._in_card = False
        self._in_rank = False
        self._btn = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = (a.get("class") or "").split()
        if tag == "section" and "moment" in cls:
            self._in_card = True
            self.cards.append({"rank": "", "buttons": []})
        elif self._in_card and tag == "span" and "rank" in cls:
            self._in_rank = True
        elif self._in_card and tag == "button":
            self._btn = {"class": cls, "label": "",
                         "data-cmd": a.get("data-cmd"),
                         "title": a.get("title")}
            self.cards[-1]["buttons"].append(self._btn)

    def handle_endtag(self, tag):
        if tag == "section":
            self._in_card = False
        elif tag == "span":
            self._in_rank = False
        elif tag == "button":
            self._btn = None

    def handle_data(self, data):
        if self._in_rank:
            self.cards[-1]["rank"] += data
        if self._btn is not None:
            self._btn["label"] += data


def _parse(html):
    p = _Dashboard()
    p.feed(html)
    return p.cards


# --- three actions per candidate, exact payloads -----------------------------

def test_every_candidate_card_carries_the_three_actions(folder):
    agg, per_file = analyze_mod.analyze_folder(folder)
    assert agg["total_candidates"] > 0
    html = analyze_mod.build_dashboard_html(agg, per_file)
    cards = _parse(html)
    assert len(cards) == min(agg["total_candidates"], analyze_mod.DEFAULT_TOP)
    for card in cards:
        labels = [b["label"] for b in card["buttons"]]
        assert labels == ["Promote as yield fixture",
                          "Promote as hold fixture", "Ignore"]


def test_promote_buttons_copy_the_exact_command(folder):
    agg, per_file = analyze_mod.analyze_folder(folder)
    html = analyze_mod.build_dashboard_html(agg, per_file)
    for rank, card in enumerate(_parse(html), 1):
        assert card["rank"] == f"#{rank}"
        cand = agg["candidates"][rank - 1]
        sid = analyze_mod.suggest_fixture_id(cand["source"], cand["kind"],
                                             rank)
        y, h, ignore = card["buttons"]
        assert y["data-cmd"] == (
            f"hotato fixture promote hotato-analyze.json#{rank} "
            f"--expect yield --id {sid} --out tests/hotato"
        )
        assert h["data-cmd"] == (
            f"hotato fixture promote hotato-analyze.json#{rank} "
            f"--expect hold --id {sid} --out tests/hotato"
        )
        # the title discloses the payload, for a blocked-clipboard reader
        assert y["title"] == "copies: " + y["data-cmd"]
        # Ignore copies nothing and carries no command
        assert ignore["data-cmd"] is None


def test_refs_parse_and_ids_pass_the_fixture_slug_rule(folder):
    agg, per_file = analyze_mod.analyze_folder(folder)
    html = analyze_mod.build_dashboard_html(agg, per_file)
    for rank, card in enumerate(_parse(html), 1):
        for btn in card["buttons"][:2]:
            argv = shlex.split(btn["data-cmd"])
            assert argv[:3] == ["hotato", "fixture", "promote"]
            path, call, number = fixture_mod.parse_candidate_ref(argv[3])
            assert (path, call, number) == ("hotato-analyze.json", None, rank)
            sid = argv[argv.index("--id") + 1]
            assert fixture_mod._SLUG_RE.match(sid), sid
            assert argv[argv.index("--out") + 1] == "tests/hotato"


def test_suggested_id_is_call_id_kind_and_rank_kebab_cased():
    assert analyze_mod.suggest_fixture_id(
        "alpha_call.wav", "overlap_while_agent_talking", 3
    ) == "alpha-call-overlap-while-agent-talking-3"
    # extensions beyond .wav are dropped from the call id
    assert analyze_mod.suggest_fixture_id(
        "fd-01-missed-interruption.example.wav", "long_response_gap", 12
    ) == "fd-01-missed-interruption-long-response-gap-12"
    # a pulled recording contributes its bare call id, like FILE#CALL:N refs
    assert analyze_mod.suggest_fixture_id(
        "vapi__call_Abc123.wav", "agent_start_during_caller", 1
    ) == "call-abc123-agent-start-during-caller-1"


# --- the report-json name is stable, never the --out path --------------------

def test_report_json_is_the_default_name_not_the_out_path(folder, tmp_path):
    out = tmp_path / "renamed-dashboard.html"
    assert cli.main(["analyze", folder, "--no-open", "--out", str(out)]) == 0
    html = out.read_text(encoding="utf-8")
    cmds = [b["data-cmd"] for c in _parse(html) for b in c["buttons"][:2]]
    assert cmds
    for cmd in cmds:
        assert "hotato-analyze.json#" in cmd
        assert "renamed-dashboard" not in cmd


def test_sweep_demo_dashboard_names_its_own_default_json(tmp_path, monkeypatch):
    monkeypatch.setenv("HOTATO_HOME", str(tmp_path / "home"))
    out = tmp_path / "dash.html"
    assert cli.main(["sweep", "--demo", "--no-open", "--out", str(out)]) == 0
    cards = _parse(out.read_text(encoding="utf-8"))
    assert cards
    for card in cards:
        for btn in card["buttons"][:2]:
            assert btn["data-cmd"].startswith(
                "hotato fixture promote hotato-sweep-demo.json#")


# --- the client-side wiring ---------------------------------------------------

def test_clipboard_and_dismiss_js_are_wired(folder):
    agg, per_file = analyze_mod.analyze_folder(folder)
    html = analyze_mod.build_dashboard_html(agg, per_file)
    # both clipboard paths ship: the async API and the execCommand fallback
    assert "navigator.clipboard" in html
    assert "document.execCommand('copy')" in html
    # dismiss is visual only: the hidden attribute plus its CSS, no storage
    assert "setAttribute('hidden','')" in html
    assert ".moment[hidden]{display:none}" in html
    assert "localStorage" not in html and "sessionStorage" not in html
    # the page-level caption names the json the buttons read
    assert "hotato-analyze.json" in html


# --- one copied command runs verbatim -----------------------------------------

def test_copied_command_runs_against_the_json_it_names(tmp_path, capsys,
                                                       monkeypatch):
    monkeypatch.setenv("HOTATO_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    # the json result the buttons name, written exactly as the caption says
    assert cli.main(["sweep", "--demo", "--format", "json"]) == 0
    doc_text = capsys.readouterr().out
    (tmp_path / "hotato-sweep-demo.json").write_text(doc_text,
                                                     encoding="utf-8")
    doc = json.loads(doc_text)
    # the dashboard whose button we copy
    assert cli.main(["sweep", "--demo", "--no-open", "--out", "dash.html"]) == 0
    cards = _parse((tmp_path / "dash.html").read_text(encoding="utf-8"))
    # promote the first overlap candidate (the agent is talking at the onset,
    # so a yield label scores)
    rank = next(i for i, c in enumerate(doc["candidates"], 1)
                if c["kind"] == "overlap_while_agent_talking")
    cmd = cards[rank - 1]["buttons"][0]["data-cmd"]
    argv = shlex.split(cmd)
    assert argv[0] == "hotato"
    assert cli.main(argv[1:]) == 0
    sid = argv[argv.index("--id") + 1]
    assert os.path.isfile(
        os.path.join("tests", "hotato", "scenarios", sid + ".json"))
    assert os.path.isfile(
        os.path.join("tests", "hotato", "audio", sid + ".example.wav"))
