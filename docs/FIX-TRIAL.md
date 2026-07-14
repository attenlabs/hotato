# fix trial: one before/after proof, composed, fail-closed

`hotato fix trial` is the fix ladder's last rung: it composes already-shipped,
already-guarded primitives into ONE before/after report that says whether a
change holds. No new scoring engine, no new networked path:

* **`hotato apply`'s offline gate** (`build_apply`, `clone=True`):
  refusal-first on the both-axes threshold funnel, requires an
  opposite-risk battery, clone-only. fix trial never calls
  `apply.create_clone` or `apply._http_json` -- no clone, no network call --
  so it carries apply's own clone-only, production-unmutatable guarantee by
  construction.
* **`hotato verify`'s battery-scale rollup**: scores the BEFORE run (the
  original failure evidence) against the AFTER run (re-captured through the
  clone you made with `hotato apply --clone --yes`) -- every paired
  fixture, not just the target failure. The "neighbouring cases" check.
* **`hotato contract verify`**, when `--contracts DIR` is given: another
  neighbouring-cases check, against labelled moments outside the battery.
* **`hotato explain`**, folded in as the report's attribution section: root
  cause of the original failure, reused exactly.

```
hotato patch fixplan.json --format json --out patch.json
hotato apply patch.json --clone --name staging-refund-fix --battery tests/hotato
# ... re-capture the battery through the source (before/) and the clone (after/) ...
hotato fix trial patch.json --name staging-refund-fix \
    --before before/ --after after/ --battery tests/hotato \
    --policy hotato.verify.yaml --out fix-trial.json --html fix-trial.html
```

## The verdict is fail-closed, never a soft pass

| Verdict | Exit | Fires when |
|---|---|---|
| `improved` | `0` | verify's claim is supported (>= `--min-n` previously-failing fixtures), at least one now passes, nothing regressed anywhere (including the hold/opposite-risk axis), no contract regressed, `--policy` passed if given, every before target/hold has an after counterpart, and every guarded fixture carries verifiable before/after audio identity (the provenance guard, below) |
| `regressed` | `1` | any fixture regressed, a contract regressed, or the policy failed |
| `inconclusive` | `1` | too few previously-failing fixtures to characterize, nothing that used to fail now passes, or a guarded fixture's audio identity is present but unverifiable (malformed/missing provenance, or well-formed but not recomputable because the audio was missing at trial time) |
| `refused` | `3` | the patch is the both-axes threshold funnel (apply's refusal-first gate fires before any before/after evidence is read); or the after set drops a required before fixture; or a guarded fixture's provenance doesn't match the audio on disk; or a guarded fixture's before/after audio is the SAME conversation (identical decoded PCM -- a re-score, not a recapture) |

**`inconclusive` is fail-closed, not a pass.** A low-n or zero-improvement
battery exits the same non-zero code as a regression, so CI never treats
"we could not tell" as green. A hold fixture that flips from passing to
failing is `compare.classify_pair`'s `"regressed"` result whichever axis
it's on -- there is no separate, weaker gate for the opposite-risk axis.

## Refusal is a correct output, not an error

If the patch is `do_not_tune_single_threshold` (the battery missed a real
interruption AND false-stopped on a backchannel), fix trial refuses before
reading `--before` / `--after` / `--contracts` at all, prints the canon
recommendation, and exits `3` -- the same code `hotato apply` uses for the
same refusal, so a script that already branches on apply's refusal code
recognizes fix trial's too.

```
No config patch will be applied
Reason: both missed real interruption and false stop on backchannel, one threshold cannot safely fix both
Recommended: enable or add engagement-control / backchannel-aware turn detection
```

## Fresh-capture provenance guard: a re-score is never a fix

`hotato apply`'s clone-only gate and `hotato verify`'s battery-scale rollup
answer "did the numbers move." Neither asks whether the AFTER evidence was
re-captured, or is the SAME recording the BEFORE run scored, just re-scored
under a looser threshold. That gap is exploitable: run the same fixture
twice with different `--max-time-to-yield` / `--max-talk-over` bounds and
you get a convincing "improved" verdict with no code, config, or model
change behind it.

Every run envelope records an `audio_provenance` block per event: a
streamed sha256 of the raw file bytes and of the decoded PCM samples (plus
sample rate and frame count), computed at capture time by `hotato run` /
`hotato capture`. `fix trial` does not trust the string -- it VERIFIES the
identity for every GUARDED fixture: the fail->pass targets AND the
still-passing holds (a frozen hold is a re-score too). The guard
recomputes what it can from disk and states exactly what it verified:

| What the guard finds | Why | Verdict effect |
|---|---|---|
| Well-formed, freshly distinct decoded-PCM identity, recomputed from disk and matching | a verified fresh recapture | proceeds -- eligible for `improved` |
| Identical decoded PCM before vs. after | the after run re-scored the SAME conversation (a header-only edit or trailing-byte append can't hide it -- the check is on samples, not container bytes) | `refused` (exit `3`) |
| Recorded digest does NOT match the audio on disk | provenance was hand-edited, or the audio was swapped after capture | `refused` (exit `3`) |
| A required before fixture (target or hold) missing from the after set | a cherry-picked, incomplete comparison | `refused` (exit `3`) |
| Malformed block (non-hex digest, absurd sample rate/frame count, or a top-level digest inconsistent with the per-side digests) | an unvalidated assertion, not a distinct recording | `inconclusive` (exit `1`) |
| Provenance block missing on either side | an older or hand-built envelope; identity is UNKNOWN | `inconclusive` (exit `1`) |
| Well-formed identity hotato could NOT recompute (audio not present) | asserted, not proven | `inconclusive` (exit `1`) |

A provenance-guard refusal is NOT the apply-gate refusal: it fires AFTER
`verify` / `contract verify` / `explain` already ran, so -- unlike the
both-axes refusal, which reads no evidence at all -- the full report
(verify's proof, the contract rollup, the provenance identities, the
attribution) still renders below the refusal banner. Every refusal path
exits the SAME code `3`; `refusal_kind` in the JSON output
(`"threshold_funnel"`, `"incomplete_after"`, `"recompute_mismatch"`,
`"same_audio_recapture"`) tells them apart for a script that wants to.

```
No fix will be certified from re-scored audio
Reason: 1 fixture(s) this verdict rests on (f1) have identical before/after decoded PCM: the after run re-scored the SAME conversation the before run scored, just against a different threshold or scorer config
Recommended: recapture the fixture(s) through the applied clone (hotato apply --clone --yes) and re-run hotato fix trial against the new after evidence
```

Every rendered report (text, `--format json`, `--html`) shows the short
digest and verified status for every guarded fixture, before and after --
a reader never has to take "fresh capture" on faith. The effective
`--min-n` is echoed in every surface too, so a lowered floor is always
visible. The report's own conclusion states plainly what a passing check
proves: that the fresh take passed the same human-labeled contract, not
that the change caused it (hotato reports coincidence, never causation,
throughout).

The text and HTML renders of this section also print, verbatim, wherever it
appears (an `improved` verdict, or a `refused`/`inconclusive` one the guard
itself downgraded): *"Provenance caution: this proves the specific fresh
capture scored above, at the revision it was captured from. It does not
certify a later deploy or every future call, and it does not re-run itself;
recapture again after the next change."* See
[`docs/RECAPTURE.md`](RECAPTURE.md#claim-language-what-each-kind-of-evidence-lets-you-accurately-say)
for the fuller claim-language table this line is drawn from.

## What this does not stop

This is an offline tool: a user who controls every input can lie to
themselves. Recomputing identity from the audio (above) makes the specific
forgeries an external red-team demonstrated against a prior build --
hand-written envelopes, a flipped header byte, a re-scored recording, a
cherry-picked after set -- impossible or loud. None of the following is a
bug the guard missed; each sits outside what an offline recompute over
supplied files can ever establish:

* **Fabricated inputs are still yours to fabricate.** Hand fix trial a
  fresh recording of a call that never happened, or one that does not
  match the bug you are claiming to fix, and the guard verifies the audio
  identity and still reaches `improved`. It checks that the bytes it
  scored are what they claim to be, never that the stimulus itself is
  real.
* **A contract's `MANIFEST.sha256.json` is integrity, not authenticity.**
  It proves the archive agrees with itself after packing, not who approved
  the policy inside it. Loosen a `.hotato` bundle's policy (raise
  `max_talk_over_sec`, say) before `contract pack`, and `contract verify`
  on the repacked bundle still passes -- it re-checks the archive against
  itself, not against an external record of what the policy was supposed
  to be. Only a trusted signature over the manifest closes this; none is
  implemented today.
* **A resample, re-encode, or gain change of the SAME call still changes
  the decoded PCM.** The freshness check above is exactly "is the decoded
  PCM different." A deliberately transcoded copy of the identical
  recording (resampled, gain-adjusted, round-tripped through a lossy
  codec) decodes to different samples, so it reads as a distinct capture
  -- it is not; it is the same call in a different container. A known,
  undetected residual of a PCM-identity check, not a claim the guard
  breaks.
* **Signatures are not implemented.** Nothing here is cryptographically
  signed; a sha256 digest is a checksum, not an attestation of who
  produced it.

A green fix trial does not prove the audio was freshly captured for the
scenario claimed, that the same policy and labels were used throughout,
that any omitted fixture was safe to omit, that the named revision or
clone existed, that the patch was applied to it, or that the deployed
agent improved. The verdict states exactly what it recomputed and
verified; everything else sits outside what an offline tool can promise.

## Flags

- **`PATCH_JSON`** -- a `hotato patch` artifact.
- **`--name NAME`** -- name of the staging clone this trial is proving
  (required for a non-refused patch; the same `--name` `hotato apply`
  takes -- fix trial evaluates the SAME clone-only gate, it never creates a
  clone itself).
- **`--before RUN.json|DIR`** -- the OLD run envelope(s): the original
  failure evidence. Also the default opposite-risk `--battery`, and the
  attribution source, when those are omitted.
- **`--after RUN.json|DIR`** -- the NEW run envelope(s), re-captured
  through the staging clone.
- **`--battery DIR`** -- the opposite-risk battery apply's gate checks
  (BOTH a yield and a hold fixture); defaults to `--before`.
- **`--contracts DIR`** -- also re-verify a directory of hotato contracts;
  any contract regression fails the trial.
- **`--policy hotato.verify.yaml`** -- gate verify's rollup (see
  [`docs/FIX-LOOP.md`](FIX-LOOP.md)); a violation fails the trial.
- **`--min-n N`** -- minimum previously-failing fixtures needed to support
  the claim (default 3).
- **`--out PATH`** -- also write the full proof JSON.
- **`--html PATH`** -- also write a self-contained before/after HTML
  report.

## Output

Every surface shows the apply receipt beside the verdict: fix trial calls
`apply.build_apply`, never `apply.create_clone`, so `apply_dry_run` is
`True` and `apply_created` / `apply_applies_change` are `False` on every
run, including an `improved` one -- a green verdict never means "and the
change was applied." Text prints `apply: dry_run=True created=False
applies_change=False` plus a plain-English line; JSON carries the same
fields (`apply_dry_run` / `apply_created` / `apply_applies_change` /
`apply_receipt_note`) at the top level next to `verdict`; HTML renders them
as header pills. Proving the change reached the clone or agent is `hotato
apply --clone --yes`'s job, recorded in its own receipt -- fix trial only
proves what the before/after evidence shows once that's already happened.

* **text** (default): the apply receipt, the verdict, verify's own
  rendered proof, the contract verify rollup when `--contracts` was given,
  and the attribution section (one `hotato explain` render per file under
  `--before`). When the trial's own verdict is not `improved` (a
  provenance, completeness, contract, or policy issue downgraded it),
  verify's nested `CLAIM` line is tagged `CLAIM (SUPERSEDED BY {VERDICT})`
  with a one-line restatement, whenever that sub-claim would otherwise
  read "supported" -- a fix-trial verdict of `regressed` / `refused` /
  `inconclusive` can still contain a verify claim that, read alone, looks
  like a pass; the parent verdict controls.
* **`--format json`**: the full machine shape, schema `hotato.fix_trial.v1`.
  Every sub-result is the nested result the underlying command already
  produces (`apply`, `verify`, `contract_verify`, `attribution`), not a
  re-derived summary. `apply_dry_run` / `apply_created` /
  `apply_applies_change` / `apply_receipt_note` sit at the top level next
  to `verdict`, not only inside the nested `apply` object.
* **`--html PATH`**: a self-contained report, reusing the same house style
  the other HTML reports use: the apply-receipt pills and note in the
  header, the verdict chip, the verify proof table (with the same
  superseded-claim label as text, when it applies), the contract-verify
  rollup, and the attribution cards.

## Exit codes

- **0** -- `improved`.
- **1** -- fail-closed: `regressed` or `inconclusive`.
- **2** -- usage error: the same gates `hotato apply` enforces (no
  `--name`, no opposite-risk battery, a stack with no clone target, a
  patch with no concrete change) or `hotato verify` / `contract verify`
  already enforce (no fixtures pair, an invalid `--policy`, a `--contracts`
  dir with no contracts, unreadable input).
- **3** -- principled refusal: the both-axes threshold funnel. Shared with
  `hotato apply`'s refusal code.

## What this is not

fix trial does not create, patch, or delete anything on your platform; it
never calls `apply.create_clone`. It does not prove authorization,
identity, compliance, or policy safety. It reports coincidence, never
causation -- every underlying `verify` and `contract verify` number keeps
that rule. See [`FIX-LOOP.md`](FIX-LOOP.md) for the manual steps this
composes, and [`APPLY.md`](APPLY.md) / [`EXPLAIN.md`](EXPLAIN.md) for the
primitives themselves.
