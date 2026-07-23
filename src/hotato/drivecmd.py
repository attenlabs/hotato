"""``hotato drive``: re-run the LIVE agent to get a red regression gate to green.

``hotato contract verify`` re-scores the SAME frozen recording a contract was
created from. That audio never changes, so a pinned bad call stays red forever:
the frozen recording can never yield. ``hotato drive`` is the OTHER lane (the
one docs/RECAPTURE.md documents as a manual task): it originates ONE fresh call
against the CURRENT agent using the caller stimulus, captures the new recording,
and scores THAT against the contract's declared invariant (``--expect
yield``/``hold`` and its policy thresholds). A pass is the fresh evidence a
recaptured contract needs to go green. It verifies the declared invariant on the
new call; it never asserts the fresh conversation is identical to the frozen one.

Two verbs, two claims: ``contract verify`` re-scores STORED evidence (the CI
gate, zero variance, never dials); ``drive`` produces FRESH evidence from the
live agent. This module composes the shipped primitives and adds no new scoring
engine and no new network path:

* the credential + egress gate and origination in :mod:`hotato.fleet.adapters`
  (:meth:`Adapter.run_scenario`), which itself calls
  :func:`hotato.drive.place_call_vapi` / :func:`hotato.drive.place_call_twilio`;
* the SAME scorer :func:`hotato.core.run_single` that ``contract verify`` and
  ``hotato run`` use, to check the invariant against the fresh recording;
* :func:`hotato.contract.inspect_contract` to read the declared invariant off a
  ``.hotato`` bundle, and :func:`hotato.scenario.load_scenario_file` to read a
  scripted caller stimulus.

Scope is stated plainly: hotato originates a real call only for the two stacks
its origination path covers, ``vapi`` and ``twilio``. For any other stack, drive
prints the manual recapture path (docs/RECAPTURE.md) instead of pretending to
dial. Placing a call reaches the provider and costs a real, billable call, so it
is triple-gated and never fires by import or by accident: credentials, an
explicit ``--yes`` egress opt-in, and an explicit drive target must ALL be
present before any dial. It never substitutes hotato's own transcription for the
provider's original STT output either; the fresh recording is scored on timing,
the same as every other recording.
"""

from __future__ import annotations

import os
import shlex
import shutil
from typing import Optional

from . import contract as _contract
from . import scenario as _scenario
from .core import run_single
from .fleet import adapters as _adapters
from .fleet.adapters import CapabilityError

__all__ = [
    "DRIVE_STACKS",
    "run_drive",
    "render_drive_text",
    "drive_result_json",
]

# The only stacks hotato.drive can ORIGINATE a call against today. Any other
# stack routes to the manual recapture path (docs/RECAPTURE.md), never a faked
# origination.
DRIVE_STACKS = ("vapi", "twilio")
RECAPTURE_DOC = "docs/RECAPTURE.md"

_TRUTHY = ("1", "true", "yes", "on")
_OPT_IN_ENV = "HOTATO_DRIVE_OPT_IN"


def _env_opt_in() -> bool:
    return os.environ.get(_OPT_IN_ENV, "").strip().lower() in _TRUTHY


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _fmt_sec(v) -> str:
    return f"{v:.2f}" if _is_num(v) else "n/a"


def _put(d: dict, key: str, value) -> None:
    if value not in (None, ""):
        d[key] = value


def _mkparents(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _looks_like_bundle(path: str) -> bool:
    if os.path.isdir(path):
        return os.path.isfile(os.path.join(path, "contract.json"))
    return os.path.basename(path) == "contract.json"


# --- resolve the declared invariant + caller stimulus from the input --------

def _load_case(input_path, *, stack, expect, max_talk_over_sec,
               max_time_to_yield_sec, scenario_path) -> dict:
    """Resolve ``input`` into the drive case: the declared invariant (expect +
    policy thresholds), the target stack, the caller-stimulus scenario (or
    None), the id to base the recapture on, and where a recaptured contract
    would be written. Raises ``ValueError``/``OSError`` (CLI exit 2) for
    unusable input, the SAME error family every other hotato usage error uses.
    """
    if not input_path:
        raise ValueError(
            "hotato drive needs an input: a .hotato contract bundle (the red "
            "gate), or a hotato.scenario.v1 caller-stimulus file.")

    if _looks_like_bundle(input_path):
        contract = _contract.inspect_contract(input_path)
        label = contract.get("label") or {}
        source = contract.get("source") or {}
        policy = (contract.get("policy") or {}).get("pass_conditions") or {}
        bundle_dir = (input_path if os.path.isdir(input_path)
                      else os.path.dirname(os.path.abspath(input_path)) or ".")
        return {
            "id": contract.get("id") or "recapture",
            "stack": (stack or source.get("stack") or "").lower(),
            "expect": expect or label.get("expected_behavior") or "yield",
            "max_talk_over_sec": (
                max_talk_over_sec if max_talk_over_sec is not None
                else policy.get("max_talk_over_sec")),
            "max_time_to_yield_sec": (
                max_time_to_yield_sec if max_time_to_yield_sec is not None
                else policy.get("max_time_to_yield_sec")),
            "scenario": (_scenario.load_scenario_file(scenario_path)
                         if scenario_path else None),
            "contracts_out_dir": os.path.dirname(os.path.normpath(bundle_dir)) or ".",
            "source_kind": "contract-bundle",
        }

    # A scenario.v1 caller-stimulus file: the input IS the caller stimulus.
    scenario_doc = _scenario.load_scenario_file(input_path)
    if not stack:
        raise ValueError(
            "which stack should drive originate against? A scenario file does "
            "not name a provider. Pass --stack vapi or --stack twilio.")
    return {
        "id": scenario_doc.get("id") or "recapture",
        "stack": stack.lower(),
        "expect": expect or "yield",
        "max_talk_over_sec": max_talk_over_sec,
        "max_time_to_yield_sec": max_time_to_yield_sec,
        "scenario": scenario_doc,
        "contracts_out_dir": "contracts",
        "source_kind": "scenario",
    }


# --- origination through the shipped credential + egress gate ----------------

def _originate(case, *, assistant, api_key, account_sid, auth_token,
               to_number, from_number, phone_number_id, customer_number,
               base_url, poll_interval, max_wait) -> dict:
    """Build the provider adapter + its ``run_scenario`` argument and originate.

    The adapter enforces two gates the CLI does not duplicate here: credentials
    and a live drive target (``to``/``from`` for twilio, phone-number-id +
    customer for vapi). Both fire BEFORE any dial; a refusal raises
    ``CapabilityError``, re-raised here as the clean ``ValueError`` (CLI exit 2,
    no call placed) every hotato usage error uses, never an uncaught runtime
    error. ``egress_opt_in`` is set only because :func:`run_drive` already
    proved the human opt-in before calling this.
    """
    stack = case["stack"]
    if stack == "vapi":
        adapter = _adapters.get_adapter(
            "vapi", api_key=api_key or os.environ.get("VAPI_API_KEY"))
        scenario_arg = {"egress_opt_in": True}
        _put(scenario_arg, "phone_number_id", phone_number_id)
        _put(scenario_arg, "customer_number", customer_number)
        clone_ref = assistant
    else:  # twilio
        adapter = _adapters.get_adapter(
            "twilio",
            account_sid=account_sid or os.environ.get("TWILIO_ACCOUNT_SID"),
            auth_token=auth_token or os.environ.get("TWILIO_AUTH_TOKEN"))
        # The scenario.v1 caller stimulus IS the twilio caller (rendered to
        # TwiML). Copy it and merge the drive params so origination stays a
        # pure read of the loaded stimulus, never a mutation of it.
        scenario_arg = dict(case["scenario"])
        scenario_arg["egress_opt_in"] = True
        _put(scenario_arg, "to_number", to_number)
        _put(scenario_arg, "from_number", from_number)
        clone_ref = None
    _put(scenario_arg, "base_url", base_url)
    _put(scenario_arg, "poll_interval", poll_interval)
    _put(scenario_arg, "max_wait", max_wait)
    try:
        return adapter.run_scenario(clone_ref, scenario_arg)
    except CapabilityError as exc:
        raise ValueError(str(exc)) from exc


def _recapture_command(*, recording, expect, onset, recap_id, contracts_out,
                       stack, max_talk_over_sec, max_time_to_yield_sec) -> str:
    parts = ["hotato contract create", f"--stereo {shlex.quote(recording)}"]
    if _is_num(onset) and onset >= 0:
        parts.append(f"--onset {onset:.2f}")
    parts.append(f"--expect {expect}")
    parts.append(f"--id {shlex.quote(recap_id)}")
    parts.append(f"--out {shlex.quote(contracts_out)}")
    if _is_num(max_talk_over_sec):
        parts.append(f"--max-talk-over {max_talk_over_sec}")
    if _is_num(max_time_to_yield_sec):
        parts.append(f"--max-time-to-yield {max_time_to_yield_sec}")
    if stack:
        parts.append(f"--stack {stack}")
    return " ".join(parts)


def run_drive(
    input_path,
    *,
    stack: Optional[str] = None,
    expect: Optional[str] = None,
    max_talk_over_sec: Optional[float] = None,
    max_time_to_yield_sec: Optional[float] = None,
    scenario_path: Optional[str] = None,
    assistant: Optional[str] = None,
    api_key: Optional[str] = None,
    account_sid: Optional[str] = None,
    auth_token: Optional[str] = None,
    to_number: Optional[str] = None,
    from_number: Optional[str] = None,
    phone_number_id: Optional[str] = None,
    customer_number: Optional[str] = None,
    base_url: Optional[str] = None,
    poll_interval: Optional[float] = None,
    max_wait: Optional[float] = None,
    out_path: Optional[str] = None,
    yes: bool = False,
    contracts_out_dir: Optional[str] = None,
) -> dict:
    """Originate ONE fresh call against the live agent and score it against the
    declared invariant. Returns a result dict carrying ``outcome`` (``pass`` /
    ``fail`` / ``not_scorable`` / ``unsupported_stack``) and ``exit_code``:
    0 pass, 1 a scored invariant FAIL (fix the agent), 2 a fresh call with no
    scorable moment (unusable fresh evidence -- still red, re-drive the call).
    Raises ``ValueError``/``OSError`` (CLI exit 2) for a usage error, missing
    credentials, a missing egress opt-in, or a missing drive target; no call is
    placed on any of those paths.
    """
    case = _load_case(
        input_path, stack=stack, expect=expect,
        max_talk_over_sec=max_talk_over_sec,
        max_time_to_yield_sec=max_time_to_yield_sec,
        scenario_path=scenario_path,
    )
    case_stack = case["stack"]
    expect = case["expect"]
    if str(expect).strip().lower() not in ("yield", "hold"):
        raise ValueError(f"--expect must be 'yield' or 'hold', got {expect!r}")

    base = {
        "tool": "hotato",
        "kind": "drive",
        "schema_version": "1",
        "id": case["id"],
        "stack": case_stack,
        "expect": expect,
        "source_kind": case["source_kind"],
    }

    # HARD scope: hotato originates a live call only for vapi/twilio. Every
    # other stack gets the manual recapture path, never a faked dial. Checked
    # before the opt-in / adapter, so nothing here touches credentials or the
    # network for an unsupported stack.
    if case_stack not in DRIVE_STACKS:
        return {
            **base,
            "outcome": "unsupported_stack",
            "exit_code": 2,
            "message": (
                f"drive is not available for stack {case_stack or '(unknown)'!r}. "
                f"hotato originates a live call for {' and '.join(DRIVE_STACKS)} "
                "only. For this stack, recapture the caller stimulus against the "
                f"current agent yourself and score it: see {RECAPTURE_DOC}."
            ),
        }

    # Egress gate (of three): an explicit human opt-in. The credential + drive-
    # target gates live in the adapter and fire before any dial; this one gives
    # the CLI a clear, actionable refusal and never reaches the adapter without
    # it.
    if not (yes or _env_opt_in()):
        raise ValueError(
            "hotato drive originates a REAL, billable call against your live "
            "agent. Pass --yes to authorize placing it (or set "
            f"{_OPT_IN_ENV}=1). No call is placed without it.")

    if case_stack == "vapi" and not assistant:
        raise ValueError(
            "vapi drive needs --assistant ID: the assistant (typically a "
            "staging clone) to originate the fresh call from.")
    if case_stack == "twilio" and case["scenario"] is None:
        raise ValueError(
            "twilio drive needs a scripted caller stimulus (a hotato.scenario.v1 "
            "with a caller.script). A .hotato bundle stores timing evidence, not "
            "a caller script, so pass --scenario FILE, or drive a scenario file "
            f"directly. See {RECAPTURE_DOC} for the manual recapture path.")

    result = _originate(
        case, assistant=assistant, api_key=api_key, account_sid=account_sid,
        auth_token=auth_token, to_number=to_number, from_number=from_number,
        phone_number_id=phone_number_id, customer_number=customer_number,
        base_url=base_url, poll_interval=poll_interval, max_wait=max_wait,
    )

    recording = result["recording"]
    if out_path:
        _mkparents(out_path)
        shutil.copyfile(recording, out_path)
        recording = out_path

    # Score the FRESH recording against the declared invariant with the SAME
    # scorer contract verify and hotato run use. This checks the invariant, not
    # conversational identity: the onset is auto-detected on the new call.
    env = run_single(
        stereo=recording, expect=expect, stack=case_stack,
        max_talk_over_sec=case["max_talk_over_sec"],
        max_time_to_yield_sec=case["max_time_to_yield_sec"],
    )
    event = env["events"][0]
    scorable = event.get("scorable") is not False
    verdict = event.get("verdict") or {}
    measurements = event.get("measurements") or {}
    passed = scorable and bool(verdict.get("passed"))
    onset = measurements.get("caller_onset_sec")

    contracts_out = contracts_out_dir or case["contracts_out_dir"]
    recap_id = f"{case['id']}-recapture"

    out = {
        **base,
        "provider": result.get("provider"),
        "provider_call_id": result.get("provider_call_id"),
        "call_status": result.get("status"),
        "recording": recording,
        "max_talk_over_sec": case["max_talk_over_sec"],
        "max_time_to_yield_sec": case["max_time_to_yield_sec"],
        "scorable": scorable,
        "not_scorable_reason": event.get("not_scorable_reason"),
        "measurement": {
            "did_yield": verdict.get("did_yield") if scorable else None,
            "seconds_to_yield": verdict.get("seconds_to_yield") if scorable else None,
            "talk_over_sec": verdict.get("talk_over_sec") if scorable else None,
            "caller_onset_sec": onset,
        },
        "recapture_id": recap_id,
        "contracts_out_dir": contracts_out,
    }
    if not scorable:
        # The fresh call produced no scorable moment: unusable fresh evidence,
        # the CLI's exit-2 refuse convention -- still red (never a pass), and
        # distinct from a scored invariant FAIL's exit 1 so exit-code-only CI
        # can separate "re-drive the call" from "fix the agent".
        out["outcome"] = "not_scorable"
        out["exit_code"] = 2
        out["next"] = None
    elif passed:
        out["outcome"] = "pass"
        out["exit_code"] = 0
        out["next"] = _recapture_command(
            recording=recording, expect=expect, onset=onset,
            recap_id=recap_id, contracts_out=contracts_out, stack=case_stack,
            max_talk_over_sec=case["max_talk_over_sec"],
            max_time_to_yield_sec=case["max_time_to_yield_sec"],
        )
    else:
        out["outcome"] = "fail"
        out["exit_code"] = 1
        out["next"] = None
    return out


# --- rendering ---------------------------------------------------------------

_MARK = {"pass": "PASS", "fail": "FAIL", "not_scorable": "NOT SCORABLE"}


def render_drive_text(t: dict) -> str:
    if t["outcome"] == "unsupported_stack":
        return "hotato drive: " + t["message"]

    verb = "yield" if t["expect"] == "yield" else "hold"
    call = f"{t.get('provider') or t['stack']} call {t.get('provider_call_id') or '?'}"
    lines = [f"hotato drive: {_MARK[t['outcome']]}  ({call})"]

    inv = f"expect {t['expect']}"
    bounds = []
    if t.get("max_talk_over_sec") is not None:
        bounds.append(f"max_talk_over={t['max_talk_over_sec']}s")
    if t.get("max_time_to_yield_sec") is not None:
        bounds.append(f"max_time_to_yield={t['max_time_to_yield_sec']}s")
    if bounds:
        inv += " (" + ", ".join(bounds) + ")"
    lines.append(f"  invariant: {inv}")

    m = t.get("measurement") or {}
    if t["outcome"] == "not_scorable":
        lines.append(
            "  the fresh call produced no scorable moment: "
            f"{t.get('not_scorable_reason')}. This is not a pass; the gate "
            "stays red.")
    else:
        lines.append(
            f"  measured:  did_yield={m.get('did_yield')} "
            f"seconds_to_yield={_fmt_sec(m.get('seconds_to_yield'))} "
            f"talk_over={_fmt_sec(m.get('talk_over_sec'))}")
    lines.append(f"  fresh recording: {t.get('recording')}")

    if t["outcome"] == "pass":
        lines.append(
            "  the live agent met the invariant on this fresh call. This is "
            "the fresh evidence a recaptured contract needs to go green.")
        lines.append("next: commit the recaptured green contract")
        lines.append(f"  {t['next']}")
    elif t["outcome"] == "fail":
        lines.append(
            "  the live agent still violates the invariant on this fresh "
            f"call. The gate stays red until it does {verb}.")
        lines.append(
            f"next: the current agent still does not {verb}. Investigate it, "
            "then drive again.")
    else:  # not_scorable
        lines.append(
            "next: a scorable fresh call is needed before the gate can go "
            "green. Recapture again.")
    return "\n".join(lines)


def drive_result_json(t: dict) -> dict:
    return t
