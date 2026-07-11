"""The optional label-suggestion assistant abstains on uncertainty and never
carries authority (plan §12 / §21: never an auto-label)."""
from hotato.fleet import suggest as S


def test_abstains_on_uncertainty():
    for kw in ({"input_health": "caution"}, {"channel_mapping": "suspect"},
               {"locale": "xx-yy"}):
        r = S.suggest({"overlap_sec": 0.6}, **kw)
        assert r["suggestion"] == "abstain"
        assert r["reason_for_abstention"]
        assert r["confidence"] == 0.0


def test_boundary_sensitive_abstains():
    r = S.suggest({"overlap_sec": 0.6, "boundary_sensitive": True}, input_health="clean")
    assert r["suggestion"] == "abstain"


def test_suggests_yield_on_long_overlap_but_stays_advisory():
    r = S.suggest({"overlap_sec": 0.7}, input_health="clean", channel_mapping="confirmed")
    assert r["suggestion"] == "yield"
    assert 0.0 < r["confidence"] < 1.0
    assert "advisory only" in r["authority"]
    assert r["contradicting_observations"]  # never one-sided


def test_ambiguous_overlap_abstains():
    r = S.suggest({"overlap_sec": 0.3}, input_health="clean")
    assert r["suggestion"] == "abstain"


def test_always_has_the_full_suggestion_shape():
    r = S.suggest({"overlap_sec": 0.7}, input_health="clean")
    for k in ("suggestion", "confidence", "model_id", "model_hash", "feature_version",
              "locale", "supporting_observations", "contradicting_observations",
              "reason_for_abstention", "authority"):
        assert k in r
