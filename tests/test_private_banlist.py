"""The private name ban list (``scripts/private_banlist.py``) and its consumers.

Real internal names live only in the maintainers' private file, so every test
here builds a temp ban list holding the digests of synthetic canary names.
"""

import hashlib
import importlib.util
import os

import pytest

from tests import _banned

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_CANARY = "zzcanaryleak"
_PHRASE = "zzcanary three word"
_PUBLIC = "zzcanarypublic"


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, *rel.split("/")))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def banlist_mod():
    return _banned._BANLIST


@pytest.fixture
def canary_banlist(tmp_path, monkeypatch):
    path = tmp_path / "private-banlist.sha256"
    path.write_text(
        f"{_sha(_CANARY)}  # canary\n"
        f"{_sha(_PHRASE)} internal\n"
        f"{_sha(_PUBLIC)} public  # public canary\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOTATO_PRIVATE_BANLIST", str(path))
    monkeypatch.delenv("HOTATO_REQUIRE_PRIVATE_BANLIST", raising=False)
    return path


@pytest.fixture
def absent_banlist(tmp_path, monkeypatch):
    monkeypatch.setenv("HOTATO_PRIVATE_BANLIST", str(tmp_path / "absent"))
    monkeypatch.delenv("HOTATO_REQUIRE_PRIVATE_BANLIST", raising=False)


def test_parse_scopes_labels_and_comments(banlist_mod):
    entries = banlist_mod.parse(
        "# header\n\n"
        f"{_sha('a')}\n"
        f"{_sha('b').upper()} public  # shown label\n")
    assert entries[_sha("a")] == ("internal", "private ban list line 3")
    assert entries[_sha("b")] == ("public", "shown label")


@pytest.mark.parametrize("line", ["xyz", f"{_sha('a')} secret", f"{_sha('a')} public extra"])
def test_parse_refuses_a_malformed_line(banlist_mod, line):
    with pytest.raises(banlist_mod.BanlistUnavailable):
        banlist_mod.parse(line + "\n")


def test_matches_tokens_and_phrases_across_separators(banlist_mod, canary_banlist):
    names = banlist_mod.digests("all")
    assert banlist_mod.first_hit(f"see {_CANARY.upper()}.", names) == "canary"
    assert banlist_mod.first_hit("path/zzcanary-three_word/x", names) is not None
    assert banlist_mod.first_hit(f"{_PUBLIC} ltd", names) == "public canary"
    assert banlist_mod.first_hit("zzcanary three words", names) is None
    assert banlist_mod.first_hit(f"x{_CANARY}", names) is None
    internal = banlist_mod.digests("internal")
    assert banlist_mod.first_hit(_PUBLIC, internal) is None


def test_banned_leaks_reads_the_private_list(canary_banlist):
    assert _banned.leaks(f"a verdict naming {_CANARY}")
    assert _banned.leaks(f"made by {_PUBLIC}")
    assert not _banned.leaks(f"made by {_PUBLIC}", internal_only=True)
    assert not _banned.leaks("a neutral verdict")


def test_banned_leaks_skips_without_the_file(absent_banlist):
    assert not _banned.leaks(f"a verdict naming {_CANARY}")


def test_banned_leaks_fails_when_required_and_absent(absent_banlist, monkeypatch):
    monkeypatch.setenv("HOTATO_REQUIRE_PRIVATE_BANLIST", "1")
    with pytest.raises(pytest.fail.Exception, match="REQUIRE_PRIVATE_BANLIST"):
        _banned.leaks("anything")


def test_atlas_neutrality_lint_refuses_ban_listed_names(canary_banlist):
    atlas = _load("build_atlas", "scripts/build_atlas.py")
    for text in (f"a {_CANARY} fix", f"by {_PUBLIC}", "Zzcanary-three-word layer"):
        with pytest.raises(atlas.AtlasBuildError, match="neutrality violation"):
            atlas._lint_neutrality("record:title", text)
    atlas._lint_neutrality("record:title", "a neutral title")


def test_atlas_neutrality_lint_skips_or_fails_without_the_file(absent_banlist, monkeypatch):
    atlas = _load("build_atlas", "scripts/build_atlas.py")
    atlas._lint_neutrality("record:title", f"a {_CANARY} fix")
    monkeypatch.setenv("HOTATO_REQUIRE_PRIVATE_BANLIST", "1")
    with pytest.raises(atlas.AtlasBuildError, match="REQUIRE_PRIVATE_BANLIST"):
        atlas._lint_neutrality("record:title", "a neutral title")
