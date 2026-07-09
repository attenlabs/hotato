"""The most conservative rung of the fix ladder: apply a patch to a CLONE only.

``hotato apply PATCH_JSON --clone --name NAME`` is the one place in Hotato that
can mutate EXTERNAL platform state, so it is the most guarded command in the
codebase. It never touches the production/source assistant under any flag
combination. What it does, in one line: read a ``hotato patch`` artifact, prove
the change is safe to apply at all, and either PRINT the fresh staging clone it
WOULD create (the default dry run, fully offline) or, only with ``--yes`` and
credentials, create a NEW staging assistant that is the source config with the
patch applied.

Five hard safety rules are enforced here, by construction:

1. CLONE-ONLY. There is no production-apply path in this version. A non-``--clone``
   invocation is a clean usage error ("production apply is not supported; use
   --clone to apply to a fresh staging assistant"). Nothing here ever PUTs or
   PATCHes the source; the only writing call is a POST that creates a NEW
   assistant.

2. REFUSAL-FIRST. If the patch is the both-axes threshold funnel
   (``do_not_tune_single_threshold`` / ``threshold_funnel``), apply REFUSES
   before doing anything and prints the canon's exact refusal. The refusal is a
   FEATURE: it exits with a documented, distinct code (:data:`REFUSAL_EXIT_CODE`)
   so a script can tell "refused by design" apart from a usage error.

3. OPPOSITE-RISK REQUIRED. apply refuses unless it is handed a battery with BOTH
   a yield fixture AND a hold fixture, so a one-sided change is never applied
   blind: the same clone can be proven both ways with ``hotato verify``.

4. GATED SIDE EFFECT. The default is a dry run that prints exactly the clone it
   WOULD create and the patch it WOULD apply, creating nothing and touching no
   network. Only ``--yes`` WITH credentials calls the platform. The actual
   create is the ONLY networked function (:func:`create_clone`), isolated: it
   reads the source config (GET), applies the patch to a COPY, and creates a NEW
   assistant (POST). It never issues a PUT/PATCH against the source.

5. NAME REQUIRED. The staging clone must be named explicitly (``--name``); apply
   never invents a name for something it creates.

The core (:func:`build_apply`, :func:`apply_patch_to_config`,
:func:`build_clone_config`) is pure and offline: it reads the patch (and the
referenced plan when present), evaluates the gates, and builds the clone payload
without any network call. All networking lives in :func:`create_clone`, reached
only from the CLI under ``--yes``.

Create/read endpoints, as DOCUMENTATION (only :func:`create_clone` ever calls
them), verified 2026-07-06:

* Vapi:   read  GET  https://api.vapi.ai/assistant/{id}
          clone POST https://api.vapi.ai/assistant        (Bearer VAPI_API_KEY)
* Retell: read  GET  https://api.retellai.com/get-agent/{id}
          clone POST https://api.retellai.com/create-agent (Bearer RETELL_API_KEY)
"""

from __future__ import annotations

import copy
import json
import os
import re
from typing import Optional

from . import errors as _errors

SCHEMA_ID = "hotato.apply.v1"
_PATCH_KIND = "patch"
_PLAN_SCHEMA_ID = "hotato.fixplan.v1"

# The refusal is a documented, distinct outcome: 0 = dry run / created, 2 = a
# usage error, 3 = the principled both-axes refusal (this exit code IS the
# feature -- a caller branches on it to see "no single-threshold patch, by
# design", never a bug and never a usage mistake).
REFUSAL_EXIT_CODE = 3

# The exact canon refusal (operator spec, 2026-07-08). Printed verbatim on the
# threshold-funnel path. Vendor-neutral by policy: it names the KIND of fix, no
# product and no numbers.
REFUSAL_HEADLINE = "No config patch will be applied"
REFUSAL_REASON = (
    "both missed real interruption and false stop on backchannel, one "
    "threshold cannot safely fix both"
)
REFUSAL_RECOMMENDED = (
    "enable or add engagement-control / backchannel-aware turn detection"
)
REFUSAL_LINES = (
    REFUSAL_HEADLINE,
    f"Reason: {REFUSAL_REASON}",
    f"Recommended: {REFUSAL_RECOMMENDED}",
)

# The message for a non-clone invocation (operator spec, verbatim).
PRODUCTION_APPLY_UNSUPPORTED = (
    "production apply is not supported; use --clone to apply to a fresh "
    "staging assistant"
)

# Only REST-config platforms have an assistant/agent to clone through an API.
# LiveKit / Pipecat keep turn-taking config in the agent SOURCE (there is no
# assistant to clone); Twilio is transport; generic has no target.
CLONE_STACKS = ("vapi", "retell")

# A platform id substituted into a REST URL must be a plain identifier, so a
# tampered patch/plan can never smuggle a second path segment or a shell/URL
# fragment into the GET the create path issues. Same rule patch.py enforces.
_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Per-stack endpoints (documentation; only create_clone calls them) and the
# field that carries the assistant/agent display name. ``strip`` lists the
# server-assigned keys dropped from a fetched source before it is POSTed as a
# NEW assistant, so the clone is a fresh object, never an overwrite of the
# source.
_CLONE_ENDPOINTS = {
    "vapi": {
        "read_method": "GET",
        "read_url_template": "https://api.vapi.ai/assistant/{id}",
        "create_method": "POST",
        "create_url": "https://api.vapi.ai/assistant",
        "id_key": "assistant_id",
        "id_placeholder": "<assistant-id>",
        "name_field": "name",
        "auth": "Bearer $VAPI_API_KEY",
        "env": "VAPI_API_KEY",
        "id_result_key": "id",
        "strip": ("id", "orgId", "createdAt", "updatedAt"),
        "provenance": (
            "POST /assistant (create a new assistant) + GET /assistant/{id} "
            "(read the source, read-only), verified against "
            "docs.vapi.ai/api-reference/assistants, 2026-07-06"
        ),
    },
    "retell": {
        "read_method": "GET",
        "read_url_template": "https://api.retellai.com/get-agent/{id}",
        "create_method": "POST",
        "create_url": "https://api.retellai.com/create-agent",
        "id_key": "agent_id",
        "id_placeholder": "<agent-id>",
        "name_field": "agent_name",
        "auth": "Bearer $RETELL_API_KEY",
        "env": "RETELL_API_KEY",
        "id_result_key": "agent_id",
        "strip": ("agent_id", "last_modification_timestamp"),
        "provenance": (
            "POST /create-agent (create a new agent) + GET /get-agent/{id} "
            "(read the source, read-only), verified against "
            "docs.retellai.com/api-references, 2026-07-06"
        ),
    },
}

_HONEST = (
    "hotato apply only ever creates a NEW staging assistant and applies the "
    "patch to that clone; it never mutates the production/source assistant."
)


# --- referenced plan (optional; the patch is self-describing) ---------------

def load_referenced_plan(patch: dict, patch_source: Optional[str]) -> Optional[dict]:
    """Best-effort load of the fix plan a patch references (``source_plan``),
    resolved relative to the patch file when the recorded path is relative.

    Returns the plan dict when it is present and is a real fix plan (schema
    ``hotato.fixplan.v1``), else ``None``. A moved/absent/foreign plan is not an
    error: the patch already carries everything apply needs (the decision, the
    stack, the merge-patch, and the endpoint with the resolved id), so the plan
    is read only to enrich the source id when available. Never networked."""
    ref = patch.get("source_plan")
    if not ref or not isinstance(ref, str):
        return None
    candidates = [ref]
    if not os.path.isabs(ref) and patch_source:
        candidates.insert(0, os.path.join(os.path.dirname(os.path.abspath(patch_source)), ref))
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as fh:
                plan = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(plan, dict) and plan.get("schema") == _PLAN_SCHEMA_ID:
            return plan
    return None


# --- gate predicates --------------------------------------------------------

def is_threshold_funnel(patch: dict) -> bool:
    """True for the both-axes case the refusal fires on: the plan refused
    single-threshold tuning. Read from the patch alone (self-describing), so it
    holds whether or not the referenced plan file is still on disk."""
    return (
        patch.get("plan_decision") == "do_not_tune_single_threshold"
        or patch.get("plan_finding") == "threshold_funnel"
    )


def _validate_patch(patch: dict) -> None:
    if not isinstance(patch, dict) or patch.get("kind") != _PATCH_KIND \
            or patch.get("tool") != "hotato":
        raise ValueError(
            "not a hotato patch artifact (kind 'patch'). Produce one first: "
            "hotato patch fixplan.json --format json --out patch.json"
        )


def _source_id(patch: dict, plan: Optional[dict], endpoint: dict) -> Optional[str]:
    """The source assistant/agent id to clone FROM, or None when it is not
    resolved. Prefer the referenced plan's target; otherwise read it off the
    patch's own (resolved) endpoint URL. A value that is not a plain id is
    refused, never interpolated into a URL."""
    id_key = endpoint["id_key"]
    resolved = None
    if plan:
        resolved = (plan.get("target") or {}).get(id_key)
    if resolved is None:
        ep = (patch.get("artifact") or {}).get("endpoint") or {}
        if ep.get("id_resolved") and isinstance(ep.get("url"), str):
            tail = ep["url"].rstrip("/").rsplit("/", 1)[-1]
            if tail and tail != endpoint["id_placeholder"]:
                resolved = tail
    if resolved is None:
        return None
    resolved = str(resolved)
    if not _ID_RE.match(resolved):
        raise ValueError(
            f"the source {id_key} {resolved!r} is not a valid platform id "
            "(allowed: letters, digits, '.', '-', '_'); refusing to build a "
            "clone URL from it."
        )
    return resolved


# --- opposite-risk battery --------------------------------------------------

def _iter_expectations(obj):
    """Yield 'yield' / 'hold' for each labelled fixture in a JSON doc: a hotato
    run envelope (per event's ``expected_yield``) or a scenario file (its
    ``expected.yield``). Anything else yields nothing."""
    if not isinstance(obj, dict):
        return
    # run envelope: events carry expected_yield
    if isinstance(obj.get("events"), list) and obj.get("tool") == "hotato":
        for ev in obj["events"]:
            if isinstance(ev, dict) and "expected_yield" in ev:
                yield "yield" if ev.get("expected_yield") else "hold"
        return
    # scenario file: expected.yield is the label
    expected = obj.get("expected")
    if isinstance(expected, dict) and "yield" in expected:
        yield "yield" if expected.get("yield") else "hold"


def battery_classes(path: str) -> dict:
    """Scan an opposite-risk battery and report which labels it covers.

    ``path`` is a directory (a fixtures dir with a ``scenarios/`` subfolder, or a
    folder of run-envelope / scenario JSONs) or a single JSON file. Returns
    ``{"path", "n", "has_yield", "has_hold", "files"}``. Filesystem read only; no
    network. Unreadable / non-JSON files in a directory are skipped rather than
    crashing the scan; a single-file path that is not readable JSON is a clean
    usage error."""
    files = []
    if os.path.isdir(path):
        scen = os.path.join(path, "scenarios")
        base = scen if os.path.isdir(scen) else path
        for name in sorted(os.listdir(base)):
            if name.endswith(".json"):
                files.append(os.path.join(base, name))
    elif os.path.isfile(path):
        files.append(path)
    else:
        raise ValueError(
            f"--battery {path!r} is not a file or directory. Point it at your "
            "fixtures (a folder with scenarios/, or run-envelope JSONs) that "
            "carries BOTH a yield and a hold fixture."
        )

    has_yield = has_hold = False
    n = 0
    single = os.path.isfile(path)
    for fp in files:
        try:
            with open(fp, encoding="utf-8") as fh:
                obj = json.load(fh)
        except (OSError, ValueError) as exc:
            if single:
                raise ValueError(
                    f"--battery {fp!r} is not readable JSON: {exc}"
                ) from exc
            continue
        for expect in _iter_expectations(obj):
            n += 1
            if expect == "yield":
                has_yield = True
            elif expect == "hold":
                has_hold = True
    return {
        "path": path,
        "n": n,
        "has_yield": has_yield,
        "has_hold": has_hold,
        "files": len(files),
    }


def _require_opposite_risk(battery_dir: Optional[str]) -> dict:
    if not battery_dir:
        raise ValueError(
            "apply refuses without an opposite-risk battery: pass --battery DIR "
            "carrying BOTH a yield fixture AND a hold fixture, so the change is "
            "verifiable both ways and never applied blind."
        )
    classes = battery_classes(battery_dir)
    missing = []
    if not classes["has_yield"]:
        missing.append("a yield fixture (a real interruption the agent must "
                       "stop for)")
    if not classes["has_hold"]:
        missing.append("a hold fixture (a backchannel the agent must keep the "
                       "floor through)")
    if missing:
        raise ValueError(
            "apply refuses: the opposite-risk battery at "
            f"{battery_dir!r} is missing " + " and ".join(missing)
            + ". A one-sided battery cannot catch the opposite risk a threshold "
            "move trades into, so the fix would be applied blind. Add the "
            "missing fixture(s) first."
        )
    return classes


# --- pure clone-config construction (source + patch; source untouched) ------

def apply_patch_to_config(source_config: dict, merge_patch: dict) -> dict:
    """Return a NEW config that is ``source_config`` with ``merge_patch`` deep-
    merged on top. Pure: ``source_config`` is deep-copied first and NEVER
    mutated, so the source object a caller holds is untouched. Nested objects
    merge key by key; a scalar or list value replaces the source value at that
    key (matching JSON merge-patch semantics for the flat fields Hotato sets)."""
    if not isinstance(source_config, dict):
        raise ValueError("source config must be a JSON object to clone from")
    result = copy.deepcopy(source_config)
    _deep_merge(result, merge_patch or {})
    return result


def _deep_merge(dst: dict, patch: dict) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            _deep_merge(dst[key], value)
        else:
            dst[key] = copy.deepcopy(value)


def build_clone_config(source_config: dict, *, stack: str, name: str,
                       merge_patch: dict) -> dict:
    """The staging-assistant config the clone is created with: the source config
    with the patch applied, given the new display name, and stripped of the
    server-assigned ids so it is a fresh object (never an overwrite of the
    source). Pure; does not mutate ``source_config``."""
    endpoint = _CLONE_ENDPOINTS[stack]
    cfg = apply_patch_to_config(source_config, merge_patch)
    for key in endpoint["strip"]:
        cfg.pop(key, None)
    cfg[endpoint["name_field"]] = name
    return cfg


# --- the core gate + payload builder (pure, offline) ------------------------

def build_apply(
    patch: dict,
    *,
    name: Optional[str],
    clone: bool,
    battery_dir: Optional[str],
    patch_source: Optional[str] = None,
    plan: Optional[dict] = None,
) -> dict:
    """Evaluate every gate and build the clone payload. Pure and OFFLINE: reads
    the patch (and the already-loaded referenced plan), returns the apply dict.
    Never touches the network or any platform.

    Returns a result dict. On the threshold-funnel case it returns a REFUSAL
    result (``refused: True``); the caller prints the canon refusal and exits
    :data:`REFUSAL_EXIT_CODE`. Every non-refusal gate failure raises ValueError,
    which the CLI surfaces as the standard exit-2 usage error."""
    # 1. CLONE-ONLY: there is no production-apply path in this version.
    if not clone:
        raise ValueError(PRODUCTION_APPLY_UNSUPPORTED)

    _validate_patch(patch)
    stack = (patch.get("stack") or "generic").strip().lower()

    # 2. REFUSAL-FIRST: the both-axes threshold funnel is refused before any
    # other check, before touching the battery, and before any network.
    if is_threshold_funnel(patch):
        return _refusal_result(patch, stack, patch_source)

    # 3. Only REST-config platforms have an assistant to clone.
    if stack not in CLONE_STACKS:
        raise ValueError(_no_clone_target_reason(stack))

    # 4. There must be a concrete config change to apply (never apply blind).
    endpoint = _CLONE_ENDPOINTS[stack]
    if not patch.get("config_patchable"):
        raise ValueError(
            "this patch produced no config change to apply (plan decision "
            f"{patch.get('plan_decision')!r}); there is nothing to clone. "
            + (patch.get("reason") or "")
        )
    artifact = patch.get("artifact") or {}
    merge_patch = artifact.get("merge_patch")
    if not isinstance(merge_patch, dict) or not merge_patch:
        raise ValueError(
            "the patch has no concrete value to apply (the plan could not read "
            "the current config value, so it carries direction only). Inspect "
            "the live config and re-plan before applying: hotato inspect "
            f"--stack {stack} ..."
        )

    # 5. NAME REQUIRED: never invent a name for something it creates.
    if not name or not str(name).strip():
        raise ValueError(
            "--name NAME is required: the staging clone must be named "
            "explicitly (apply never invents a name for something it creates)."
        )

    # 6. OPPOSITE-RISK REQUIRED: both a yield and a hold fixture, or refuse.
    opposite_risk = _require_opposite_risk(battery_dir)

    source_id = _source_id(patch, plan, endpoint)
    read_url = (
        endpoint["read_url_template"].format(id=source_id)
        if source_id else endpoint["read_url_template"].format(
            id=endpoint["id_placeholder"])
    )

    change = patch.get("change")
    return {
        "tool": "hotato",
        "kind": "apply",
        "schema_version": "1",
        "offline": True,
        "mode": "clone",
        "clone_only": True,
        "production_apply_supported": False,
        "refused": False,
        "applies_change": False,
        "created": False,
        "dry_run": True,
        "source_patch": patch_source,
        "source_plan": patch.get("source_plan"),
        "stack": stack,
        "clone": {
            "name": name,
            "based_on_source_id": source_id,
            "source_id_resolved": source_id is not None,
            "name_field": endpoint["name_field"],
            "merge_patch": merge_patch,
            "change": change,
            "read_source": {
                "method": endpoint["read_method"],
                "url": read_url,
                "auth": endpoint["auth"],
            },
            "create": {
                "method": endpoint["create_method"],
                "url": endpoint["create_url"],
                "auth": endpoint["auth"],
                "provenance": endpoint["provenance"],
            },
        },
        "opposite_risk": {
            "battery": battery_dir,
            "fixtures": opposite_risk["n"],
            "has_yield": opposite_risk["has_yield"],
            "has_hold": opposite_risk["has_hold"],
        },
        "verify_command": _verify_command(battery_dir),
        "next": [
            "dry run: nothing was created. Re-run with --yes (and platform "
            "credentials) to create the staging clone and apply the patch to it",
            "then prove the fix on the CLONE, before and after: "
            + _verify_command(battery_dir),
        ],
        "honest": _HONEST,
    }


def _no_clone_target_reason(stack: str) -> str:
    if stack in ("livekit", "pipecat"):
        return (
            f"apply --clone has no platform assistant to clone for {stack}: its "
            "turn-taking config lives in your agent SOURCE, not behind a config "
            "API. Apply the source edit from hotato patch yourself and prove it "
            "with hotato verify."
        )
    if stack == "twilio":
        return (
            "apply --clone has nothing to clone for twilio: it carries the "
            "audio but runs no turn-taking agent. Point at the upstream stack "
            "(vapi or retell) that runs the agent."
        )
    return (
        "apply --clone needs a concrete platform with an assistant/agent to "
        f"clone (one of: {', '.join(CLONE_STACKS)}); the patch stack is "
        f"{stack!r}. Re-plan against your stack, then re-produce the patch."
    )


def _verify_command(battery_dir: Optional[str]) -> str:
    b = battery_dir or "before/"
    return (
        "hotato verify --before before/ --after after/ "
        f"--policy hotato.verify.yaml   # re-capture {b} through the clone into "
        "before/ (source) and after/ (clone)"
    )


def _refusal_result(patch: dict, stack: str, patch_source: Optional[str]) -> dict:
    return {
        "tool": "hotato",
        "kind": "apply",
        "schema_version": "1",
        "offline": True,
        "mode": "clone",
        "clone_only": True,
        "production_apply_supported": False,
        "stack": stack,
        "source_patch": patch_source,
        "source_plan": patch.get("source_plan"),
        "refused": True,
        "config_patchable": False,
        "applies_change": False,
        "created": False,
        "exit_code": REFUSAL_EXIT_CODE,
        "refusal": {
            "headline": REFUSAL_HEADLINE,
            "reason": REFUSAL_REASON,
            "recommended": REFUSAL_RECOMMENDED,
            "lines": list(REFUSAL_LINES),
            "why": (
                "This is the both-axes case: the battery missed a genuine "
                "interruption AND stopped for a backchannel. No single "
                "sensitivity threshold satisfies both, so no config patch is "
                "applied. The refusal is the feature."
            ),
        },
        "honest": _HONEST,
    }


# --- the ONLY networked function (create the staging clone) -----------------

def _http_json(method: str, url: str, *, headers: dict, body: Optional[dict],
               timeout: int) -> dict:
    """The one HTTP primitive apply uses. Only GET (read the source) and POST
    (create the clone) are ever issued; a PUT/PATCH is refused by construction,
    so this code path can never overwrite the source."""
    if method not in ("GET", "POST"):
        raise ValueError(
            "hotato apply only issues GET (read the source) and POST (create a "
            f"new assistant); refusing {method} (it would mutate the source)."
        )
    import urllib.error
    import urllib.request

    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec - user API host
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise ValueError(
            f"HTTP {exc.code} from {method} {url}: {exc.reason}. {detail}".strip()
        ) from exc
    except urllib.error.URLError as exc:  # pragma: no cover - live network path
        raise ValueError(
            f"network error on {method} {url}: {exc.reason}"
        ) from exc
    if not raw.strip():
        return {}
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError(
            f"{method} {url} returned a {type(obj).__name__}, not a JSON object; "
            "the endpoint gave an unexpected shape (a proxy/error page or a "
            "vendor failure)."
        )
    return obj


def create_clone(
    *,
    stack: str,
    source_id: str,
    name: str,
    merge_patch: dict,
    api_key: str,
    timeout: int = 30,
) -> dict:
    """Create the staging clone. THE ONLY networked function in apply, reached
    only from the CLI under ``--yes``.

    It (1) GETs the source config read-only, (2) builds a NEW config = source +
    patch with the new name, and (3) POSTs it to create a NEW assistant. It
    never PUTs/PATCHes the source; the source is only ever read. Returns
    ``{"created", "clone_id", "read_url", "create_url", "stack"}``."""
    if stack not in _CLONE_ENDPOINTS:
        raise ValueError(f"{stack} has no clone endpoint")
    if not source_id:
        raise ValueError(
            "no source assistant/agent id to clone from. The patch's plan did "
            "not resolve one; re-run hotato plan against the live assistant so "
            "the id is recorded, then re-produce the patch."
        )
    endpoint = _CLONE_ENDPOINTS[stack]
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    read_url = endpoint["read_url_template"].format(id=source_id)
    # (1) read the source, read-only.
    source_config = _http_json(
        endpoint["read_method"], read_url, headers=headers, body=None,
        timeout=timeout,
    )
    # (2) build the clone config off a COPY; the source object is never mutated.
    clone_config = build_clone_config(
        source_config, stack=stack, name=name, merge_patch=merge_patch)
    # (3) create a NEW assistant. POST, never PUT/PATCH -> the source is safe.
    created = _http_json(
        endpoint["create_method"], endpoint["create_url"], headers=headers,
        body=clone_config, timeout=timeout,
    )
    clone_id = created.get(endpoint["id_result_key"]) or created.get("id")
    return {
        "created": True,
        "clone_id": clone_id,
        "read_url": read_url,
        "create_url": endpoint["create_url"],
        "stack": stack,
    }


# --- text rendering ---------------------------------------------------------

def render_refusal_text(result: dict) -> str:
    """The canon refusal, verbatim, plus the honest one-liner. The three canon
    lines are printed exactly as specified."""
    lines = list(result["refusal"]["lines"])
    lines.append("")
    lines.append(f"  {result['refusal']['why']}")
    lines.append(f"  {result['honest']}")
    return "\n".join(lines)


def render_text(result: dict) -> str:
    if result.get("refused"):
        return render_refusal_text(result)
    c = result["clone"]
    stack = result["stack"]
    source = c["based_on_source_id"] or "<source id unresolved>"
    noun = "agent" if stack == "retell" else "assistant"
    created = result.get("created")
    lines = [
        f"hotato apply [{stack}] clone name={c['name']!r}  "
        "(staging clone only; the source is never mutated)",
    ]
    if created:
        lines.append(
            f"  CREATED staging {noun} {result.get('clone_id')!r} by "
            f"{c['create']['method']} {c['create']['url']}, cloned from "
            f"{source} with the patch applied. The source was only read, never "
            "changed."
        )
    else:
        lines.append(
            f"  would create: {c['create']['method']} {c['create']['url']}  "
            f"a NEW {noun} named {c['name']!r}, cloned from {source}"
        )
        lines.append(
            f"  would read source (read-only): {c['read_source']['method']} "
            f"{c['read_source']['url']}"
        )
    lines.append(
        "  patch it applies to the NEW "
        f"{noun}: {_errors.safe_json_dumps(c['merge_patch'], sort_keys=True)}"
    )
    ch = c.get("change")
    if ch:
        frm = "?" if ch.get("from") is None else ch["from"]
        to = "?" if ch.get("to") is None else ch["to"]
        lines.append(
            f"  change: {ch['field']}  {frm} -> {to}  ({ch['direction']})")
    orr = result["opposite_risk"]
    lines.append(
        f"  opposite-risk battery: {orr['fixtures']} fixture(s) at "
        f"{orr['battery']!r} (yield: {str(orr['has_yield']).lower()}, hold: "
        f"{str(orr['has_hold']).lower()})"
    )
    if not created:
        lines.append(
            "  dry run: nothing was created. Re-run with --yes and credentials "
            "to create the staging clone.")
    lines.append(f"  next: prove the fix on the clone: {result['verify_command']}")
    lines.append(f"  {result['honest']}")
    return "\n".join(lines)
