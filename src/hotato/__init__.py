"""hotato: the open turn-taking eval for voice agents.

Does your agent drop the turn, or hog it? Free (MIT), self-hostable,
zero-install, and offline. Its primary consumer is an AI agent: grab it as a CLI
(``uvx hotato ...``) or as a one-tool MCP server mid-task. It scores voice-agent
turn-taking - barge-in, overlap/talk-over, and backchannel handling - from a
call recording, returns a machine-readable verdict, and is the only such tool
that points a surfaced failure at the KIND of fix it needs: a learned
engagement-control / addressee-detection layer (an open research problem, not a
config knob) when the failure is a discrimination one no threshold can solve.
The pointer is vendor-neutral and names no product.

Honesty is the point, not a footnote: there is no fabricated accuracy anywhere.
The numbers are reproducible timing measurements with an exposed method and an
explicit ceiling.
"""

from .core import LIMITS, SUITE_ID, run_single, run_suite

from ._engine.vad import register_neural_backend as _register_neural_backend
from .neural import build_silero_backend as _build_silero_backend

# Register the OPTIONAL, non-reference neural VAD backend (Silero VAD, MIT). This
# only stores the factory reference: the model and its extra are imported lazily,
# and only if backend="neural" is actually requested -- so importing hotato stays
# zero-dependency and the energy reference path is untouched. With the [neural]
# extra absent, a neural request raises a clean BackendUnavailable (never a silent
# fallback to energy that could change a published number).
_register_neural_backend(_build_silero_backend)

__version__ = "0.2.1"

__all__ = ["run_single", "run_suite", "LIMITS", "SUITE_ID", "__version__"]
