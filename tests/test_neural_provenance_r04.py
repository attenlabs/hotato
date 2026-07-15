"""R-04 regression: the neural scorer's IDENTITY (implementation source + ONNX
weights) is bound into its provenance.

The energy reference's ``wheel_hash`` covers only ``__init__``/``core``/``_engine``
-- NOT top-level ``neural.py`` or the bundled ``data/silero_vad.onnx``. Under
``--backend neural`` those two files ARE the per-frame speech decision, so a
swapped ONNX or an edited ``neural.py`` used to leave provenance byte-identical:
the tampered scorer was indistinguishable from the honest one. After the fix,
``neural_backend_provenance()`` carries ``source_sha256`` + ``weights_sha256`` --
the exact content digests of the scorer source and weights -- so any tamper
changes the recorded identity.

``neural_backend_provenance()`` only reads + hashes bytes (no onnxruntime), so
these tests need neither the ``[neural]`` extra nor any audio.
"""

import hashlib
from importlib import resources

from hotato import neural
from hotato.core import _vad_backend_provenance
from hotato._engine.score import ScoreConfig
from hotato._engine.vad import VADParams


def _expected(rel_parts):
    return hashlib.sha256(
        resources.files("hotato").joinpath(*rel_parts).read_bytes()
    ).hexdigest()


def test_provenance_carries_source_and_weights_digests():
    """Pre-fix the provenance had no content identity; post-fix it names the
    exact neural.py source + silero_vad.onnx weights that produced the track."""
    prov = neural.neural_backend_provenance()
    assert "source_sha256" in prov
    assert "weights_sha256" in prov
    # They are the REAL content digests of the shipped files, not a constant, so
    # a tampered neural.py or a swapped ONNX yields a different digest here.
    assert prov["source_sha256"] == _expected(["neural.py"])
    assert prov["weights_sha256"] == _expected(["data", "silero_vad.onnx"])
    # And they are well-formed sha256 hex (64 lowercase hex chars).
    for key in ("source_sha256", "weights_sha256"):
        assert len(prov[key]) == 64
        assert all(ch in "0123456789abcdef" for ch in prov[key])
    # The non-reference labeling is preserved -- never mistaken for the energy
    # reference.
    assert prov["reference"] is False


def test_recorded_digests_are_bound_to_actual_content():
    """A swapped ``silero_vad.onnx`` or a tampered ``neural.py`` must change the
    recorded identity -- otherwise the tampered scorer would be indistinguishable
    in provenance. Proven by content-binding: the recorded digest equals the hash
    of the shipped bytes, and any single-byte change hashes differently."""
    prov = neural.neural_backend_provenance()

    real_weights = resources.files("hotato").joinpath("data", "silero_vad.onnx").read_bytes()
    real_source = resources.files("hotato").joinpath("neural.py").read_bytes()

    # The recorded digests IDENTIFY the exact shipped weights + source.
    assert prov["weights_sha256"] == hashlib.sha256(real_weights).hexdigest()
    assert prov["source_sha256"] == hashlib.sha256(real_source).hexdigest()

    # A swapped ONNX (any single byte flipped) hashes differently, so the
    # recorded scorer identity would change -- the swap is detectable, not silent.
    tampered_weights = bytearray(real_weights)
    tampered_weights[0] ^= 0xFF
    assert hashlib.sha256(bytes(tampered_weights)).hexdigest() != prov["weights_sha256"]

    # Same for an edited neural.py implementation.
    tampered_source = real_source + b"\n# tampered\n"
    assert hashlib.sha256(tampered_source).hexdigest() != prov["source_sha256"]


def test_digest_flows_through_vad_backend_provenance():
    """The digest reaches the scored event: core._vad_backend_provenance stamps
    neural_backend_provenance() (incl. the digests) under a neural cfg, while the
    energy default stays byte-identical (no block at all)."""
    cfg = ScoreConfig(caller_vad=VADParams(backend="neural"),
                      agent_vad=VADParams(backend="neural"))
    block = _vad_backend_provenance(cfg)
    assert block is not None
    assert "source_sha256" in block["neural"]
    assert "weights_sha256" in block["neural"]

    # Energy reference (the default) attaches NO provenance -> byte-identical.
    assert _vad_backend_provenance(ScoreConfig()) is None


def test_provenance_is_deterministic_across_calls():
    """Repeated calls return equal digests (a fresh dict each time), so the
    identity is stable within a process."""
    a = neural.neural_backend_provenance()
    b = neural.neural_backend_provenance()
    assert a == b
    assert a is not b
