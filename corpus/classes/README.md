# corpus/classes/ seven scenario classes, additive to corpus/suites/

Seven small, deterministic scenario classes, built the same way as
`corpus/suites/`: synthetic shaped noise rendered from each scenario's own
`reference_render` segment timings, seeded by `sha256(id)`, so two renders
are byte-identical on any machine. No recorded speech, no accuracy claim.
Audio is gitignored and regenerates on demand (or automatically at the start
of a `pytest` session, see `tests/conftest.py`).

| class | scenarios | what it holds |
|---|---|---|
| `mid-utterance-pause` | 3 | The caller speaks, pauses mid-turn for a multi-second thinking gap, then resumes. Scored on the latency axis (`premature_start_sec`), with a `turn_end_silence_sec` wide enough that the pause is not mistaken for the caller's true turn end (see `tests/test_corpus_classes.py`). One defect: the agent grabs the floor inside the pause. |
| `backchannel-multilingual` | 5 | Short non-English acknowledgement tokens (romanized labels: Hindi/Telugu "hmm", Spanish "si", Japanese "hai") over agent speech, should NOT yield. Hotato's VAD is energy-based, not lexical; it does not detect language. One defect: a false yield on the Spanish token. |
| `noise-hold` | 3 | The caller channel carries sustained non-speech energy for most of the call (a cafe/TV-like background presence, not a brief backchannel), should NOT yield. Hotato measures whether the agent held the floor through it; it does not classify the energy as noise versus speech. One defect: a false yield triggered by the ambient energy. |
| `telephony-degraded` | 2 | The exact `reference_render` timings of the existing `gl-8k-hard-interrupt` gold scenario, re-rendered through a degraded 8 kHz telephony line: G.711 mu-law companding (`telephony_codec.py`) plus a fixed, mild, non-random packet-loss schedule. One PASS and one defect FAIL, proving the scorer's verdict is stable across codec degradation in both directions. |
| `leading-edge-onset` | 3 | The caller onset IS a short leading burst (a leading-phoneme analog) at the interruption boundary, then the sustained utterance. Two PASS renders (burst on a frame boundary, and shifted off it) yield within the bound measured from the labeled onset. One defect FAIL drops the leading burst from the caller channel while the label keeps the ground-truth onset, so a pipeline that drops leading audio at the boundary is measurable: the corroborated yield lands at the later utterance and time-to-yield runs past the bound. |
| `structured-utterance` | 4 | The caller reads structured data in one turn (a 3-3-4 phone number, a spelled email with local part / "at" / domain bursts) with intra-item gaps. Scored on the latency axis with a `turn_end_silence_sec` wider than the widest intra-item gap, so a pause between digit groups (or after "at") is not mistaken for the turn end. Two PASS (agent waits for the true end) and two defect FAIL (agent grabs the floor inside an intra-item gap: a digit gap, and the pause after "at"). The label states every gap duration. |
| `browser-telephony-parity` | 2 | One scripted conversation, two renders. The clean browser leg (16 kHz, continuous turn-taking) surfaces zero `long_response_gap` candidates at the default 2.0s `hotato scan` threshold. The telephony leg is the identical `reference_render` timings through `telephony_codec.py` (mu-law + packet loss) plus a fixed schedule that silences the agent channel over stated windows, so the same scan surfaces exactly those windows as gaps. Same scenario, same scan, the divergence is the finding: passes in the browser, fails on the phone line. |

Rebuild or verify the whole tree:

```bash
python3 corpus/classes/build_classes.py          # write labels + render audio
python3 corpus/classes/build_classes.py --check  # regenerate to a temp dir, byte-compare
```

`tests/test_corpus_classes.py` is the regression gate: manifest vs disk,
schema shape and honesty rules, every labeled verdict scored through the real
entry point, and the byte-identical regenerate. Every test skips cleanly if
the audio has not been rendered yet.

Kept separate from `corpus/suites/` on purpose: `mid-utterance-pause` and
`structured-utterance` need a non-default scoring config (a `turn_end_silence_sec`
wider than the rendered pause / widest intra-item gap), and
`browser-telephony-parity` is scored by the whole-call `hotato scan` rather than
the barge-in verdict; the generic, dynamically-discovered suite tests apply
neither. See the module docstrings in `build_classes.py` and
`tests/test_corpus_classes.py` for the full reasoning.
