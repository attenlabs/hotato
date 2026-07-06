# License verification for the real-audio example set

Every source considered for this tree was license-checked against three
requirements: permission to download, permission to modify (cut clips,
re-pair channels, resample), and permission to redistribute the derived
clips with attribution. Quotes below are verbatim from the pages cited.
Access date for all quotes: 2026-07-06.

## Primary source (used): AMI Meeting Corpus, CC BY 4.0

- License page: https://groups.inf.ed.ac.uk/ami/corpus/license.shtml
- Download page: https://groups.inf.ed.ac.uk/ami/download/
- HuggingFace mirror: https://huggingface.co/datasets/edinburghcstr/ami

From the license page:

> "The AMI corpus and its annotations are released under the Creative
> Commons Attribution 4.0 license agreement (also called CC BY 4.0). Use of
> this data implies agreement with the terms below."

The page is followed by the full CC BY 4.0 legal code. Section 2(a)(1) of
CC BY 4.0 grants the right to "reproduce and Share the Licensed Material,
in whole or in part" and to "produce, reproduce, and Share Adapted
Material"; Section 3(a) conditions this only on attribution.

From the download page:

> "All of the signals and transcription, and some of the annotations, have
> been released publicly under the Creative Commons Attribution 4.0
> International Licence (CC BY 4.0)."

The download page also notes for the annotation releases: "annotations
unchanged since 16-June 2014 release; license altered to CC BY 4.0"
(the corpus was previously under a non-commercial license; the current
release is CC BY 4.0). The HuggingFace mirror `edinburghcstr/ami` carries
the matching `cc-by-4.0` license tag.

Verdict: download, modification, and redistribution of derived clips are
permitted with attribution. This is the source used here. This build uses
the official Edinburgh mirror (full-meeting individual headset WAVs plus
the NXT word alignments) rather than the HuggingFace mirror, because the
HF variant is pre-segmented per utterance and loses the cross-speaker time
alignment this set depends on.

### Attribution (as CC BY 4.0 requires)

The audio clips in `corpus/real/audio/` contain material from:

> AMI Meeting Corpus, https://groups.inf.ed.ac.uk/ami/corpus/,
> licensed under the Creative Commons Attribution 4.0 International
> License (CC BY 4.0), https://creativecommons.org/licenses/by/4.0/.
> Created by the AMI Consortium.

Changes made: each clip is a short window (9 seconds) cut from two
speakers' individual headset microphone channels, paired into one
two-channel WAV (channel 0 = the party that starts talking, channel 1 =
the party holding the floor), 16 kHz 16-bit PCM. Onset times, speech
segments, and category labels are derived from the corpus's manual word
alignments (`ami_public_manual_1.6.2.zip`). The derived clips remain under
CC BY 4.0. They are NOT part of hotato's MIT-licensed contribution corpus
and are not relicensed; every clip's JSON carries the same attribution
block. The exact source files and their sha256 checksums are pinned in
`build_real.py` and recorded in `manifest.json`.

Citation the corpus asks for: Jean Carletta et al., "The AMI Meeting
Corpus: A Pre-announcement", MLMI 2005.

## Considered and usable, not used: ICSI Meeting Corpus

- Edinburgh distribution: https://groups.inf.ed.ac.uk/ami/icsi/license.shtml

> "The ICSI corpus and its annotations are released under the Creative
> Commons Attribution 4.0 license agreement (also called CC BY 4.0). Use
> of this data implies agreement with the terms below."

The LDC distribution of the same corpus (LDC2004S02) is under the separate
paid "LDC User Agreement for Non-Members" and is NOT freely
redistributable; only the Edinburgh CC BY 4.0 distribution would qualify.
Verdict: usable via Edinburgh with attribution; skipped only because AMI
already provides per-speaker headset channels with word alignments and one
primary source keeps the provenance chain short.

## Considered and rejected: LibriCSS

- Repo: https://github.com/chenzhuo1011/libri_css

The repo LICENSE is MIT, but its text covers "this software and associated
documentation files (the 'Software')", the evaluation code, not the
re-recorded audio. The LICENSE file adds only:

> "The original LibriSpeech corpus is distributed at
> http://www.openslr.org/12/ under CC-BY 4.0."

The LibriCSS audio itself (replayed LibriSpeech re-recorded in a meeting
room, hosted on Google Drive) ships with no explicit license statement of
its own, and GitHub's license detection reports `NOASSERTION`. Verdict:
no clear grant for redistributing derived clips of the re-recorded audio;
rejected on documentation, not on suspicion of bad faith.

## Considered and rejected: Santa Barbara Corpus of Spoken American English

- https://www.linguistics.ucsb.edu/research/santa-barbara-corpus-spoken-american-english

> "SBCSAE by John W. Du Bois is licensed under a Creative Commons
> Attribution-No Derivative Works 3.0 United States License."

Verdict: CC BY-ND forbids distributing adapted material; cut clips are
adaptations. Rejected.

## Considered and rejected: TalkBank / CABank

- https://talkbank.org/0share/rules.html

> "Except where otherwise indicated, the use of TalkBank data is governed
> by the Creative Commons CC BY-NC-SA 3.0 copyright license."

Verdict: NonCommercial plus ShareAlike is incompatible with redistribution
inside this repository. Rejected.
