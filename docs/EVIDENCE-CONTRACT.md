# The evidence contract: four tiers, one policy

Every hotato verdict states what evidence it stands on. This page is the
single source of truth for that policy; the README, [AUTOPSY.md](AUTOPSY.md),
the platform health reports, and the trust docs
([TRUST.md](TRUST.md) &#183; [TRUST-MATRIX.md](TRUST-MATRIX.md)) all apply it
and link back here.

Four tiers, by what the recording itself can prove:

## Tier 1 -- dual-channel audio: deterministic

Caller on one channel, agent on the other, separated at capture. Overlap and
timing are facts of the recording, exact to the sample, so the deterministic
timing walk runs end to end: byte-identical output for the same input, every
threshold and frame inspectable. Only this tier is **verdict-eligible** --
`hotato pin` freezes a caught moment as a contract, `hotato prove` and the CI
gate re-run it, and it alone enters the Voice Stability denominator.

## Tier 2 -- mono plus provider metadata: attributable, with declared authority

A mixed single channel cannot attribute energy to a speaker by itself, but
metadata a provider or pipeline supplies alongside it can: a
speaker-attributed transcript, a voice-pipeline trace, tool-call logs, or a
diarizer's turn labels (`hotato run --mono call.wav --diarize`,
[DIARIZE.md](DIARIZE.md)). A finding here is **attributable**: something
assigned the speech to a speaker, and the finding names that source. The
production evidence plane records it, weakest claim first -- `submitted`,
`adapter_reported`, `provider_export`, `signed_attestation`, `measured`.
A verdict a diarizer attributed is stamped `diarized-mono`, and
`indicative_only` when that diarizer's own confidence sits below the bar.
Attribution is only as strong as its source, so the authority prints with
the finding and this tier never merges into Tier 1's deterministic counts.

## Tier 3 -- raw mixed mono: symptom detection, with measured confidence

One mixed channel with no metadata still measures what silence shows:
dead air and latency gaps. `hotato autopsy` (one recording in, the incident
list out) and the mono-stack health reports run this path best-effort, and
every finding carries a **measured confidence** with its derivation printed
beside it. A mono gap says everything stopped, not who stopped -- talk-over
and barge-in attribution comes from the tiers above, that scope is stated once
per run, and Tier 3 findings report in their own block, outside the Voice
Stability denominator.

## Tier 4 -- insufficient evidence: refused, with the remediation

An input that supports none of the above -- an unreadable file, a silent
required channel, a mixed export where a deterministic verdict was asked
for -- is **refused** (exit `2`): hotato scores nothing and prints the reason
and the next step -- the recording scaffold for your stack (`hotato setup`),
the input health check (`hotato trust --stereo call.wav`), or the mono escapes
above. A refusal leaves no artifact and never becomes a number, so a green or
red build only ever reports calls that were scored.

## Reading a report against the tiers

The health and scan reports render the split directly: the Voice Stability
Score and the `health:` share count **dual-channel calls only** (Tier 1);
mono findings sit in the *Best-effort mono observations* block with their
own counts (Tier 3, or Tier 2 where metadata attributed them); refused
files are listed with their reasons (Tier 4), never scored. The **evidence
coverage** block prints each lane's measured count (dual-channel timing, mono
best-effort, refused), so a lane reads as assessed only when it had evidence.
