"""Guards on the live (Vapi-verified) adapter ops: the vendor-URL download must
flow through capture's validated path, delete_clone must technically refuse a
production assistant, apply_variant must stamp the staging marker delete_clone
checks, dotted variant deltas must nest without clobbering, and every provider
request must carry the explicit hotato User-Agent (Cloudflare 403s urllib's
default UA before auth ever happens)."""
import io
import urllib.request

import pytest

from hotato import apply as apply_mod
from hotato import inspectcfg
from hotato.fleet import adapters
from hotato.fleet.adapters import _nest_dotted

# --- _nest_dotted -----------------------------------------------------------

def test_nest_dotted_expands_dotted_path():
    assert _nest_dotted({"stopSpeakingPlan.numWords": 0}) == {
        "stopSpeakingPlan": {"numWords": 0}}


def test_nest_dotted_field_to_form():
    assert _nest_dotted({"field": "stopSpeakingPlan.numWords", "to": 2}) == {
        "stopSpeakingPlan": {"numWords": 2}}


def test_nest_dotted_plain_nested_passthrough():
    patch = {"stopSpeakingPlan": {"numWords": 1}}
    assert _nest_dotted(patch) == patch


def test_nest_dotted_mixed_dotted_and_nested_merge_without_clobber():
    # a dotted key and a plain nested dict for the SAME top-level field must
    # deep-merge; losing either side would silently apply a partial variant
    assert _nest_dotted({"a.b": 1, "a": {"c": 2}}) == {"a": {"b": 1, "c": 2}}
    assert _nest_dotted({"a": {"c": 2}, "a.b": 1}) == {"a": {"b": 1, "c": 2}}


# --- delete_clone guards ----------------------------------------------------

def _vapi():
    return adapters.get_adapter("vapi", api_key="k-test")


def _receipt(clone_id, *, provider="vapi", nonce="nonce-1"):
    """A durable clone receipt that names THIS clone id + provider -- the PRIMARY
    authorization delete_clone now requires. A production assistant this tool
    never cloned has no such receipt."""
    return {"receipt_id": f"clonercpt-{clone_id}", "clone_id": clone_id,
            "provider": provider, "nonce": nonce, "trial_id": "t1"}


def test_delete_clone_requires_a_clone_id():
    out = _vapi().delete_clone({"source_id": "abc", "pending": True})
    assert out == {"deleted": False, "reason": "no clone id"}


def test_delete_clone_refuses_url_smuggling_id():
    with pytest.raises(ValueError, match="not a valid"):
        _vapi().delete_clone("abc/../../org", receipt=_receipt("abc/../../org"))


def test_delete_clone_refuses_without_a_receipt():
    # PRIMARY authorization: even a validly-named staging clone id cannot be
    # deleted without the durable clone receipt (a mutable display name is not
    # sufficient; the production-assistant attack has no receipt).
    with pytest.raises(ValueError, match="no clone receipt"):
        _vapi().delete_clone("abc123")


def test_delete_clone_refuses_production_assistant_named_hotato_without_receipt(monkeypatch):
    # THE audit scenario: a PRODUCTION assistant that legitimately carries a
    # 'hotato' name prefix. Without a durable clone receipt it is refused BEFORE
    # any network read or DELETE -- the mutable name marker is never sufficient
    # authorization, so the prod assistant can never be destroyed.
    def _no_net(*a, **k):  # pragma: no cover - reaching this is the failure
        raise AssertionError("no network call may happen without a clone receipt")
    monkeypatch.setattr(apply_mod, "_http_json", _no_net)
    monkeypatch.setattr(urllib.request, "urlopen", _no_net)
    with pytest.raises(ValueError, match="no clone receipt"):
        _vapi().delete_clone("prod-123")


def test_delete_clone_refuses_receipt_for_a_different_clone():
    # A receipt cannot be replayed onto another assistant: the id it names must
    # match the id being deleted.
    with pytest.raises(ValueError, match="different clone id"):
        _vapi().delete_clone("prod-123", receipt=_receipt("staging-999"))


def test_delete_clone_refuses_receipt_for_a_different_provider():
    with pytest.raises(ValueError, match="across providers"):
        _vapi().delete_clone("abc123", receipt=_receipt("abc123", provider="retell"))


def test_delete_clone_refuses_assistant_without_staging_marker(monkeypatch):
    # the fetched assistant is named like a production agent -> REFUSE (the name
    # marker is a SECONDARY invariant), and the DELETE request must never be
    # issued -- even WITH a receipt, the name marker still guards.
    monkeypatch.setattr(apply_mod, "_http_json",
                        lambda *a, **k: {"id": "abc123", "name": "Riley"})
    def _no_delete(*a, **k):  # pragma: no cover - reaching this is the failure
        raise AssertionError("DELETE must not be issued for a non-staging name")
    monkeypatch.setattr(urllib.request, "urlopen", _no_delete)
    with pytest.raises(ValueError, match="staging marker"):
        _vapi().delete_clone("abc123", receipt=_receipt("abc123"))


def test_delete_clone_deletes_marked_staging_clone(monkeypatch):
    monkeypatch.setattr(apply_mod, "_http_json",
                        lambda *a, **k: {"id": "abc123", "name": "hotato-staging-t1"})
    issued = {}
    def _fake_urlopen(req, timeout=None):
        issued["method"] = req.get_method()
        issued["url"] = req.full_url
        return io.BytesIO(b"")
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    out = _vapi().delete_clone({"clone_id": "abc123"}, receipt=_receipt("abc123"))
    assert out == {"deleted": True, "clone_id": "abc123"}
    assert issued["method"] == "DELETE" and issued["url"].endswith("/abc123")


def test_delete_clone_404_on_fetch_is_a_noop(monkeypatch):
    def _gone(*a, **k):
        err = ValueError("clone read failed: HTTP 404 for that id")
        err.status_code = 404
        raise err
    monkeypatch.setattr(apply_mod, "_http_json", _gone)
    out = _vapi().delete_clone("abc123", receipt=_receipt("abc123"))
    assert out["deleted"] is True and out["already_gone"] is True


def test_delete_clone_non_404_error_with_404_in_text_is_not_a_noop(monkeypatch):
    # A genuine non-404 failure (e.g. a real 500, auth failure, or outage) whose
    # message text happens to CONTAIN the substring "404" -- via the id embedded
    # in the URL or via vendor response detail -- must NOT be treated as an
    # already-gone no-op. Only the real numeric status_code decides that.
    def _real_error(*a, **k):
        err = ValueError(
            "HTTP 500 from GET https://api.vapi.ai/assistant/id-with-404-in-it: "
            "Internal Server Error. some vendor detail mentioning 404 by accident"
        )
        err.status_code = 500
        raise err
    monkeypatch.setattr(apply_mod, "_http_json", _real_error)
    with pytest.raises(ValueError, match="HTTP 500"):
        _vapi().delete_clone("abc123", receipt=_receipt("abc123"))


def test_delete_clone_error_without_status_code_is_not_a_noop(monkeypatch):
    # A ValueError with no .status_code attribute at all (e.g. a network/URLError
    # path, or any future raiser that forgets to set it) must fail closed --
    # never silently treated as an already-gone delete.
    def _no_status(*a, **k):
        raise ValueError("clone read failed: HTTP 404 for that id")
    monkeypatch.setattr(apply_mod, "_http_json", _no_status)
    with pytest.raises(ValueError, match="404"):
        _vapi().delete_clone("abc123", receipt=_receipt("abc123"))


# --- capture_result download validation ---------------------------------------

def test_capture_result_refuses_file_url_from_vendor_json(monkeypatch, tmp_path):
    # a tampered/compromised vendor response pointing the "recording" at a
    # local file must be refused by the validated download path, never fetched
    from hotato import capture as capture_mod
    call = {"id": "c1", "artifact": {"recording": {"stereoUrl": "file:///etc/passwd"}}}
    monkeypatch.setattr(capture_mod, "_http_get_json", lambda *a, **k: call)
    with pytest.raises(ValueError, match="scheme"):
        _vapi().capture_result(None, call_id="c1", out_path=str(tmp_path / "o.wav"))


# --- apply_variant marker + clone_id normalization ----------------------------

def test_apply_variant_stamps_staging_marker_and_clone_id(monkeypatch):
    seen = {}
    def _fake_create(stack, source_id, name, merge_patch, api_key):
        seen.update(stack=stack, name=name, patch=merge_patch)
        return {"created": True, "id": "new-clone-9"}
    monkeypatch.setattr(apply_mod, "create_clone", _fake_create)
    out = _vapi().apply_variant({"source_id": "src1", "name": "mytest"},
                                {"config_delta": {"stopSpeakingPlan.numWords": 0}})
    assert seen["name"] == "hotato-mytest"  # marker stamped for delete_clone's check
    assert seen["patch"] == {"stopSpeakingPlan": {"numWords": 0}}
    assert out["clone_id"] == "new-clone-9"


def test_apply_variant_survives_created_bool_with_no_id(monkeypatch):
    monkeypatch.setattr(apply_mod, "create_clone",
                        lambda **k: {"created": True})
    out = _vapi().apply_variant("src1", {"config_delta": {"x": 1}})
    assert out["clone_id"] is None  # no crash on the bool "created" field


# --- explicit hotato User-Agent on provider requests ---------------------------

def _capture_request(monkeypatch, module):
    seen = {}
    def _fake_urlopen(req, timeout=None):
        seen["ua"] = req.get_header("User-agent")
        class _R(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return _R(b"{}")
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    return seen


def test_apply_http_json_sends_hotato_user_agent(monkeypatch):
    seen = _capture_request(monkeypatch, apply_mod)
    apply_mod._http_json("GET", "https://api.vapi.ai/assistant/x",
                         headers={"Authorization": "Bearer k"}, body=None, timeout=5)
    assert seen["ua"] and seen["ua"].startswith("hotato/")


def test_inspectcfg_sends_hotato_user_agent(monkeypatch):
    seen = _capture_request(monkeypatch, inspectcfg)
    inspectcfg._http_get_json("https://api.vapi.ai/assistant/x",
                              headers={"Authorization": "Bearer k"})
    assert seen["ua"] and seen["ua"].startswith("hotato/")


def test_vapi_strip_tuple_removes_server_computed_secret_flag():
    assert "isServerUrlSecretSet" in apply_mod._CLONE_ENDPOINTS["vapi"]["strip"]
