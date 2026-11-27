"""The stdlib-only side of `hotato cloud`: runs with or without the extra."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from hotato import cloud_cli


def test_core_never_imports_the_cloud_package_or_its_dependencies():
    code = ("import sys, hotato.cli, hotato.cloud_cli\n"
            "bad = [m for m in sys.modules if m.split('.')[0] in "
            "('fastapi','sqlalchemy','jinja2','uvicorn','argon2') or m.startswith('hotato.cloud.')"
            " or m == 'hotato.cloud']\n"
            "assert not bad, bad\n")
    subprocess.run([sys.executable, "-c", code], check=True)


def _event(i):
    return {"schema": "hotato.monitorevent.v1", "call_id": f"c{i}", "stack": "vapi"}


def test_push_classifies_and_batches(tmp_path):
    (tmp_path / "run.json").write_text(json.dumps({"schema": "hotato.testrun.v1"}))
    (tmp_path / "events.jsonl").write_text("\n".join(json.dumps(_event(i)) for i in range(450)))
    (tmp_path / "list.json").write_text(json.dumps([_event(900), _event(901)]))
    (tmp_path / "other.json").write_text(json.dumps({"schema": "something.else"}))
    sent, lines = [], []

    def poster(url, token, body):
        sent.append((url, token, body))
        return 201, "{}"

    rc = cloud_cli.push(str(tmp_path), "https://h.example.com/", "pid1", "tok",
                        out=lines.append, poster=poster)
    assert rc == 0
    urls = [u for u, _, _ in sent]
    assert urls[0].endswith("/api/v1/projects/pid1/testruns") or "/testruns" in "".join(urls)
    assert sum(len(b) for u, _, b in sent if u.endswith("/events")) == 452
    assert all(t == "tok" for _, t, _ in sent)
    assert any("skipped" in ln and "other.json" in ln for ln in lines)
    assert not any("tok" == ln for ln in lines)


def test_push_exit_codes(tmp_path):
    f = tmp_path / "e.json"
    f.write_text(json.dumps(_event(1)))
    out = []
    assert cloud_cli.push(str(f), "https://h", "p", "t", out=out.append,
                          poster=lambda *a: (422, '{"detail":"bad"}')) == 1
    assert cloud_cli.push(str(f), "https://h", "p", "t", out=out.append,
                          poster=lambda *a: (401, "")) == 2
    assert cloud_cli.push(str(tmp_path / "missing"), "https://h", "p", "t", out=out.append) == 2
    empty = tmp_path / "empty"
    empty.mkdir()
    assert cloud_cli.push(str(empty), "https://h", "p", "t", out=out.append) == 2
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    assert cloud_cli.push(str(bad), "https://h", "p", "t", out=out.append) == 2


def test_push_refuses_token_over_plain_http_to_remote(tmp_path):
    f = tmp_path / "e.json"
    f.write_text(json.dumps(_event(1)))
    out = []
    calls = []

    def poster(*a):
        calls.append(a)
        return 201, ""

    assert cloud_cli.push(str(f), "http://example.com", "p", "t", out=out.append, poster=poster) == 2
    assert not calls
    assert cloud_cli.push(str(f), "http://127.0.0.1:8080", "p", "t", out=out.append, poster=poster) == 0
    assert cloud_cli.push(str(f), "http://example.com", "p", "t", allow_insecure=True,
                          out=out.append, poster=poster) == 0
    assert cloud_cli.push(str(f), "ftp://x", "p", "t", out=out.append) == 2


def test_push_end_to_end_against_the_app(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("argon2")
    pytest.importorskip("httpx")
    pytest.importorskip("multipart")
    pytest.importorskip("sqlalchemy")
    from tests.test_cloud_app import Browser, make_app, make_event, make_testrun, make_token

    app = make_app()
    b = Browser(app)
    pid = b.signup("e2e@example.com")
    tok = make_token(b, pid)
    (tmp_path / "run.json").write_text(json.dumps(make_testrun("e2e-run")))
    (tmp_path / "ev.jsonl").write_text("\n".join(json.dumps(make_event(f"e{i}")) for i in range(3)))

    def poster(url, token, body):
        path = url.split("://", 1)[1].split("/", 1)[1]
        r = b.c.post("/" + path, json=body, headers={"Authorization": f"Bearer {token}"})
        return r.status_code, r.text

    out = []
    assert cloud_cli.push(str(tmp_path), "https://h.example.com", pid, tok,
                          out=out.append, poster=poster) == 0, out
    assert "e2e-run" in b.page(f"/p/{pid}/tests").text
    assert "agent-a" in b.page(f"/p/{pid}/calls").text


def test_cloud_commands_are_registered_with_exit_codes(capsys):
    from hotato import cli
    for name in ("cloud", "cloud serve", "cloud push", "cloud admin", "cloud admin create-user"):
        assert name in cli._EXIT_CODES
    with pytest.raises(SystemExit):
        cli.main(["cloud", "push", "--help"])
    assert "Exit codes:" in capsys.readouterr().out
    assert cli.main(["cloud", "push", "x"]) == 2
