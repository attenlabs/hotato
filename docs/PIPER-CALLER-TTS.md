# Local Piper caller speech

`hotato.piper_tts.PiperCallerTTS` turns caller `say` actions into mono PCM16LE
with a local Piper executable. It is an optional adapter around the Piper CLI,
not a bundled speech model and not an audio-quality claim.

## CLI

Supply both the ONNX model and its matching config:

```bash
hotato caller run scenarios/appointment.json \
  --target-ws ws://127.0.0.1:8765/caller \
  --piper-model models/en_US-lessac-medium.onnx \
  --piper-config models/en_US-lessac-medium.onnx.json \
  --out artifacts/appointment
```

The same flags provide spoken input for the direct LiveKit caller session. The
config must declare `audio.sample_rate`. Hotato publishes that rate without an
implicit resample; an incompatible transport rate is refused.

Useful bounds are explicit:

```text
--piper-command PATH             default: piper
--piper-timeout SEC              default: 60
--piper-max-output-bytes BYTES   default: 33554432
--piper-voice LABEL              default: default
```

The voice label is descriptive provenance. It does not select a Piper speaker;
use a single-speaker model or configure speaker selection in a separately
reviewed adapter before claiming multi-speaker coverage.

## Execution boundary

For each CLI run, the adapter:

1. refuses model/config symlinks, FIFOs, devices, oversized files, and files
   changed while they are staged;
2. copies the exact model and config bytes once into a mode-`0600` private
   temporary directory;
3. invokes the resolved Piper executable with an argument vector and
   `shell=False`;
4. supplies a minimal environment instead of forwarding operator secrets;
5. bounds input text, PCM stdout, diagnostic stderr, execution time, staged
   model/config bytes, and executable bytes;
6. accepts only non-empty, even-length `--output_raw` bytes; and
7. deletes the private staging directory when the caller run closes.

No diagnostic text enters the package. Successful synthesis records the staged
model/config SHA-256 values, the resolved executable SHA-256 observed before
and after execution, sample rate, mono PCM16LE encoding, diagnostic byte count,
and diagnostic digest. Model/config paths, the private temporary path,
environment values, and Piper stderr contents are excluded.

The resulting PCM is content-addressed again by the caller engine before it is
submitted to the transport. Piper success establishes local synthesis only.
`sdk_playout_complete`, target delivery, SIP delivery, and carrier delivery are
separate evidence boundaries.

## Acceptance gate

The hermetic suite exercises argument-vector invocation, bounded output,
timeout, symlink refusal, private-environment behavior, PCM structure, cleanup,
and provenance. Before publishing a voice-quality or performance result, pin
the Piper release, executable digest, model license and digest, config digest,
hardware, input corpus, and the metric and adjudication method used.
