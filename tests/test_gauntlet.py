"""``hotato gauntlet``: the bundled turn-taking stress suite and its N/M badge.

Pins the properties that make the gauntlet a shareable regression guard rather
than a demo:

* the manifest and its cases are a valid, non-empty, unique-id set; every case
  names a bundled reference recording that ships in the wheel (package-data);
* the whole suite runs deterministically over the packaged fixtures -- the same
  result envelope, byte for byte, across two runs (the reproducibility wedge),
  and the derived robustness clips are byte-identical too;
* a case clears when the deterministic scorer's yield/hold verdict agrees with
  the case's ground-truth label; the bundled suite clears every case;
* the CLI lists the cases (``--list``), runs the suite (``--format json`` /
  text), and gates (exit 1) when a case does not clear;
* ``hotato gauntlet badge`` renders a deterministic, self-contained SVG whose
  ``N`` is READ from a real gauntlet result (never invented) and refuses a
  non-gauntlet input.
"""

import json
import xml.dom.minidom as minidom
from importlib import resources

import pytest

from hotato import cli
from hotato import gauntlet as G


def _suite_dir():
    return resources.files("hotato").joinpath(*G.SUITE_DIR)


# --------------------------------------------------------------------------
# manifest + package-data: the suite ships in the wheel and is well-formed
# --------------------------------------------------------------------------

def test_manifest_loads_and_is_a_unique_non_empty_case_set():
    cases = G.list_cases()
    assert len(cases) >= 8
    ids = [c["id"] for c in cases]
    assert ids == sorted(ids)                 # list_cases is byte-stable
    assert len(set(ids)) == len(ids)          # unique
    for c in cases:
        assert c["expect"] in ("yield", "hold")
        assert isinstance(c["onset_sec"], (int, float))


def test_manifest_resource_and_every_case_wav_ship_as_package_data():
    # the manifest itself ...
    assert _suite_dir().joinpath(G.MANIFEST_FILENAME).is_file()
    # ... and every case's reference recording (reused from data/audio)
    for c in G.list_cases():
        wav = resources.files("hotato").joinpath(*G.AUDIO_DIR, c["wav"])
        assert wav.is_file(), c["wav"]


def test_manifest_rejects_a_bad_expect(tmp_path, monkeypatch):
    bad = {"suite": "x", "version": 1,
           "cases": [{"id": "c", "wav": "01-hard-interruption.example.wav",
                      "onset_sec": 2.0, "expect": "maybe"}]}
    monkeypatch.setattr(G, "_suite_resource",
                        lambda _f: _FakeResource(json.dumps(bad)))
    with pytest.raises(ValueError):
        G.load_manifest()


class _FakeResource:
    def __init__(self, text):
        self._text = text

    def read_text(self, encoding="utf-8"):
        return self._text


def test_suite_covers_turn_taking_families_and_synth_robustness():
    fams = G.families()
    # the core turn-taking families the scored audio supports ...
    for fam in ("barge-in", "backchannel", "talk-over"):
        assert fam in fams
    # ... plus at least one seeded synthetic robustness variant (synth-derived)
    robustness = [c for c in G.list_cases() if c.get("perturbation")]
    assert robustness
    for c in robustness:
        assert isinstance(c.get("seed"), int)
        assert c["perturbation"]["transform"] in ("noise", "dropout")


# --------------------------------------------------------------------------
# a full deterministic run over the packaged fixtures (byte-stable)
# --------------------------------------------------------------------------

def test_full_run_is_byte_identical_across_two_runs():
    a = G.run_gauntlet()
    b = G.run_gauntlet()
    assert a == b                                    # the wedge
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert a["kind"] == "gauntlet"
    assert a["total"] == len(G.list_cases())
    assert 0 <= a["passed"] <= a["total"]


def test_bundled_suite_clears_every_case():
    result = G.run_gauntlet()
    assert result["all_passed"] is True
    assert result["passed"] == result["total"]
    for c in result["cases"]:
        assert c["passed"] is True, (c["id"], c["did_yield"], c["scorable"])
        assert c["scorable"] is True


def test_case_pass_agrees_with_the_ground_truth_label():
    # PASS means the scorer's did_yield matched the case's yield/hold label.
    for c in G.run_gauntlet()["cases"]:
        expected_yield = c["expect"] == "yield"
        assert c["did_yield"] == expected_yield, c["id"]


def test_out_dir_persists_byte_identical_clips_and_a_result(tmp_path):
    one = tmp_path / "one"
    two = tmp_path / "two"
    r1 = G.run_gauntlet(out_dir=str(one))
    r2 = G.run_gauntlet(out_dir=str(two))
    assert r1 == r2
    # the derived robustness clips are byte-identical (seeded synth) ...
    clips_one = sorted(p.name for p in one.iterdir() if p.suffix == ".wav")
    clips_two = sorted(p.name for p in two.iterdir() if p.suffix == ".wav")
    assert clips_one == clips_two and clips_one            # at least one clip
    for name in clips_one:
        assert (one / name).read_bytes() == (two / name).read_bytes(), name
    # ... and a gauntlet.json result is written
    assert (one / "gauntlet.json").is_file()
    persisted = json.loads((one / "gauntlet.json").read_text(encoding="utf-8"))
    assert persisted["kind"] == "gauntlet"


# --------------------------------------------------------------------------
# the badge: deterministic, self-contained, and READ from a real result
# --------------------------------------------------------------------------

def _assert_self_contained_svg(svg):
    doc = minidom.parseString(svg)               # raises if not well-formed
    assert doc.documentElement.tagName == "svg"
    for banned in ("xlink", "<image", "<script", "@import", "url(", "href",
                   "src="):
        assert banned not in svg, f"badge SVG must not contain {banned!r}"
    assert svg.count("http") == 1                # only the xmlns declaration
    assert 'xmlns="http://www.w3.org/2000/svg"' in svg


def test_badge_is_derived_from_a_real_result_and_self_contained():
    result = G.run_gauntlet()
    svg = G.render_badge(result)
    _assert_self_contained_svg(svg)
    # the N/M shown is exactly the measured count, never a hardcoded number
    assert f"{result['passed']}/{result['total']}" in svg
    assert "hotato gauntlet" in svg
    # the status is in WORDS (accessibility), not color alone
    assert f"{result['passed']} of {result['total']}" in svg
    assert "cleared" in svg


def test_badge_is_deterministic():
    result = G.run_gauntlet()
    assert G.render_badge(result) == G.render_badge(result)


def test_badge_reflects_the_result_it_is_given_never_invented():
    # a crafted partial result renders its OWN count and the partial accent,
    # proving the number is read from the result, not fabricated.
    partial = {"tool": "hotato", "kind": "gauntlet", "suite": "s",
               "total": 10, "passed": 7, "all_passed": False, "cases": []}
    svg = G.render_badge(partial)
    assert "7/10" in svg
    assert G._BADGE_PARTIAL in svg          # ember, not the all-clear green
    assert G._BADGE_OK not in svg
    # a perfect result renders the all-clear green instead
    perfect = {**partial, "passed": 10, "all_passed": True}
    good = G.render_badge(perfect)
    assert "10/10" in good
    assert G._BADGE_OK in good


def test_badge_refuses_a_non_gauntlet_input():
    for bad in ({"kind": "not-gauntlet", "passed": 1, "total": 1},
                {"kind": "gauntlet", "passed": 5, "total": 3},   # passed>total
                {"kind": "gauntlet", "passed": 1, "total": 0},   # empty
                "nope"):
        with pytest.raises(ValueError):
            G.render_badge(bad)


# --------------------------------------------------------------------------
# CLI: --list, run (text + json), gate on failure, badge to file/stdout
# --------------------------------------------------------------------------

def test_cli_gauntlet_list_text(capsys):
    assert cli.main(["gauntlet", "--list"]) == 0
    out = capsys.readouterr().out
    assert G.SUITE_NAME in out
    for cid in G.ids():
        assert cid in out


def test_cli_gauntlet_list_json(capsys):
    assert cli.main(["gauntlet", "--list", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "gauntlet-list"
    assert payload["count"] == len(G.ids())
    assert {c["id"] for c in payload["cases"]} == set(G.ids())
    assert payload["families"] == G.families()


def test_cli_gauntlet_run_json_all_cleared(capsys):
    assert cli.main(["gauntlet", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "gauntlet"
    assert payload["all_passed"] is True
    assert payload["passed"] == payload["total"] == len(G.ids())


def test_cli_gauntlet_run_text(capsys):
    assert cli.main(["gauntlet"]) == 0
    out = capsys.readouterr().out
    assert "case(s) cleared" in out
    assert "[PASS]" in out


def test_cli_gauntlet_out_writes_result_and_clips(tmp_path, capsys):
    out = tmp_path / "g"
    assert cli.main(["gauntlet", "--out", str(out), "--format", "json"]) == 0
    assert (out / "gauntlet.json").is_file()
    assert any(p.suffix == ".wav" for p in out.iterdir())


def test_cli_gauntlet_gates_when_a_case_does_not_clear(monkeypatch):
    # a suite the scorer got wrong is a real regression -> exit 1.
    monkeypatch.setattr(
        G, "run_gauntlet",
        lambda out_dir=None: {"tool": "hotato", "schema_version": "1",
                              "kind": "gauntlet", "suite": "s", "total": 10,
                              "passed": 9, "all_passed": False,
                              "cases": [{"id": "x", "title": "x", "family": "f",
                                         "expect": "yield", "onset_sec": 2.0,
                                         "perturbation": None, "seed": None,
                                         "clip": None, "scorable": True,
                                         "did_yield": False,
                                         "seconds_to_yield": None,
                                         "talk_over_sec": 3.0,
                                         "passed": False}]})
    assert cli.main(["gauntlet"]) == 1


def test_cli_gauntlet_badge_to_file(tmp_path, capsys):
    out = tmp_path / "gauntlet.svg"
    assert cli.main(["gauntlet", "badge", "--out", str(out)]) == 0
    svg = out.read_text(encoding="utf-8")
    _assert_self_contained_svg(svg)
    assert "hotato gauntlet" in svg


def test_cli_gauntlet_badge_to_stdout(capsys):
    assert cli.main(["gauntlet", "badge"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("<svg")
    _assert_self_contained_svg(out)
