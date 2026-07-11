"""`hotato connect`: credentials stored 0600, never emitted, auth-check gating,
and the flag > connection > env resolution order. Fully offline (the auth check
mocks urllib)."""

import io
import json
import os
import stat
import sys
import urllib.error
import urllib.request

import pytest

from hotato import capture as cap
from hotato import cli
from hotato import connections


class _Resp:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install(monkeypatch, routes):
    keys = sorted(routes, key=len, reverse=True)

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        for key in keys:
            if key in url:
                payload = routes[key]
                if isinstance(payload, Exception):
                    raise payload
                return _Resp(payload)
        raise AssertionError(f"unexpected URL fetched offline: {url}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


def _http(code):
    return urllib.error.HTTPError("https://x.test", code, "err", None,
                                  io.BytesIO(b"nope"))


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOTATO_HOME", str(tmp_path / "home"))
    for var in ("VAPI_API_KEY", "RETELL_API_KEY", "TWILIO_ACCOUNT_SID",
                "TWILIO_AUTH_TOKEN", "BLAND_API_KEY"):
        monkeypatch.delenv(var, raising=False)


# --- storage: 0600, never phoned home, never emitted -----------------------

def test_connect_stores_creds_0600_and_never_prints_the_secret(monkeypatch, capsys):
    _install(monkeypatch, {"api.vapi.ai/call": b"[]"})  # auth check -> empty list
    rc = cli.main(["connect", "vapi", "--api-key", "super-secret-key"])
    assert rc == 0
    out = capsys.readouterr()
    # the secret must never appear on stdout or stderr
    assert "super-secret-key" not in out.out
    assert "super-secret-key" not in out.err

    path = connections.connections_path()
    assert os.path.exists(path)
    if sys.platform != "win32":
        # os.chmod's 0o600/0o700 bits are a POSIX permission model; Windows
        # has no equivalent octal mode (chmod there only toggles a read-only
        # attribute), so the exact-mode assertion is meaningless there.
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600, oct(mode)
        dmode = stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode)
        assert dmode == 0o700, oct(dmode)
    # but the file itself does hold the key (for reuse by pull/sweep)
    assert json.loads(open(path).read())["vapi"]["api_key"] == "super-secret-key"


def test_connect_reports_ok_auth_check(monkeypatch, capsys):
    _install(monkeypatch, {"api.vapi.ai/call": b"[]"})
    cli.main(["connect", "vapi", "--api-key", "k"])
    assert "auth check: OK" in capsys.readouterr().out


def test_connect_auth_failure_does_not_store(monkeypatch, capsys):
    _install(monkeypatch, {"api.vapi.ai/call": _http(401)})
    rc = cli.main(["connect", "vapi", "--api-key", "bad"])
    assert rc == 2
    assert not os.path.exists(connections.connections_path())


def test_connect_no_verify_stores_without_network(monkeypatch, capsys):
    # No urlopen installed: --no-verify must not make any call.
    rc = cli.main(["connect", "vapi", "--api-key", "k", "--no-verify"])
    assert rc == 0
    assert connections.get("vapi") == {"api_key": "k"}


def test_connect_retell_has_no_probe_but_still_stores(monkeypatch, capsys):
    # Retell has no list endpoint to verify against: store, note, no network.
    rc = cli.main(["connect", "retell", "--api-key", "k"])
    assert rc == 0
    assert connections.get("retell") == {"api_key": "k"}
    assert "validated on the first pull" in capsys.readouterr().out


def test_connect_twilio_two_fields(monkeypatch):
    _install(monkeypatch, {"Recordings.json": b'{"recordings": []}'})
    rc = cli.main(["connect", "twilio", "--account-sid", "AC1", "--auth-token", "t"])
    assert rc == 0
    assert connections.get("twilio") == {"account_sid": "AC1", "auth_token": "t"}


def test_connect_reads_env_var(monkeypatch):
    monkeypatch.setenv("VAPI_API_KEY", "from-env")
    _install(monkeypatch, {"api.vapi.ai/call": b"[]"})
    cli.main(["connect", "vapi"])
    assert connections.get("vapi") == {"api_key": "from-env"}


def test_connect_missing_creds_exits_2(monkeypatch, capsys):
    rc = cli.main(["connect", "vapi"])  # no key, no env
    assert rc == 2
    assert "missing credentials" in capsys.readouterr().err


def test_connect_json_format_omits_no_secret(monkeypatch, capsys):
    _install(monkeypatch, {"api.vapi.ai/call": b"[]"})
    cli.main(["connect", "vapi", "--api-key", "shhh", "--format", "json"])
    out = capsys.readouterr().out
    assert "shhh" not in out
    payload = json.loads(out)
    assert payload["stored_fields"] == ["api_key"]
    assert payload["verified"] is True


# --- resolution order: flag > connection > env -----------------------------

def test_resolve_creds_prefers_flag_then_connection_then_env(monkeypatch):
    connections.save("vapi", {"api_key": "from-connection"})
    monkeypatch.setenv("VAPI_API_KEY", "from-env")
    assert cap.resolve_creds("vapi", {"api_key": "from-flag"})["api_key"] == "from-flag"
    assert cap.resolve_creds("vapi", {})["api_key"] == "from-connection"


def test_resolve_creds_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("VAPI_API_KEY", "from-env")
    assert cap.resolve_creds("vapi", {})["api_key"] == "from-env"


def test_resolve_stack_infers_single_connection(monkeypatch):
    connections.save("bland", {"api_key": "k"})
    assert cap.resolve_stack(None) == "bland"


def test_resolve_stack_ambiguous_needs_explicit(monkeypatch):
    connections.save("vapi", {"api_key": "k"})
    connections.save("bland", {"api_key": "k"})
    with pytest.raises(ValueError, match="several stacks"):
        cap.resolve_stack(None)


def test_resolve_stack_none_connected_points_at_connect(monkeypatch):
    with pytest.raises(ValueError, match="hotato connect"):
        cap.resolve_stack(None)


def test_pull_after_connect_needs_no_key(monkeypatch, tmp_path):
    connections.save("vapi", {"api_key": "stored"})
    seen = {}

    def fake_fetch(stack, ident, creds, out_path=None, *, allow_mono=False):
        seen["key"] = creds["api_key"]
        open(out_path, "wb").close()
        return out_path

    monkeypatch.setattr(cap, "fetch_one", fake_fetch)
    _install(monkeypatch, {"api.vapi.ai/call": json.dumps([{"id": "v1"}]).encode()})
    rc = cli.main(["pull", "--out", str(tmp_path / "d")])  # no --stack, no --api-key
    assert rc == 0
    assert seen["key"] == "stored"


def test_save_works_without_os_fchmod(tmp_path, monkeypatch):
    """Windows has no os.fchmod. Credential save must not crash there.
    Found by the cross-OS CI matrix (windows-latest), 2026-07-10."""
    import os as _os
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delattr(_os, "fchmod", raising=False)
    from hotato import connections
    monkeypatch.setattr(connections, "connections_path",
                        lambda: str(tmp_path / ".hotato" / "connections.json"))
    monkeypatch.setattr(connections, "_ensure_home",
                        lambda: str((tmp_path / ".hotato").resolve()) if (tmp_path / ".hotato").exists()
                        or not (tmp_path / ".hotato").mkdir(parents=True) else str(tmp_path / ".hotato"))
    connections.save("vapi", {"api_key": "k"})
    assert connections.load_all()["vapi"]["api_key"] == "k"


# --- #16: load path must refuse a group/world-readable connections file ----

@pytest.mark.skipif(sys.platform == "win32",
                     reason="POSIX mode bits have no Windows equivalent")
def test_load_all_refuses_world_readable_file(monkeypatch):
    """The read path must enforce the same 0600 invariant the write path
    creates. Reproduces the original defect: chmod 644 on connections.json
    used to be read back with zero warning."""
    connections.save("vapi", {"api_key": "sk-test-12345"})
    path = connections.connections_path()
    os.chmod(path, 0o644)
    with pytest.raises(ValueError, match=r"group/world-accessible"):
        connections.load_all()


@pytest.mark.skipif(sys.platform == "win32",
                     reason="POSIX mode bits have no Windows equivalent")
def test_load_all_refusal_never_leaks_the_secret_value(monkeypatch):
    connections.save("vapi", {"api_key": "sk-test-12345"})
    os.chmod(connections.connections_path(), 0o644)
    with pytest.raises(ValueError) as excinfo:
        connections.load_all()
    assert "sk-test-12345" not in str(excinfo.value)


@pytest.mark.skipif(sys.platform == "win32",
                     reason="POSIX mode bits have no Windows equivalent")
def test_get_and_connected_stacks_inherit_the_permission_refusal(monkeypatch):
    """get() and connected_stacks() both call load_all() internally, so they
    must refuse too rather than silently returning stale/insecure data."""
    connections.save("vapi", {"api_key": "sk-test-12345"})
    os.chmod(connections.connections_path(), 0o640)
    with pytest.raises(ValueError, match=r"group/world-accessible"):
        connections.get("vapi")
    with pytest.raises(ValueError, match=r"group/world-accessible"):
        connections.connected_stacks()


@pytest.mark.skipif(sys.platform == "win32",
                     reason="POSIX mode bits have no Windows equivalent")
def test_load_all_still_works_for_a_properly_permissioned_file(monkeypatch):
    """No false positive: the normal 0600 file saved by save() must keep
    loading without complaint."""
    connections.save("vapi", {"api_key": "sk-test-12345"})
    assert connections.load_all()["vapi"]["api_key"] == "sk-test-12345"


def test_load_all_skips_the_mode_check_on_non_posix(monkeypatch):
    """The refusal is gated to POSIX, mirroring the existing os.fchmod guard,
    so Windows (no meaningful mode bits) behavior is unchanged."""
    connections.save("vapi", {"api_key": "sk-test-12345"})
    if sys.platform != "win32":
        os.chmod(connections.connections_path(), 0o644)
    monkeypatch.setattr(os, "name", "nt")
    assert connections.load_all()["vapi"]["api_key"] == "sk-test-12345"
