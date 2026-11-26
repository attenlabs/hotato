"""Unattended operation: logging contract, GitHub credentials, promotion, model drift.
All offline: the local Store and injected fakes; no network."""
import datetime as dt
import io
import json
import urllib.error

import pytest

import common
import vertex_models as models
import promotion

UTC = dt.timezone.utc
CFG = {"auto_promote": True, "promote_after_clean_days": 7, "promote_after_clean_days_by_job": {"weekly": 2}}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("DRY_RUN", "GITHUB_BOT_TOKEN", "GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY",
              "GITHUB_APP_INSTALLATION_ID", "CLOUD_RUN_JOB", "K_SERVICE", "BUILDER_MODEL", "AUTOPILOT_VERSION"):
        monkeypatch.delenv(k, raising=False)
    common.reset_github_cache()
    yield
    common.reset_github_cache()


def lines(capsys):
    return [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.startswith("{")]


def alerts(out, kind=None):
    return [o for o in out if "hotato_alert" in o and (kind is None or o["hotato_alert"] == kind)]


def store(tmp_path):
    return common.Store(local_dir=str(tmp_path))


def day(n, hour=12):
    return dt.datetime(2026, 12, 1, hour, tzinfo=UTC) + dt.timedelta(days=n)


# ---- logging contract -------------------------------------------------------------

def test_heartbeat_line_and_file(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("AUTOPILOT_VERSION", "abc123")
    s = store(tmp_path)
    assert common.run_job("atlasx", lambda st, r: {"n": 1}, s) == 0
    out = lines(capsys)
    assert {"hotato_heartbeat": "atlasx", "severity": "INFO"} in out
    hb = s.get_json("heartbeats/atlasx.json")
    assert hb["outcome"] == "ok" and hb["version"] == "abc123" and hb["time"]


def test_crash_alerts_reraises_and_skips_heartbeat(tmp_path, capsys):
    s = store(tmp_path)

    def boom(st, r):
        raise ValueError("bad thing")
    with pytest.raises(ValueError):
        common.run_job("crashy", boom, s)
    out = lines(capsys)
    crash = alerts(out, "job_crash")
    assert crash and crash[0]["severity"] == "ERROR" and "bad thing" in crash[0]["detail"]
    assert not any("hotato_heartbeat" in o for o in out)
    assert s.get_json("heartbeats/crashy.json") is None
    rec = json.loads(next((tmp_path / "runs" / "crashy").iterdir()).read_text())
    assert rec["status"] == "error" and rec["error_kind"] == "job_crash"


def test_store_failure_is_a_crash_too(capsys):
    class Broken(common.Store):
        def exists(self, key):
            raise OSError("bucket unreachable")
    with pytest.raises(OSError):
        common.run_job("x", lambda st, r: 1, Broken(local_dir="/nonexistent"))
    assert alerts(lines(capsys), "job_crash")


def test_alert_shape(capsys):
    common.alert("k", "WARNING", "d", "j")
    assert lines(capsys) == [{"hotato_alert": "k", "severity": "WARNING", "detail": "d", "job": "j"}]
    with pytest.raises(ValueError):
        common.alert("k", "INFO", "d")


def test_kill_switch_alerts(tmp_path, capsys):
    s = store(tmp_path)
    s.put_bytes(common.KILL_KEY, b'{"reason": "budget cap"}')
    assert common.run_job("t", lambda st, r: 1, s) == 0
    a = alerts(lines(capsys), "kill_switch")
    assert a and a[0]["severity"] == "WARNING" and "budget cap" in a[0]["detail"]


# ---- write mode ---------------------------------------------------------------------

def test_env_modes(monkeypatch):
    assert common.env_mode() == "shadow"  # unset = forced shadow
    monkeypatch.setenv("DRY_RUN", "1")
    assert common.effective_mode("live") == "shadow"  # DRY_RUN=1 wins over a promotion
    monkeypatch.setenv("DRY_RUN", "auto")
    assert common.effective_mode("live") == "live" and common.effective_mode("shadow") == "shadow"
    assert common.effective_mode(None) == "shadow"
    monkeypatch.setenv("DRY_RUN", "0")
    assert common.effective_mode("shadow") == "live"
    monkeypatch.setenv("DRY_RUN", "yes please")
    assert common.env_mode() == "shadow"  # unknown values fail safe


def test_promoted_state_drives_dry_run_under_auto(tmp_path, monkeypatch):
    s = store(tmp_path)
    s.put_json(promotion.STATE_KEY, {"jobs": {"j": dict(promotion.new_job_state(), mode="live")}})
    seen = []
    monkeypatch.setenv("DRY_RUN", "auto")
    common.run_job("j", lambda st, r: seen.append(common.dry_run()), s)
    monkeypatch.setenv("DRY_RUN", "1")
    common.run_job("j", lambda st, r: seen.append(common.dry_run()), s)
    assert seen == [False, True]
    assert common.dry_run()  # outside a run: shadow


# ---- promotion ----------------------------------------------------------------------

def run(s, job, at, status="ok", violations=0, killed=False, block=False):
    mode = promotion.begin_run(s, job, at, killed=killed, kill_reason="budget cap", cfg=CFG)
    rec = {"job": job, "started": at.isoformat(), "status": "killed" if killed else status}
    if status == "error":
        rec["error_kind"] = "job_crash"
    if violations:
        rec["gate_violations"] = [{"kind": "owner_only_paths", "detail": "x"}] * violations
    if block:
        rec["promotion_block"] = "no GitHub credential"
    promotion.end_run(s, job, rec, at)
    return mode


def js(s, job="j"):
    return promotion.status(s)["jobs"][job]


def test_promotes_after_seven_clean_days(tmp_path, capsys):
    s = store(tmp_path)
    for n in range(7):
        assert run(s, "j", day(n)) == "shadow"
        run(s, "j", day(n, 18))  # several runs a day count once
    assert js(s)["clean_days"] == 6  # day 7 is still open
    assert run(s, "j", day(7)) == "live"
    a = alerts(lines(capsys), "promoted")
    assert len(a) == 1 and a[0]["severity"] == "WARNING"
    log = [json.loads(x) for x in (tmp_path / "promotion" / "log.jsonl").read_text().splitlines()]
    assert [e["event"] for e in log].count("day_closed") == 7 and log[-1]["event"] == "promoted"


def test_neutral_days_do_not_break_streak_but_dirty_days_do(tmp_path):
    s = store(tmp_path)
    for n in (0, 2, 4):  # gaps are neutral
        run(s, "j", day(n))
    assert js(s)["clean_days"] == 2  # days 0 and 2 closed clean, day 4 open
    run(s, "j", day(5), status="error")
    run(s, "j", day(5, 20))  # a later success the same day does not clean it
    run(s, "j", day(6))
    assert js(s)["clean_days"] == 0
    for n in range(7, 13):
        run(s, "j", day(n))
    assert js(s)["clean_days"] == 6 and js(s)["mode"] == "shadow"  # days 6..11 closed clean
    assert run(s, "j", day(13)) == "live"


def test_violation_or_block_spoils_the_day(tmp_path):
    s = store(tmp_path)
    run(s, "j", day(0))
    run(s, "j", day(1), violations=1)
    run(s, "j", day(2))
    assert js(s)["clean_days"] == 0
    run(s, "j", day(3), block=True)
    run(s, "j", day(4))
    assert js(s)["clean_days"] == 0


def test_per_job_threshold_for_weekly_job(tmp_path):
    s = store(tmp_path)
    run(s, "weekly", day(0))
    run(s, "weekly", day(7))
    assert run(s, "weekly", day(14)) == "live"


def test_auto_promote_off_never_promotes(tmp_path):
    s = store(tmp_path)
    cfg = dict(CFG, auto_promote=False)
    for n in range(20):
        promotion.begin_run(s, "j", day(n), cfg=cfg)
        promotion.end_run(s, "j", {"status": "ok", "started": day(n).isoformat()}, day(n))
    assert js(s)["mode"] == "shadow" and js(s)["clean_days"] >= 7


def live_store(tmp_path):
    s = store(tmp_path)
    s.put_json(promotion.STATE_KEY, {"jobs": {"j": dict(promotion.new_job_state(), mode="live", clean_days=9)}})
    return s


def test_demote_on_gate_violation(tmp_path, capsys):
    s = live_store(tmp_path)
    run(s, "j", day(0), violations=1)
    assert js(s)["mode"] == "shadow" and js(s)["clean_days"] == 0 and "gate violation" in js(s)["reason"]
    a = alerts(lines(capsys), "demoted")
    assert len(a) == 1 and a[0]["severity"] == "WARNING"


def test_demote_on_two_consecutive_failures_only(tmp_path):
    s = live_store(tmp_path)
    run(s, "j", day(0), status="error")
    run(s, "j", day(0, 13))
    run(s, "j", day(0, 14), status="error")
    assert js(s)["mode"] == "live"  # failures were not consecutive
    run(s, "j", day(1), status="error")
    assert js(s)["mode"] == "shadow" and "2 consecutive failed runs" in js(s)["reason"]


def test_demote_on_kill_switch(tmp_path):
    s = live_store(tmp_path)
    assert run(s, "j", day(0), killed=True) == "shadow"
    assert "kill switch" in js(s)["reason"] and js(s)["day"]["dirty"]


def test_after_demotion_the_job_must_earn_n_days_again(tmp_path):
    s = live_store(tmp_path)
    run(s, "j", day(0), violations=1)
    for n in range(1, 8):
        assert run(s, "j", day(n)) == "shadow"
    assert run(s, "j", day(8)) == "live"


def test_run_job_end_to_end_violation_demotes(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "auto")
    s = live_store(tmp_path)

    def bad(st, rec):
        common.gate_violation(rec, "owner_only_paths", "gates/policy.json")
        return {}
    assert common.run_job("j", bad, s) == 0
    out = lines(capsys)
    assert alerts(out, "gate_violation")[0]["severity"] == "ERROR"
    assert alerts(out, "demoted")
    assert js(s)["mode"] == "shadow"


def test_policy_json_switches():
    from gates.policy import load_config
    cfg = load_config()
    assert cfg["auto_promote"] is True and promotion.needed_days("builder", cfg) == 7
    assert cfg["shadow_mode"] is True  # merge gate unchanged


# ---- GitHub credentials ----------------------------------------------------------------

def no_sm(name):
    return None


class FakeHTTP:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, url, method="GET", headers=None, data=None, timeout=30):
        self.calls.append((method, url, dict(headers or {})))
        for frag, resp in self.routes.items():
            if frag in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError("unexpected " + url)


def rsa_pem():
    crypto = pytest.importorskip("cryptography")  # noqa: F841
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                            serialization.NoEncryption()).decode()
    return key, pem


def test_app_jwt_is_valid_rs256():
    key, pem = rsa_pem()
    import base64
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    tok = common.app_jwt("12345", pem.replace("\n", "\\n"), at=1_800_000_000)  # one-line key works too
    h, p, sig = tok.split(".")
    pad = lambda x: base64.urlsafe_b64decode(x + "=" * (-len(x) % 4))  # noqa: E731
    assert json.loads(pad(h)) == {"alg": "RS256", "typ": "JWT"}
    claims = json.loads(pad(p))
    assert claims == {"iat": 1_800_000_000 - 60, "exp": 1_800_000_000 + 540, "iss": "12345"}
    key.public_key().verify(pad(sig), f"{h}.{p}".encode(), padding.PKCS1v15(), hashes.SHA256())


def test_app_preferred_over_pat(monkeypatch):
    _, pem = rsa_pem()
    monkeypatch.setenv("GITHUB_APP_ID", "1")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", pem)
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "99")
    monkeypatch.setenv("GITHUB_BOT_TOKEN", "pat")
    fake = FakeHTTP({"/app/installations/99/access_tokens":
                     (201, {}, json.dumps({"token": "ghs_inst", "expires_at": "2099-01-01T00:00:00Z"}).encode())})
    monkeypatch.setattr(common, "http_resp", fake)
    assert common.get_github_token(fetch=no_sm) == "ghs_inst"
    assert fake.calls[0][0] == "POST" and fake.calls[0][2]["Authorization"].startswith("Bearer ey")
    assert common.get_github_token(fetch=no_sm) == "ghs_inst" and len(fake.calls) == 1  # cached per run


def test_app_failure_falls_back_to_pat_with_alert(monkeypatch, capsys):
    _, pem = rsa_pem()
    monkeypatch.setenv("GITHUB_APP_ID", "1")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", pem)
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "99")
    monkeypatch.setenv("GITHUB_BOT_TOKEN", "pat")
    err = urllib.error.HTTPError("u", 401, "no", {}, io.BytesIO(b""))
    fake = FakeHTTP({"access_tokens": err, "/rate_limit": (200, {}, b"{}")})
    monkeypatch.setattr(common, "http_resp", fake)
    assert common.get_github_token(fetch=no_sm) == "pat"
    assert alerts(lines(capsys), "github_app_failed")[0]["severity"] == "ERROR"


def test_absent_app_secrets_use_pat_quietly(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_BOT_TOKEN", "pat")
    monkeypatch.setattr(common, "http_resp", FakeHTTP({"/rate_limit": (200, {}, b"{}")}))
    assert common.get_github_token(fetch=no_sm) == "pat"
    assert alerts(lines(capsys)) == []


def test_partial_app_secrets_warn(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_APP_ID", "1")
    monkeypatch.setenv("GITHUB_BOT_TOKEN", "pat")
    monkeypatch.setattr(common, "http_resp", FakeHTTP({"/rate_limit": (200, {}, b"{}")}))
    assert common.get_github_token(fetch=no_sm) == "pat"
    assert alerts(lines(capsys), "github_app_incomplete")


@pytest.mark.parametrize("days,sev", [(60, None), (20, "WARNING"), (5, "ERROR"), (-1, "ERROR")])
def test_pat_expiry_header(monkeypatch, capsys, days, sev):
    at = dt.datetime(2026, 12, 1, tzinfo=UTC)
    exp = (at + dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S UTC")
    assert common.check_pat_expiry({"github-authentication-token-expiration": exp}, at) == sev
    a = alerts(lines(capsys), "github_token_expiring")
    assert (a[0]["severity"] if a else None) == sev


def test_pat_expiry_read_on_first_call(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_BOT_TOKEN", "pat")
    soon = (common.now() + dt.timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S -0000")
    fake = FakeHTTP({"/rate_limit": (200, {"github-authentication-token-expiration": soon}, b"{}")})
    monkeypatch.setattr(common, "http_resp", fake)
    common.get_github_token(fetch=no_sm)
    assert alerts(lines(capsys), "github_token_expiring")[0]["severity"] == "ERROR"
    assert fake.calls[0][2]["Authorization"] == "Bearer pat"


def test_expiry_formats():
    assert common.parse_gh_expiry("2026-12-31 00:00:00 -0800").utcoffset() == dt.timedelta(hours=-8)
    assert common.parse_gh_expiry("2026-12-31 00:00:00 UTC").utcoffset() == dt.timedelta(0)


def test_no_credential(monkeypatch):
    assert common.get_github_token(required=False, fetch=no_sm) is None
    with pytest.raises(RuntimeError):
        common.get_github_token(fetch=no_sm)


def test_secret_manager_fallback_and_empty_values(monkeypatch):
    got = {"github-bot-token": " pat-from-sm \n", "github-app-id": ""}
    assert common.read_secret("github-bot-token", got.get) == "pat-from-sm"
    assert common.read_secret("github-app-id", got.get) is None
    monkeypatch.setenv("GITHUB_BOT_TOKEN", "from-env")
    assert common.read_secret("github-bot-token", got.get) == "from-env"
    assert common.read_secret("github-app-id") is None  # off Cloud Run: no Secret Manager call


def test_github_job_without_credential_cannot_promote(tmp_path, monkeypatch):
    s = store(tmp_path)
    monkeypatch.setattr(common, "_secret_manager", no_sm)
    common.run_job("upkeep", lambda st, r: 1, s)
    rec = json.loads(next((tmp_path / "runs" / "upkeep").iterdir()).read_text())
    assert rec["github_auth"] == "none" and rec["promotion_block"]
    assert promotion.status(s)["jobs"]["upkeep"]["day"]["blocked"] == 1


def test_agent_never_sees_github_credentials(monkeypatch):
    import builder
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "k")
    monkeypatch.setenv("GITHUB_BOT_TOKEN", "t")
    env = builder.agent_env()
    assert "GITHUB_APP_PRIVATE_KEY" not in env and "GITHUB_BOT_TOKEN" not in env


# ---- model drift ---------------------------------------------------------------------------

MCFG = {"models": {"builder": {"publisher": "anthropic", "env": "BUILDER_MODEL", "use": ["m1", "m2", "m3"]}}}


def probe_of(available, unknown=()):
    def probe(publisher, model):
        return None if model in unknown else model in available
    return probe


def test_model_primary_ok(capsys):
    assert models.resolve("builder", probe_of({"m1", "m2"}), MCFG) == "m1"
    assert alerts(lines(capsys)) == []


def test_model_fallback_warns(capsys):
    assert models.resolve("builder", probe_of({"m3"}), MCFG) == "m3"
    a = alerts(lines(capsys), "model_fallback")
    assert a[0]["severity"] == "WARNING" and "m1" in a[0]["detail"] and "m3" in a[0]["detail"]


def test_model_none_available_errors(tmp_path, capsys, monkeypatch):
    with pytest.raises(common.ModelUnavailable):
        models.resolve("builder", probe_of(set()), MCFG)
    assert alerts(lines(capsys), "model_unavailable")[0]["severity"] == "ERROR"

    # inside a job: exit code 1, no job_crash, counts as a failed run
    def work(st, rec):
        models.resolve("builder", probe_of(set()), MCFG)
    assert common.run_job("builder2", work, store(tmp_path)) == 1
    out = lines(capsys)
    assert alerts(out, "model_unavailable") and not alerts(out, "job_crash")
    assert promotion.status(store(tmp_path))["jobs"]["builder2"]["consecutive_failures"] == 1


def test_model_probe_unknown_counts_as_available(capsys):
    assert models.resolve("builder", probe_of(set(), unknown={"m1"}), MCFG) == "m1"
    assert alerts(lines(capsys), "model_probe_failed")


def test_model_env_override_first(monkeypatch):
    monkeypatch.setenv("BUILDER_MODEL", "pinned")
    assert models.candidates("builder", MCFG["models"]) == ["pinned", "m1", "m2", "m3"]
    assert models.resolve("builder", probe_of({"pinned"}), MCFG) == "pinned"


def test_models_in_policy_json():
    m = models.model_config()
    assert set(m) >= {"atlas", "builder", "reviewer", "simulate"}
    assert all(spec["use"] for spec in m.values())


def test_vertex_probe_classifies_http_errors(monkeypatch):
    monkeypatch.setattr(models, "gcp_token", lambda: "t")

    def raiser(code):
        def f(*a, **k):
            raise urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(b""))
        return f
    monkeypatch.setattr(models, "http_resp", raiser(404))
    assert models.vertex_probe("google", "gone") is False
    monkeypatch.setattr(models, "http_resp", raiser(503))
    assert models.vertex_probe("anthropic", "x") is None
    monkeypatch.setattr(models, "http_resp", lambda *a, **k: (200, {}, b"{}"))
    assert models.vertex_probe("google", "ok") is True


# ---- store concurrency helpers ----------------------------------------------------------------

def test_update_json_and_append(tmp_path):
    s = store(tmp_path)
    s.update_json("a/b.json", lambda o: dict(o, n=o.get("n", 0) + 1), {})
    s.update_json("a/b.json", lambda o: dict(o, n=o["n"] + 1), {})
    assert s.get_json("a/b.json") == {"n": 2}
    s.append_jsonl("a/log.jsonl", {"x": 1})
    s.append_jsonl("a/log.jsonl", [{"x": 2}, {"x": 3}])
    assert [json.loads(x)["x"] for x in (tmp_path / "a" / "log.jsonl").read_text().splitlines()] == [1, 2, 3]


def test_redact_strips_tokens_from_clone_errors():
    import subprocess
    import common
    err = subprocess.CalledProcessError(
        128, ["git", "clone", "https://x-access-token:ghs_abcdefghijklmnopqrstuvwx@github.com/o/r.git", "/tmp/d"])
    out = common.redact(f"{type(err).__name__}: {err}")
    assert "ghs_abcdefghijklmnopqrstuvwx" not in out
    assert "***@github.com" in out
    assert "ghp_" + "A" * 30 not in common.redact("token ghp_" + "A" * 30)
    assert "Bearer ***" in common.redact("Authorization: Bearer ya29.abcdefghijklmnopqrstu")


def test_alert_detail_is_redacted(capsys):
    import common
    obj = common.alert("job_crash", "ERROR", "https://x-access-token:SECRETSECRET@github.com/o/r")
    assert "SECRETSECRET" not in obj["detail"]


def test_vertex_403_is_unknown_not_retired(monkeypatch):
    import urllib.error
    import vertex_models

    def boom(*a, **k):
        raise urllib.error.HTTPError("u", 403, "forbidden", {}, None)
    monkeypatch.setattr(vertex_models, "http_resp", boom)
    monkeypatch.setattr(vertex_models, "gcp_token", lambda: "t")
    assert vertex_models.vertex_probe("google", "gemini-x") is None
