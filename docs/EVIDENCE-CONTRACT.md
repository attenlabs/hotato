# The evidence contract: four tiers, one policy

Every hotato verdict states what evidence it stands on. This page is the
single source of truth for that policy; the README, [AUTOPSY.md](AUTOPSY.md),
the platform health reports, and the trust docs
([TRUST.md](TRUST.md) &#183; [TRUST-MATRIX.md](TRUST-MATRIX.md)) all apply it
and link back here.

The four tiers, from strongest evidence to none:

## Tier 1 -- dual-channel audio: deterministic

Caller on one channel, agent on the other, separated at capture. Overlap and
timing are facts of the recording, exact to the sample, so the deterministic
timing walk runs end to end: byte-identical output for the same input, every
threshold exposed, every frame inspectable. This is the only tier that is
**verdict-eligible**: contracts, `hotato pin`, `hotato prove`, and the CI
gate all stand on it, and it alone enters the Voice Stability denominator.

## Tier 2 -- mono plus provider metadata: attributable, with declared authority

A mixed single channel cannot attribute energy to a speaker by itself, but
metadata a provider or pipeline supplies alongside it can: a
speaker-attributed transcript, a voice-pipeline trace, tool-call logs, or a
diarizer's turn labels (`hotato run --mono call.wav --diarize`,
[DIARIZE.md](DIARIZE.md)). Findings on this tier are **attributable**, and
each one carries the **declared authority** of the source that attributed it
-- the production evidence plane records it explicitly (`submitted`,
`adapter_reported`, `provider_export`, `signed_attestation`, `measured`),
and a diarized verdict is tiered and stamped (`diarized-mono`;
`indicative_only` below the confidence bar). Attribution is only as strong
as its source, so the authority prints with the finding and this tier never
merges into Tier 1's deterministic counts.

## Tier 3 -- raw mixed mono: symptom detection, with measured confidence

One mixed channel with no metadata still measures what silence shows:
dead air and latency gaps. `hotato autopsy` and the mono-stack health
reports run this path best-effort, and every finding carries a **measured
confidence** with its derivation printed beside it. A mono gap says
everything stopped, not who stopped -- talk-over and barge-in attribution
comes from the tiers above, that scope is stated once per run, and Tier 3
findings report in their own block, outside the Voice Stability
denominator.

## Tier 4 -- insufficient evidence: refused, with the remediation

An input that supports none of the above -- an unreadable file, a silent
required channel, a mixed export where a deterministic verdict was asked
for -- is **refused** (exit `2`) with the reason and the next step: the
recording scaffold for your stack (`hotato setup`), the input health check
(`hotato trust --stereo call.wav`), or the mono escapes above. A refusal
leaves no artifact and never becomes a number; a green or red build always
means something.

## Reading a report against the tiers

The health and scan reports render the split directly: the Voice Stability
Score and the `health:` share count **dual-channel calls only** (Tier 1);
mono findings sit in the *Best-effort mono observations* block with their
own counts (Tier 3, or Tier 2 where metadata attributed them); refused
files are listed with their reasons (Tier 4), never scored. The per-lane
**evidence coverage** block states which tiers this run actually had --
a lane whose evidence was absent never renders as assessed.
