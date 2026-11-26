"""Vertex AI model names live in one place: the "models" section of gates/policy.json.

  "models": {"<job>": {"publisher": "google"|"anthropic", "env": "<override var>",
                       "use": ["<primary>", "<successor>", ...]}}

At job start resolve(job) probes each candidate in order and returns the first one that
is available. Falling past the primary logs hotato_alert model_fallback (WARNING); no
candidate available logs model_unavailable (ERROR) and raises common.ModelUnavailable,
which makes the run exit non-zero. An env override (e.g. BUILDER_MODEL) is tried first.
A probe that cannot tell (network or server error) counts as available, with a
model_probe_failed WARNING: the real call then fails loudly instead of a false fallback.
"""
import json
import os
import sys
import urllib.error

from common import ModelUnavailable, alert, gcp_token, http_resp

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def model_config(cfg=None):
    if cfg is None:
        from gates.policy import load_config
        cfg = load_config()
    return cfg.get("models", {})


def candidates(job, models):
    spec = models[job]
    out = []
    override = os.environ.get(spec.get("env", ""), "").strip() if spec.get("env") else ""
    for m in ([override] if override else []) + list(spec.get("use", [])):
        if m and m not in out:
            out.append(m)
    return out


def vertex_probe(publisher, model):
    """True = available, False = not found (404, the model is retired or unknown),
    None = unknown (400, 403 permission or API off, 429, 5xx, network)."""
    project = os.environ.get("GCP_PROJECT", "")
    region = os.environ.get("GCP_REGION", "us-central1")
    host = "aiplatform.googleapis.com" if region == "global" else f"{region}-aiplatform.googleapis.com"
    try:
        headers = {"Authorization": f"Bearer {gcp_token()}", "Content-Type": "application/json"}
        if publisher == "google":
            # countTokens is free and fails for retired or unknown models.
            url = (f"https://{host}/v1/projects/{project}/locations/{region}/publishers/google/"
                   f"models/{model}:countTokens")
            body = json.dumps({"contents": [{"role": "user", "parts": [{"text": "ping"}]}]}).encode()
            http_resp(url, "POST", headers, body, timeout=20)
        else:
            # Claude on Vertex: the documented call with max_tokens=1 (a fraction of a cent).
            url = (f"https://{host}/v1/projects/{project}/locations/{region}/publishers/{publisher}/"
                   f"models/{model}:rawPredict")
            body = json.dumps({"anthropic_version": "vertex-2023-10-16", "max_tokens": 1,
                               "messages": [{"role": "user", "content": "ping"}]}).encode()
            http_resp(url, "POST", headers, body, timeout=30)
        return True
    except urllib.error.HTTPError as e:
        return False if e.code == 404 else None
    except (urllib.error.URLError, OSError, KeyError, ValueError):
        return None


def resolve(job, probe=None, cfg=None):
    """-> model name to use for `job`. Raises ModelUnavailable (already alerted)."""
    models = model_config(cfg)
    spec = models[job]
    probe = probe or vertex_probe
    cands = candidates(job, models)
    primary = cands[0] if cands else None
    tried = []
    for m in cands:
        ok = probe(spec.get("publisher", "google"), m)
        if ok is None:
            alert("model_probe_failed", "WARNING", f"{job}: could not check {m}; using it", job)
            ok = True
        if ok:
            if m != primary:
                alert("model_fallback", "WARNING",
                      f"{job}: {primary} is unavailable, using {m}; update models.{job} in gates/policy.json", job)
            return m
        tried.append(m)
    alert("model_unavailable", "ERROR",
          f"{job}: none of {tried} is available on Vertex AI; add a successor to models.{job} in gates/policy.json",
          job)
    raise ModelUnavailable(f"no available model for {job} (tried {tried})")
