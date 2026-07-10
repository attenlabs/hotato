# fix trial: one before/after proof, composed, fail-closed

`hotato fix trial` is the last rung of the fix ladder: it composes the
already-shipped, already-guarded primitives into ONE before/after report that
says whether a candidate change actually holds. No new scoring engine, no new
networked path:

* **`hotato apply`'s exact offline gate** (`build_apply`, `clone=True`):
  refusal-first on the both-axes threshold funnel, opposite-risk-battery-
  required, clone-only. fix trial never creates a clone itself and never
  touches the network -- it never calls `apply.create_clone` /
  `apply._http_json`, so it carries the same clone-only, production-
  unmutatable guarantee apply's own dry run gives, by construction.
* **`hotato verify`'s battery-scale rollup**: the BEFORE run (the original
  failure evidence) against the AFTER run (re-captured through the clone you
  created separately with `hotato apply --clone --yes`), scoring EVERY paired
  fixture, not just the target failure -- the "neighbouring cases" check.
* **`hotato contract verify`**, when `--contracts DIR` is given: another
  neighbouring-cases check, on real labelled moments outside the battery.
* **`hotato explain`**, folded in as the report's attribution section: root
  cause of the ORIGINAL failure, reused exactly.

```
hotato patch fixplan.json --format json --out patch.json
hotato apply patch.json --clone --name staging-refund-fix --battery tests/hotato
# ... re-capture the battery through the source (before/) and the clone (after/) ...
hotato fix trial patch.json --name staging-refund-fix \
    --before before/ --after after/ --battery tests/hotato \
    --policy hotato.verify.yaml --out fix-trial.json --html fix-trial.html
```

## The verdict is fail-closed, never a soft pass

| Verdict | When | Exit |
| --- | --- | --- |
| `improved` | the verify claim is supported (>= `--min-n` previously-failing fixtures), at least one now passes, NOTHING regressed anywhere in the battery (including the hold/opposite-risk axis), no contract regressed, `--policy` (if given) passed, AND every fixture the claim rests on carries distinct, known before/after audio identity (the fresh-capture provenance guard, below) | `0` |
| `regressed` | any fixture regressed, a contract regressed, or the policy failed | `1` |
| `inconclusive` | too few previously-failing fixtures to characterize, nothing that used to fail now passes, OR audio identity is unknown on either side for a fixture the claim rests on | `1` |
| `refused` | EITHER the patch is the both-axes threshold funnel (apply's refusal-first gate fires before any before/after evidence is even read), OR every other bar clears but a fixture the claim rests on has byte-identical before/after audio (the after run re-scored the SAME recording, not a fresh capture) | `3` |

**`inconclusive` is fail-closed, not a pass.** A low-n battery or a
zero-improvement battery exits the SAME non-zero code as a real regression,
so CI never treats "we could not tell" as green. A hold fixture that flips
from passing to failing (the opposite-risk axis) is `compare.classify_pair`'s
`"regressed"` result whichever axis it is on, so it is caught by the same
check that catches a talk-over regression -- there is no separate,
weaker gate for the opposite-risk axis.

## Refusal: correct output, not an error

If the patch is `do_not_tune_single_threshold` (the battery missed a real
interruption AND false-stopped on a backchannel in the same battery), fix
trial refuses before reading `--before` / `--after` / `--contracts` at all,
prints the exact canon recommendation, and exits `3` -- the SAME distinct
code `hotato apply` uses for the same refusal, so a script that already
branches on apply's refusal code recognizes fix trial's too.

```
No config patch will be applied
Reason: both missed real interruption and false stop on backchannel, one threshold cannot safely fix both
Recommended: enable or add engagement-control / backchannel-aware turn detection
```

## Fresh-capture provenance guard: a re-score is never a fix

`hotato apply`'s clone-only gate and `hotato verify`'s battery-scale rollup
answer "did the numbers move." Neither one asks whether the AFTER evidence
was actually RE-CAPTURED, or is just the SAME recording the BEFORE run
scored, re-scored under a looser threshold. That gap is exploitable: run the
same fixture twice with different `--max-time-to-yield` / `--max-talk-over`
bounds, and you get a genuine-looking "improved" verdict with no code,
config, or model change behind it at all.

Every run envelope now records an `audio_provenance` block per event: a
streamed sha256 of the exact audio bytes that were scored (plus sample rate
and frame count), computed at capture time by `hotato run` / `hotato
capture`. `hotato fix trial` compares this identity, before vs. after, for
every fixture the `improved` claim rests on (previously failing, now
passing -- exactly the fixtures composing verify's "N of M" headline):

| Provenance | Meaning | Effect on verdict |
| --- | --- | --- |
| Distinct, known digests on every target fixture | a real recapture happened | none -- proceeds exactly as before |
| Identical digest on any target fixture | the after run re-scored the SAME recording as the before run | downgraded to `refused` (exit `3`), never a soft pass |
| A digest missing on either side of any target fixture | an older envelope, or one hand-built without `audio_provenance` -- identity is UNKNOWN | downgraded to `inconclusive` (exit `1`), never assumed fresh |

A same-audio refusal is NOT the apply-gate refusal: it fires AFTER
`verify` / `contract verify` / `explain` have already run, so (unlike the
both-axes refusal, which reads no evidence at all) the full report --
verify's proof, the contract rollup, the provenance digests, the
attribution -- still renders below the refusal banner. Both refusal paths
exit the SAME code `3`; `refusal_kind` in the JSON output
(`"threshold_funnel"` vs. `"same_audio_recapture"`) tells them apart for a
script that wants to.

```
No fix will be certified from re-scored audio
Reason: 1 fixture(s) this claim rests on (f1) have byte-identical before/after audio (same sha256): the after run re-scored the SAME recording the before run scored, just against a different threshold or scorer config
Recommended: recapture the fixture(s) through the applied clone (hotato apply --clone --yes) and re-run hotato fix trial against the new after evidence
```

Every rendered report (text, `--format json`, `--html`) surfaces the short
digest for every target fixture, before and after, and whether they match --
so a reader never has to take "fresh capture" on faith. The report's own
conclusion states plainly what a passing digest check proves: that the fresh
take passed the same human-labeled contract, not that the change caused it
(hotato reports coincidence, never causation, throughout).

## Flags

| Flag | Meaning |
| --- | --- |
| `PATCH_JSON` | a `hotato patch` artifact |
| `--name NAME` | name of the staging clone this trial is proving (required for a non-refused patch; the same `--name` `hotato apply` takes -- fix trial evaluates the SAME clone-only gate, it never creates a clone itself) |
| `--before RUN.json\|DIR` | the OLD run envelope(s): the original failure evidence. Also the default opposite-risk `--battery`, and the attribution source, when those are omitted |
| `--after RUN.json\|DIR` | the NEW run envelope(s), re-captured through the staging clone |
| `--battery DIR` | the opposite-risk battery apply's gate checks (BOTH a yield and a hold fixture); defaults to `--before` |
| `--contracts DIR` | also re-verify a directory of hotato contracts; any contract regression fails the trial |
| `--policy hotato.verify.yaml` | gate verify's rollup (see `docs/FIX-LOOP.md`); a violation fails the trial |
| `--min-n N` | minimum previously-failing fixtures needed to support the claim (default 3) |
| `--out PATH` | also write the full proof JSON |
| `--html PATH` | also write a self-contained before/after HTML report |

## Output

* **text** (default): the verdict, verify's own rendered proof, the contract
  verify rollup when `--contracts` was given, and the attribution section
  (one `hotato explain` render per file under `--before`).
* **`--format json`**: the full machine shape, schema `hotato.fix_trial.v1`.
  Every sub-result is the REAL nested result the underlying command already
  produces (`apply`, `verify`, `contract_verify`, `attribution`) -- nothing
  here is a re-derived summary.
* **`--html PATH`**: a self-contained report, reusing the same house style
  the other HTML reports use: the verdict chip, the verify proof table, the
  contract-verify rollup, and the attribution cards.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | `improved` |
| 1 | fail-closed: `regressed` or `inconclusive` |
| 2 | usage error: the same gates `hotato apply` enforces (no `--name`, no opposite-risk battery, a stack with no clone target, a patch with no concrete change) or `hotato verify` / `contract verify` already enforce (no fixtures pair, an invalid `--policy`, a `--contracts` dir with no contracts, unreadable input) |
| 3 | principled refusal: the both-axes threshold funnel. Shared with `hotato apply`'s refusal code |

## What this is not

fix trial does not create, patch, or delete anything on your platform; it
never calls `apply.create_clone`. It does not prove authorization, identity,
compliance, or policy safety. It reports coincidence, never causation --
every underlying `verify` and `contract verify` number keeps that rule. See
[`FIX-LOOP.md`](FIX-LOOP.md) for the manual steps this composes, and
[`APPLY.md`](APPLY.md) / [`EXPLAIN.md`](EXPLAIN.md) for the primitives
themselves.
