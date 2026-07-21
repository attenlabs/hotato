"""Percentile math is pinned on a known set of values: dist_summary's mean /
median / p90 / p95 are hand-checkable, the nearest-rank fleet percentiles
(nearest_rank / corpus_percentiles) are pinned with their null exclusion, and
the latency SLA gate (latency_sla) is exercised over its three states: not
configured, pass, fail.
"""

from hotato._stats import (corpus_percentiles, dist_summary, latency_sla,
                           nearest_rank, percentile)


def test_percentile_linear_interpolation_on_known_values():
    v = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    # pos = 0.9 * 9 = 8.1 -> v[8] + 0.1*(v[9]-v[8]) = 0.9 + 0.1*0.1 = 0.91
    assert round(percentile(v, 0.90), 6) == 0.91
    # pos = 0.95 * 9 = 8.55 -> v[8] + 0.55*(v[9]-v[8]) = 0.9 + 0.55*0.1 = 0.955
    assert round(percentile(v, 0.95), 6) == 0.955


def test_percentile_empty_is_none():
    assert percentile([], 0.9) is None


def test_dist_summary_known_response_gap_values():
    # a known set of 6 dead-air measurements (dead air before the agent speaks)
    v = [0.2, 0.3, 0.4, 0.5, 0.6, 0.7]
    d = dist_summary(v)
    assert d == {
        "n": 6,
        "min": 0.2,
        "mean": 0.45,
        "median": 0.45,
        "p90": 0.65,
        "p95": 0.675,
        "max": 0.7,
    }


def test_dist_summary_empty_is_none():
    assert dist_summary([]) is None


def test_nearest_rank_on_known_values():
    v = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    # rank = ceil(0.5 * 10) = 5 -> v[4] = 0.5
    assert nearest_rank(v, 0.50) == 0.5
    # rank = ceil(0.9 * 10) = 9 -> v[8] = 0.9
    assert nearest_rank(v, 0.90) == 0.9
    # rank = ceil(0.99 * 10) = 10 -> v[9] = 1.0
    assert nearest_rank(v, 0.99) == 1.0


def test_nearest_rank_is_always_an_observed_value():
    v = [0.2, 0.3, 0.4, 0.5, 0.6, 0.7]
    for q in (0.50, 0.90, 0.95, 0.99):
        assert nearest_rank(v, q) in v


def test_nearest_rank_empty_is_none():
    assert nearest_rank([], 0.5) is None


def test_corpus_percentiles_known_values_with_null_exclusion():
    # 6 measured of 8 candidate events: the 2 nulls are excluded and counted.
    # p50: rank = ceil(0.5*6) = 3 -> 0.4; p90: rank = ceil(0.9*6) = 6 -> 0.7;
    # p99: rank = ceil(0.99*6) = 6 -> 0.7
    p = corpus_percentiles([0.2, 0.3, 0.4, 0.5, 0.6, 0.7], total=8)
    assert p == {"method": "nearest-rank", "n": 6, "excluded_null": 2,
                 "p50": 0.4, "p90": 0.7, "p99": 0.7}


def test_corpus_percentiles_never_treats_null_as_zero():
    # nulls in the pooled values are excluded, not read as 0: with them the
    # p50 of [None, None, 0.5, 0.6] stays 0.5 (rank ceil(0.5*2)=1 of the 2
    # measured values), not the 0-padded answer.
    p = corpus_percentiles([None, None, 0.5, 0.6], total=4)
    assert p["n"] == 2 and p["excluded_null"] == 2
    assert p["p50"] == 0.5 and p["p90"] == 0.6 and p["p99"] == 0.6


def test_corpus_percentiles_nothing_measured_keeps_shape():
    p = corpus_percentiles([], total=4)
    assert p == {"method": "nearest-rank", "n": 0, "excluded_null": 4,
                 "p50": None, "p90": None, "p99": None}


def test_latency_sla_not_configured_never_fails():
    d = dist_summary([0.2, 0.3, 0.9])
    sla = latency_sla(d, None)
    assert sla == {"bound_sec": None, "observed_p95_sec": d["p95"], "passed": None}


def test_latency_sla_fails_over_bound():
    d = dist_summary([0.2, 0.3, 0.9])  # p95 well above 0.5
    sla = latency_sla(d, 0.5)
    assert sla["passed"] is False
    assert sla["observed_p95_sec"] == d["p95"]
    assert sla["bound_sec"] == 0.5


def test_latency_sla_passes_under_bound():
    d = dist_summary([0.2, 0.3, 0.4])
    sla = latency_sla(d, 10.0)
    assert sla["passed"] is True


def test_latency_sla_passes_at_exact_bound():
    d = dist_summary([0.5])
    sla = latency_sla(d, d["p95"])
    assert sla["passed"] is True


def test_latency_sla_no_measurements_with_bound_never_fails():
    sla = latency_sla(None, 0.5)
    assert sla == {"bound_sec": 0.5, "observed_p95_sec": None, "passed": True}
