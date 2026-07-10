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
| `improved` | the verify claim is supported (>= `--min-n` previously-failing fixtures), at least one now passes, NOTHING regressed anywhere in the battery (including the hold/opposite-risk axis), no contract regressed, `--policy` (if given) passed, every before target and hold has an after counterpart, AND every guarded fixture (target and hold) carries VERIFIABLE before/after audio identity (well-formed, freshly distinct decoded PCM, recomputed from the audio on disk -- the provenance guard, below) | `0` |
| `regressed` | any fixture regressed, a contract regressed, or the policy failed | `1` |
| `inconclusive` | too few previously-failing fixtures to characterize, nothing that used to fail now passes, OR the audio identity is present-but-unverifiable for a guarded fixture (a malformed provenance block, a missing block, or a well-formed assertion hotato could not recompute at trial time because the audio was not present) | `1` |
| `refused` | the patch is the both-axes threshold funnel (apply's refusal-first gate fires before any before/after evidence is even read); OR the after set drops a required before fixture (an incomplete, cherry-picked comparison); OR a guarded fixture's recorded provenance does not match the audio on disk; OR a guarded fixture's before/after audio is the SAME conversation (identical decoded PCM -- a re-score, not a recapture) | `3` |

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

Every run envelope records an `audio_provenance` block per event: a streamed
sha256 of the raw file bytes AND a streamed sha256 of the decoded PCM samples
(plus sample rate and frame count), computed at capture time by `hotato run`
/ `hotato capture`. `hotato fix trial` does not trust the string; it VERIFIES
the identity for every GUARDED fixture -- the fail->pass targets AND the
still-passing holds (a frozen hold is a re-score too, so holds get the same
guard). This is an offline tool: a user who controls every input can always
lie to themselves. The guard's job is narrower and honest -- make the
motivated failure modes impossible or loud, recompute what can be recomputed
from the actual files, and state exactly what was and was NOT verified:

| Situation | What it is | Effect on verdict |
| --- | --- | --- |
| Well-formed, freshly distinct (decoded PCM) identity, recomputed from the audio present on disk and matching | a verified fresh recapture | proceeds -- eligible for `improved` |
| Identical decoded PCM before vs. after on any guarded fixture | the after run re-scored the SAME conversation (a header-only edit or trailing-byte append cannot hide it -- the comparison is on samples, not container bytes) | `refused` (exit `3`) |
| Recorded digest does NOT match the audio present on disk | the provenance was hand-edited or the audio was swapped after capture | `refused` (exit `3`) |
| A required before fixture (target or hold) missing from the after set | a cherry-picked, incomplete comparison | `refused` (exit `3`) |
| Malformed block (non-hex digest, absurd sample rate / frame count, or a top-level digest inconsistent with the per-side digests) | an unvalidated assertion, not a distinct recording | `inconclusive` (exit `1`) |
| A provenance block missing on either side | an older envelope, or one hand-built without `audio_provenance` -- identity is UNKNOWN | `inconclusive` (exit `1`) |
| Well-formed identity that hotato could NOT recompute (the audio was not present) | asserted, not proven | `inconclusive` (exit `1`) |

A provenance-guard refusal is NOT the apply-gate refusal: it fires AFTER
`verify` / `contract verify` / `explain` have already run, so (unlike the
both-axes refusal, which reads no evidence at all) the full report --
verify's proof, the contract rollup, the provenance identities, the
attribution -- still renders below the refusal banner. Every refusal path
exits the SAME code `3`; `refusal_kind` in the JSON output
(`"threshold_funnel"`, `"incomplete_after"`, `"recompute_mismatch"`,
`"same_audio_recapture"`) tells them apart for a script that wants to.

```
No fix will be certified from re-scored audio
Reason: 1 fixture(s) this verdict rests on (f1) have identical before/after decoded PCM: the after run re-scored the SAME conversation the before run scored, just against a different threshold or scorer config
Recommended: recapture the fixture(s) through the applied clone (hotato apply --clone --yes) and re-run hotato fix trial against the new after evidence
```

Every rendered report (text, `--format json`, `--html`) surfaces the short
digest and the verified status for every guarded fixture, before and after --
so a reader never has to take "fresh capture" on faith, and can see exactly
what was verified versus merely asserted. The effective `--min-n` is echoed
in every surface too, so a lowered floor is always visible. The report's own
conclusion states plainly what a passing check proves: that the fresh take
passed the same human-labeled contract, not that the change caused it (hotato
reports coincidence, never causation, throughout).

The text and HTML renders of this section also print, verbatim, wherever it
appears (an `improved` verdict, or a `refused`/`inconclusive` one the guard
itself downgraded): *"Provenance caution: this proves the specific fresh
capture scored above, at the revision it was captured from. It does not
certify a later deploy or every future call, and it does not re-run itself;
recapture again after the next change."* See
[`docs/RECAPTURE.md`](RECAPTURE.md#claim-language-what-each-kind-of-evidence-lets-you-honestly-say)
for the fuller claim-language table this line is drawn from.

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
