"""hotato: open-source, self-hosted conversation QA for voice agents.

Simulate, evaluate, review, and track calls across five dimensions (outcome,
policy, conversation, speech, reliability) with the evidence behind every result.
Free (MIT), self-hostable, zero-install, and offline. Its primary consumer is an
AI agent: grab it as a CLI (``uvx hotato ...``) or as a one-tool MCP server
mid-task. Deterministic checks stay separate from the model-judged rubric lane;
there is no blended score. The turn-taking (conversation) dimension is the
deterministic crown jewel, and it points a surfaced failure at the KIND of fix it needs. Discrimination
failures are not solvable by one timing threshold; where your stack provides an
interruption/backchannel classifier, use it, otherwise a learned
engagement-control / addressee-detection layer is needed. The pointer is
vendor-neutral and names no product.

Honesty is the point, not a footnote: there is no fabricated accuracy anywhere.
The numbers are reproducible timing measurements with an exposed method and an
explicit ceiling.
"""

from ._engine.vad import register_neural_backend as _register_neural_backend
from .core import LIMITS, SUITE_ID, run_single, run_suite
from .neural import build_silero_backend as _build_silero_backend

# Register the OPTIONAL, non-reference neural VAD backend (Silero VAD, MIT). This
# only stores the factory reference: the model and its extra are imported lazily,
# and only if backend="neural" is actually requested -- so importing hotato stays
# zero-dependency and the energy reference path is untouched. With the [neural]
# extra absent, a neural request raises a clean BackendUnavailable (never a silent
# fallback to energy that could change a published number).
_register_neural_backend(_build_silero_backend)

# Register the OPTIONAL, non-reference diarizer backends (the mono-scorability
# front-end). Same discipline as the neural seam: this only stores the factory
# references by name -- each model and its extra ([diarize] / [diarize-sortformer]
# / [diarize-hosted]) is imported lazily, and only if a diarized run
# (hotato run --mono call.wav --diarize [--diarizer ...]) is actually requested.
# Importing hotato stays zero-dependency and the dual-channel reference is
# untouched. With the extra absent, a diarized request raises a clean
# BackendUnavailable (never a silent fallback that scores raw mono).
from .diarize import (  # noqa: E402
    build_pyannote_backend as _build_pyannote_backend,
)
from .diarize import (
    build_pyannoteai_backend as _build_pyannoteai_backend,
)
from .diarize import (
    build_sortformer_backend as _build_sortformer_backend,
)
from .diarize import (
    register_diarizer_backend as _register_diarizer_backend,
)

_register_diarizer_backend("pyannote", _build_pyannote_backend)
_register_diarizer_backend("sortformer", _build_sortformer_backend)
_register_diarizer_backend("pyannoteai", _build_pyannoteai_backend)

# Version lockstep: this literal MUST match pyproject.toml's `version` (and
# server.json, CITATION.cff, llms.txt, CHANGELOG.md -- see
# docs/RELEASE-CHECKLIST.md). It is deliberately a literal, not derived from
# importlib.metadata: the suite runs from uninstalled source trees (conftest
# prepends src/ to sys.path; CI runs an extracted sdist), where dist-info is
# absent or can describe a DIFFERENT installed copy than the code executing.
# tests/test_version_lockstep.py enforces the match; 0.4.0 shipped
# self-reporting 0.3.1 because nothing did.
__version__ = "1.13.0"

__all__ = ["run_single", "run_suite", "LIMITS", "SUITE_ID", "__version__"]
