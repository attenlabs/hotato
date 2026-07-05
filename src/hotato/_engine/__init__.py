"""voice-agent-barge-in-tests: a dependency-light harness for measuring the three
objective barge-in signals (did the agent yield, time to yield, talk-over
seconds) from a voice-agent call recording.

See the repository README for the failure mode this tests and how to wire it
into CI. This package measures timing from audio and makes no accuracy claim
about any detector.
"""

from .audio import Signal, read_wav, write_wav
from .score import (
    ScoreConfig,
    ScoreResult,
    Verdict,
    evaluate,
    score_channels,
    score_stereo,
)
from .vad import (
    BackendUnavailable,
    VADParams,
    clear_neural_backend,
    energy_vad,
    neural_vad,
    register_neural_backend,
)

__version__ = "0.1.0"

__all__ = [
    "Signal",
    "read_wav",
    "write_wav",
    "ScoreConfig",
    "ScoreResult",
    "Verdict",
    "evaluate",
    "score_channels",
    "score_stereo",
    "VADParams",
    "energy_vad",
    "neural_vad",
    "register_neural_backend",
    "clear_neural_backend",
    "BackendUnavailable",
    "__version__",
]
