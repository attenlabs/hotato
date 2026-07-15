"""R-08: recompute must score under the config the manifest PINNED, not a
fresh default ScoreConfig(), and a caller-supplied config that diverges from the
pinned one must be surfaced in the evidence vector -- never silently labelled
'recomputed'.

Before this fix, ``recompute.recompute_trial`` did ``cfg = cfg or ScoreConfig()``
and never reconstructed ``man['scorer']['config']`` / compared
``man['scorer']['config_hash']`` -- so a manifest that pinned a non-default
scorer config (e.g. a tighter ``yield_hangover_sec`` or an alternate VAD backend)
was rescored under a hard-coded default and still stamped 'recomputed'
(TIER_ATTESTED). The pinned ``config_hash`` field was written into every manifest
and never read back for verification.
"""
import json

from hotato import core as _core
from hotato import evidence as _evidence  # noqa: F401  (kept for tier symmetry)
from hotato import manifest as _manifest
from hotato import recompute as _recompute
from hotato._engine.score import ScoreConfig
from hotato._engine.vad import VADParams
from tests import _trial_audio as ta

ONSET = 5.0
TOTAL = 8.0
SUF = ".example.wav"


def _make_trial(tmp_path):
    """One yield fixture: before fails (talkover), after passes (same caller
    stimulus, the agent now yields). Returns
    (before_env, before_dir, after_env, after_dir)."""
    scen = tmp_path / "scenarios"
    before = tmp_path / "before"
    after = tmp_path / "after"
    for d in (scen, before, after):
        d.mkdir()
    (scen / "f1.json").write_text(
        json.dumps({"id": "f1", "title": "f1", "caller_onset_sec": ONSET,
                    "expected": {"yield": True}}),
        encoding="utf-8")
    ta.talkover_call(str(before / ("f1" + SUF)), onset=ONSET, total=TOTAL)
    ta.yielding_call(str(after / ("f1" + SUF)), onset=ONSET, total=TOTAL)
    before_env = _core.run_suite(scenarios_dir=str(scen), audio_dir=str(before),
                                 suffix=SUF)
    after_env = _core.run_suite(scenarios_dir=str(scen), audio_dir=str(after),
                                suffix=SUF)
    (before / "run.json").write_text(json.dumps(before_env), encoding="utf-8")
    (after / "run.json").write_text(json.dumps(after_env), encoding="utf-8")
    return before_env, str(before), after_env, str(after)


# --- manifest.config_from_dict: the inverse used to reconstruct the pin -------

def test_config_from_dict_roundtrips_pinned_config():
    cfg = ScoreConfig(yield_hangover_sec=0.5, max_search_sec=2.0,
                      caller_vad=VADParams(rel_db=18.0, hangover_sec=0.3))
    d, h = _manifest.score_config_hash(cfg)
    rebuilt = _manifest.config_from_dict(d)
    d2, h2 = _manifest.score_config_hash(rebuilt)
    assert h2 == h and d2 == d
    assert rebuilt.yield_hangover_sec == 0.5
    assert rebuilt.max_search_sec == 2.0
    assert rebuilt.caller_vad.rel_db == 18.0
    assert rebuilt.caller_vad.hangover_sec == 0.3


def test_config_from_dict_defaults_and_partial_are_valid():
    default_hash = _manifest.score_config_hash(ScoreConfig())[1]
    # a default round-trips to a default
    d = _manifest.score_config_hash(ScoreConfig())[0]
    assert _manifest.score_config_hash(_manifest.config_from_dict(d))[1] == default_hash
    # robust to None / empty / additive-unknown keys (older or forward manifests)
    assert _manifest.score_config_hash(_manifest.config_from_dict(None))[1] == default_hash
    assert _manifest.score_config_hash(_manifest.config_from_dict({}))[1] == default_hash
    assert _manifest.score_config_hash(
        _manifest.config_from_dict({"unknown_future_field": 1}))[1] == default_hash


# --- R-08 primary: recompute defaults to the manifest's pinned config ---------

def test_recompute_defaults_to_manifest_pinned_config(tmp_path, monkeypatch):
    """With no cfg supplied, recompute_trial must score under the config the
    manifest pinned, NOT a fresh default. Pre-fix it always passed a default
    ScoreConfig() to run_single regardless of the pin."""
    before_env, before_dir, after_env, after_dir = _make_trial(tmp_path)
    pinned = ScoreConfig(yield_hangover_sec=0.5)          # deliberately non-default
    man = _manifest.build_manifest(before_env, trial_id="t", nonce="n",
                                   min_n=1, cfg=pinned)
    _, pinned_hash = _manifest.score_config_hash(pinned)
    default_hash = _manifest.score_config_hash(ScoreConfig())[1]
    assert man["scorer"]["config_hash"] == pinned_hash != default_hash

    seen = []
    real_run_single = _recompute._core.run_single

    def _spy(*args, **kwargs):
        seen.append(kwargs.get("cfg"))
        return real_run_single(*args, **kwargs)

    monkeypatch.setattr(_recompute._core, "run_single", _spy)
    _recompute.recompute_trial(before_env, before_dir, after_env, after_dir, man)

    assert seen, "run_single was never called (audio was not recomputed)"
    for cfg in seen:
        assert cfg is not None
        # the cfg recompute actually scored under is the manifest's pinned one,
        # not a default -- pre-fix this hashed to default_hash and failed.
        assert _manifest.score_config_hash(cfg)[1] == pinned_hash


# --- R-08 divergence: a caller-supplied cfg != the pin is refused, not green --

def test_recompute_refuses_caller_config_divergent_from_pin(tmp_path):
    """When a caller supplies a cfg whose hash differs from the manifest's
    pinned scorer.config_hash, the recompute ran under a DIFFERENT config than
    the manifest attests to: it must be a refusal + score_integrity
    'config_changed', never a silent 'recomputed'. The supplied cfg here is the
    DEFAULT (what the stored envelopes were scored under) so verdicts still match
    exactly -- isolating the config divergence as the sole integrity issue.
    Pre-fix: no config comparison existed, so this legit-looking improvement had
    refusal=None and score_integrity='recomputed'."""
    before_env, before_dir, after_env, after_dir = _make_trial(tmp_path)
    pinned = ScoreConfig(yield_hangover_sec=0.5)          # manifest pins non-default
    man = _manifest.build_manifest(before_env, trial_id="t", nonce="n",
                                   min_n=1, cfg=pinned)
    rc = _recompute.recompute_trial(before_env, before_dir, after_env, after_dir,
                                    man, cfg=ScoreConfig())  # caller supplies default

    assert rc["refusal"] is not None
    assert rc["refusal"]["kind"] == "config_mismatch"
    assert rc["evidence"]["vector"]["score_integrity"] == "config_changed"
    assert rc["evidence"]["allows_positive_paired"] is False


def test_recompute_caller_cfg_matching_pin_is_not_flagged(tmp_path):
    """Guard against over-refusal: a caller cfg whose hash EQUALS the pinned one
    is honoured with no config divergence and no config refusal."""
    before_env, before_dir, after_env, after_dir = _make_trial(tmp_path)
    man = _manifest.build_manifest(before_env, trial_id="t", nonce="n", min_n=1)
    # rebuild the exact pinned cfg and hand it back explicitly
    supplied = _manifest.config_from_dict(man["scorer"]["config"])
    rc = _recompute.recompute_trial(before_env, before_dir, after_env, after_dir,
                                    man, cfg=supplied)
    assert not (rc["refusal"] and rc["refusal"]["kind"] == "config_mismatch")
    assert rc["evidence"]["vector"]["score_integrity"] != "config_changed"
