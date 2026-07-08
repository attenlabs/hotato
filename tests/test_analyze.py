"""``hotato analyze <folder>``: zero-config drop-a-folder discovery.

Pinned here, over a temp folder of the bundled real corpus clips
(``corpus/vapi-defaults/audio``):

  * analyze aggregates + ranks candidate moments across the whole folder and
    writes a dashboard + JSON that are byte-identical across two runs;
  * the HTML carries the hear-the-bug player: an inline base64 ``<audio>`` and
    the requestAnimationFrame playhead-sync JS, driven by ``audio.currentTime``,
    reduced-motion safe;
  * ranking is by the scanner's own salience (worst first);
  * a mono / unreadable file is reported skipped-with-reason, never a crash;
  * ``--format json`` has a stable shape an agent can drive;
  * the honest-copy contract holds: no "failure(s)", no "verdict", and no
    accuracy percentage anywhere on the page;
  * a bare ``hotato <folder>`` routes to analyze;
  * a bad path exits 2, a good folder exits 0.
"""

import json
import os
import re
import shutil
import struct
import wave

import pytest

from hotato import analyze as analyze_mod
from hotato import cli

CORPUS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "corpus", "vapi-defaults", "audio",
)


@pytest.fixture(scope="module")
def corpus_folder(tmp_path_factory):
    """A temp folder of the bundled real corpus clips (copied so the test owns
    the directory and never writes into the packaged corpus)."""
    dst = tmp_path_factory.mktemp("analyze-corpus")
    for name in sorted(os.listdir(CORPUS)):
        if name.lower().endswith(".wav"):
            shutil.copy(os.path.join(CORPUS, name), dst / name)
    return str(dst)


# --- aggregate + ranking ----------------------------------------------------

def test_analyze_folder_aggregates_and_ranks_by_salience(corpus_folder):
    agg, per_file = analyze_mod.analyze_folder(corpus_folder)
    assert agg["kind"] == "analyze"
    assert agg["calls_scanned"] == 16
    assert agg["calls_skipped"] == 0
    assert agg["total_candidates"] > 0
    # ranked worst-first: salience is non-increasing across the list
    sal = [c["salience"] for c in agg["candidates"]]
    assert sal == sorted(sal, reverse=True)
    # every candidate names a real scanned file and carries its window
    scanned = {s["source"] for s in agg["scanned"]}
    for c in agg["candidates"]:
        assert c["source"] in scanned
        assert c["window"]["end_sec"] > c["window"]["start_sec"]
        assert c["source"] in per_file


def test_analyze_folder_ranking_is_stable_across_calls_and_kinds(corpus_folder):
    agg, _ = analyze_mod.analyze_folder(corpus_folder)
    # the sort key is (-salience, source, t_sec, kind): equal-salience ties
    # break deterministically, so two aggregations agree exactly.
    agg2, _ = analyze_mod.analyze_folder(corpus_folder)
    assert agg["candidates"] == agg2["candidates"]


# --- determinism: two runs byte-identical -----------------------------------

def test_dashboard_and_json_are_byte_identical_across_two_runs(corpus_folder, tmp_path):
    h1, h2 = tmp_path / "a1.html", tmp_path / "a2.html"
    assert cli.main(["analyze", corpus_folder, "--no-open", "--out", str(h1)]) == 0
    assert cli.main(["analyze", corpus_folder, "--no-open", "--out", str(h2)]) == 0
    assert h1.read_bytes() == h2.read_bytes()

    j1, j2 = tmp_path / "a1.json", tmp_path / "a2.json"
    cli.main(["analyze", corpus_folder, "--format", "json", "--out", str(j1)])
    cli.main(["analyze", corpus_folder, "--format", "json", "--out", str(j2)])
    assert j1.read_bytes() == j2.read_bytes()


# --- the hear-the-bug player -----------------------------------------------

def test_html_has_embedded_audio_and_the_playhead_sync_js(corpus_folder, tmp_path):
    out = tmp_path / "dash.html"
    cli.main(["analyze", corpus_folder, "--no-open", "--out", str(out)])
    html = out.read_text(encoding="utf-8")
    # real audio embedded, offline, as a base64 data URI (no upload, no fetch)
    assert "<audio" in html
    assert "data:audio/wav;base64," in html
    # the playhead + the requestAnimationFrame sync driven by audio.currentTime
    assert 'class="ph"' in html
    assert "requestAnimationFrame" in html
    assert "currentTime" in html
    # reduced-motion safe: the loop is gated and timeupdate still drives it
    assert "prefers-reduced-motion" in html
    assert "timeupdate" in html
    # the timeline SVG renderer from report.py is reused
    assert "tl-svg" in html
    # exactly one embedded player per audio-top moment (default 8)
    assert html.count("<audio") == 8
    # one playhead per shown moment (default top 25, but corpus has fewer here)
    assert html.count('class="ph"') == html.count('class="card moment"')


def test_audio_top_caps_the_number_of_players(corpus_folder, tmp_path):
    out = tmp_path / "dash.html"
    cli.main(["analyze", corpus_folder, "--no-open", "--audio-top", "3",
              "--out", str(out)])
    html = out.read_text(encoding="utf-8")
    assert html.count("<audio") == 3


# --- honest copy: no failures / verdict / accuracy percentage --------------

def test_no_failure_verdict_or_accuracy_percentage_anywhere(corpus_folder, tmp_path):
    out = tmp_path / "dash.html"
    cli.main(["analyze", corpus_folder, "--no-open", "--out", str(out)])
    html = out.read_text(encoding="utf-8")
    # The page carries megabytes of base64 audio whose alphabet ([A-Za-z0-9+/=])
    # can incidentally spell letter-only words; lint the COPY, not the payload,
    # by blanking the data URIs first. ('%' is not a base64 char, so the
    # percent-sign check needs no stripping and stays exact.)
    copy = re.sub(r"data:audio/wav;base64,[A-Za-z0-9+/=]+",
                  "data:audio/wav;base64,", html)
    low = copy.lower()
    assert "failure" not in low
    assert "failed" not in low
    assert "verdict" not in low
    # the strongest form of "no accuracy score": no percent sign at all
    assert "%" not in html
    assert re.search(r"\d\s*%", html) is None
    # framed as measured candidate moments you label, never a decided outcome
    assert "candidate moment" in low
    assert "fixture create" in html


# --- skipped, never a crash -------------------------------------------------

def test_mono_and_unreadable_files_are_skipped_with_reason(tmp_path):
    folder = tmp_path / "mixed"
    folder.mkdir()
    # one good dual-channel clip from the corpus
    good = next(n for n in sorted(os.listdir(CORPUS)) if n.endswith(".wav"))
    shutil.copy(os.path.join(CORPUS, good), folder / "good.wav")
    # a mono WAV: cannot attribute talk-over -> skipped with reason
    with wave.open(str(folder / "mono.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<" + "h" * 1600, *([0] * 1600)))
    # a non-wav file is ignored entirely
    (folder / "notes.txt").write_text("not audio")

    agg, _ = analyze_mod.analyze_folder(str(folder))
    assert agg["calls_scanned"] == 1
    assert agg["calls_skipped"] == 1
    assert agg["skipped"][0]["file"] == "mono.wav"
    assert agg["skipped"][0]["reason"]  # a non-empty honest reason
    # the dashboard renders the skip cleanly, still exit 0
    out = tmp_path / "d.html"
    assert cli.main(["analyze", str(folder), "--no-open", "--out", str(out)]) == 0
    assert "Skipped files" in out.read_text(encoding="utf-8")


def test_empty_folder_runs_clean_with_zero_candidates(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    agg, per_file = analyze_mod.analyze_folder(str(empty))
    assert agg["calls_scanned"] == 0
    assert agg["total_candidates"] == 0
    assert per_file == {}
    out = tmp_path / "e.html"
    assert cli.main(["analyze", str(empty), "--no-open", "--out", str(out)]) == 0
    assert "No candidate moments" in out.read_text(encoding="utf-8")


# --- JSON shape (stable for an agent to drive) ------------------------------

def test_json_format_has_a_stable_shape(corpus_folder, capsys):
    code = cli.main(["analyze", corpus_folder, "--format", "json", "--top", "5"])
    assert code == 0
    out = capsys.readouterr().out
    doc = json.loads(out)
    assert doc["tool"] == "hotato"
    assert doc["kind"] == "analyze"
    assert doc["schema_version"] == "1"
    assert set(doc) >= {
        "folder", "note", "config", "calls_scanned", "calls_skipped",
        "scanned", "skipped", "total_candidates", "candidates", "shown",
    }
    assert doc["shown"] == len(doc["candidates"]) == 5
    c0 = doc["candidates"][0]
    assert set(c0) >= {
        "source", "t_sec", "kind", "salience", "durations", "agent_reaction",
        "window",
    }
    # the note is the honest label contract, never a verdict
    assert "fixture create" in doc["note"]


# --- routing + exit codes ---------------------------------------------------

def test_bare_folder_routes_to_analyze(corpus_folder, tmp_path):
    out = tmp_path / "bare.html"
    # a bare positional that is a directory routes to `analyze`
    assert cli.main([corpus_folder, "--no-open", "--out", str(out)]) == 0
    assert out.exists()
    assert "hotato analyze" in out.read_text(encoding="utf-8")


def test_bad_path_exits_2(tmp_path):
    missing = str(tmp_path / "does-not-exist")
    assert cli.main(["analyze", missing]) == 2


def test_not_a_directory_exits_2(corpus_folder):
    a_file = os.path.join(corpus_folder, os.listdir(corpus_folder)[0])
    assert cli.main(["analyze", a_file]) == 2
