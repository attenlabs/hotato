# hotato.diarize benchmark harness (throwaway, not shipped)

Reproduces the methodology of `hotato-launch/DIARIZE-BENCHMARK-2026-07-09.md`
(spec 8 of `SEPARATION-BUILD-SPEC-2026-07-09.md`) against a REAL diarizer, so a
morning-only `/tmp` harness (wiped by a reboot) survives inside the repo.

This is a dev-only measurement tool, not product code: it calls the SHIPPED
`hotato.diarize` module's public functions in the same order
`prepare_diarized_mono` does, plus `pyannote.metrics` for DER. It lives under
`tools/`, which is NOT listed in `MANIFEST.in` (unlike `scripts/`, which IS
shipped in the sdist -- see the recursive-include list there), so it never
ships in the wheel or sdist. Needs the heavy `[diarize]` extra (torch,
pyannote.audio>=4.0, pyannote.metrics) which the base package does not
require.

## Setup

```
python3 -m venv .venv-bench
source .venv-bench/bin/activate
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
pip install "pyannote.audio>=4.0" pyannote.metrics
pip install -e ../..   # hotato itself, editable
export HF_TOKEN=$(cat ~/.hf-token)   # a token with community-1's conditions accepted
```

(pyannote.audio 4.x depends on `torchaudio>=2.8`, which the CPU wheel index
only publishes up to `2.11.0+cpu`; pin `torch==2.11.0` to match, or you will
get a CUDA-linked `torchaudio` that fails to import with `libcudart.so.13`
errors on a CPU-only box.)

## Run

```
python bench_diarize.py            # writes bench_results.json here
python aggregate.py                # reads bench_results.json, prints the tables
```

`bench_results.json` and any `*.log` here are gitignored (see `.gitignore`
next to this file) -- they are measurement output, not source.
