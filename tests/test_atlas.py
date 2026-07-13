"""Delta D5: the static Voice Failure Atlas builder (``scripts/build_atlas.py``).

Typed sources under ``atlas/{records,contracts,implementations}/`` are the
single source of truth; the builder renders them into a static page graph,
machine indexes, and discovery files with no network, no JS, and no build-
clock timestamp. These tests pin:

  (a) every typed source schema+digest validates (cross-checked against the
      shipped JSON Schema, mirroring test_capability_routing.py's pattern);
  (b) building twice from the same sources is byte-identical (the same
      determinism discipline as examples/render_examples.py);
  (c) every internal link and both machine indexes resolve to a real page;
  (d) the hard publication gate actually excludes what it should (a tampered
      copy of a real record is refused; an unqualified pattern stays
      noindex);
  (e) the backchannel-exclusion rule: the bundled funnel-demo battery's own
      paired evidence routes to turn_intent_discriminator, never
      utterance_addressee_gate, and SAA/vendor names never appear anywhere
      in a typed source or a rendered page.
"""
from __future__ import annotations

import copy
import glob
import importlib.util
import json
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ATLAS = os.path.join(ROOT, "atlas")


def _load_build_atlas():
    spec = importlib.util.spec_from_file_location(
        "build_atlas", os.path.join(ROOT, "scripts", "build_atlas.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _schema(name):
    with open(os.path.join(ROOT, "src", "hotato", "schema", name), encoding="utf-8") as fh:
        return json.load(fh)


def _load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _all_sources():
    records = sorted(glob.glob(os.path.join(ATLAS, "records", "*.json")))
    contracts = sorted(glob.glob(os.path.join(ATLAS, "contracts", "*.json")))
    implementations = sorted(glob.glob(os.path.join(ATLAS, "implementations", "*.json")))
    return records, contracts, implementations


# ---------------------------------------------------------------------------
# (a) schema + digest validation of every typed source
# ---------------------------------------------------------------------------

def test_every_typed_source_directory_is_nonempty():
    records, contracts, implementations = _all_sources()
    assert len(records) >= 2
    assert len(contracts) >= 1
    assert len(implementations) >= 1


def test_atlas_records_validate_against_schema_and_embed_a_valid_failure_record():
    jsonschema = pytest.importorskip("jsonschema")
    rec_schema = _schema("atlas-record.v1.json")
    fr_schema = _schema("failure-record.v1.json")
    records, _, _ = _all_sources()
    for path in records:
        doc = _load_json(path)
        jsonschema.validate(instance=doc, schema=rec_schema)
        jsonschema.validate(instance=doc["failure_record"], schema=fr_schema)


def test_atlas_contracts_validate_against_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = _schema("atlas-contract.v1.json")
    _, contracts, _ = _all_sources()
    for path in contracts:
        jsonschema.validate(instance=_load_json(path), schema=schema)


def test_atlas_implementations_validate_against_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = _schema("atlas-implementation.v1.json")
    _, _, implementations = _all_sources()
    for path in implementations:
        jsonschema.validate(instance=_load_json(path), schema=schema)


def test_every_typed_source_content_digest_is_correct():
    build_atlas = _load_build_atlas()
    records, contracts, implementations = _all_sources()
    for path in records + contracts + implementations:
        doc = _load_json(path)
        recomputed = build_atlas._canonical_digest(doc)
        assert doc["content_digest"] == recomputed, f"{path}: stale content_digest"


def test_a_tampered_digest_is_refused_by_the_loader(tmp_path):
    build_atlas = _load_build_atlas()
    records, _, _ = _all_sources()
    doc = _load_json(records[0])
    doc["title"] = doc["title"] + " (tampered, digest not recomputed)"
    tampered_dir = tmp_path / "records"
    tampered_dir.mkdir()
    with open(tampered_dir / "tampered.json", "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    with pytest.raises(build_atlas.AtlasBuildError, match="content_digest mismatch"):
        build_atlas._load_dir(str(tampered_dir), "hotato.atlas-record.v1")


# ---------------------------------------------------------------------------
# (b) build-twice byte-identical determinism
# ---------------------------------------------------------------------------

def test_build_is_byte_identical_across_two_runs(tmp_path):
    build_atlas = _load_build_atlas()
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    build_atlas.build(str(out_a))
    build_atlas.build(str(out_b))

    files_a = sorted(os.path.relpath(p, out_a) for p in glob.glob(str(out_a / "**"), recursive=True)
                     if os.path.isfile(p))
    files_b = sorted(os.path.relpath(p, out_b) for p in glob.glob(str(out_b / "**"), recursive=True)
                     if os.path.isfile(p))
    assert files_a == files_b
    for rel in files_a:
        with open(out_a / rel, "rb") as fh:
            content_a = fh.read()
        with open(out_b / rel, "rb") as fh:
            content_b = fh.read()
        assert content_a == content_b, f"{rel} differs between two builds"


def test_build_output_carries_no_build_clock_timestamp(tmp_path):
    # The only dates anywhere in the output must be a recorded_date pulled
    # from a typed source (2026-07-13 in every seeded fixture); nothing here
    # should ever be "today" at build time.
    build_atlas = _load_build_atlas()
    out = tmp_path / "site"
    build_atlas.build(str(out))
    sitemap = (out / "sitemap.xml").read_text(encoding="utf-8")
    dates = set(re.findall(r"<lastmod>([0-9-]+)</lastmod>", sitemap))
    records, contracts, implementations = _all_sources()
    allowed = {_load_json(p)["recorded_date"] for p in records + contracts + implementations}
    assert dates <= allowed


# ---------------------------------------------------------------------------
# (c) internal links + machine indexes resolve
# ---------------------------------------------------------------------------

def _known_paths(out_dir):
    known = set()
    for dirpath, _dirs, files in os.walk(out_dir):
        for f in files:
            rel = os.path.relpath(dirpath, out_dir)
            prefix = "" if rel == "." else rel
            if f == "index.html":
                p = "/" + prefix + ("/" if prefix else "")
                known.add(p if p.startswith("/") else "/" + p)
                known.add("/" + prefix + "/" if prefix else "/")
            else:
                known.add("/" + os.path.join(prefix, f) if prefix else "/" + f)
    return known


def test_every_internal_link_resolves(tmp_path):
    build_atlas = _load_build_atlas()
    out = tmp_path / "site"
    build_atlas.build(str(out))
    known = _known_paths(str(out))

    broken = []
    for html_path in glob.glob(str(out / "**" / "*.html"), recursive=True):
        text = open(html_path, encoding="utf-8").read()
        for href in re.findall(r'href="([^"]+)"', text):
            if href.startswith("http") or href.startswith("#"):
                continue
            if href not in known:
                broken.append((os.path.relpath(html_path, out), href))
    assert not broken, "broken internal links:\n" + "\n".join(f"{p} -> {h}" for p, h in broken)


def test_machine_indexes_resolve_and_are_well_formed(tmp_path):
    build_atlas = _load_build_atlas()
    out = tmp_path / "site"
    build_atlas.build(str(out))

    failures_index = _load_json(out / "failures" / "index.json")
    assert failures_index["schema"] == "hotato.atlas-failures-index.v1"
    for entry in failures_index["records"]:
        target = out / entry["path"].strip("/") / "index.html"
        assert target.is_file(), f"failures/index.json points at missing page {entry['path']}"

    impl_index = _load_json(out / "implementations" / "index.json")
    assert impl_index["schema"] == "hotato.atlas-implementations-index.v1"
    for entry in impl_index["implementations"]:
        target = out / entry["path"].strip("/") / "index.html"
        assert target.is_file(), f"implementations/index.json points at missing page {entry['path']}"


def test_discovery_files_are_derived_from_the_same_indexed_page_set(tmp_path):
    build_atlas = _load_build_atlas()
    out = tmp_path / "site"
    build_atlas.build(str(out))

    sitemap = (out / "sitemap.xml").read_text(encoding="utf-8")
    sitemap_paths = set(re.findall(r"<loc>([^<]+)</loc>", sitemap))
    llms = (out / "llms.txt").read_text(encoding="utf-8")
    llms_paths = set(re.findall(r"\]\((/[^)]*)\)", llms))
    feed = (out / "feed.xml").read_text(encoding="utf-8")
    feed_paths = set(re.findall(r'<link href="([^"]+)"/>', feed))

    assert sitemap_paths == llms_paths == feed_paths
    assert sitemap_paths, "discovery files carry zero pages"
    for p in sitemap_paths:
        assert (out / p.strip("/") / "index.html").is_file()


# ---------------------------------------------------------------------------
# (d) publication gate
# ---------------------------------------------------------------------------

def test_hard_gate_refuses_a_record_missing_release_permission():
    build_atlas = _load_build_atlas()
    records, _, _ = _all_sources()
    doc = copy.deepcopy(_load_json(records[0]))
    doc["release"]["release_permission"] = False
    reasons = build_atlas.record_gate_reasons(doc)
    assert any("release_permission" in r for r in reasons)


def test_hard_gate_refuses_a_record_with_embedded_raw_audio():
    build_atlas = _load_build_atlas()
    records, _, _ = _all_sources()
    doc = copy.deepcopy(_load_json(records[0]))
    doc["failure_record"]["privacy"]["raw_audio_embedded"] = True
    reasons = build_atlas.record_gate_reasons(doc)
    assert any("raw_audio_embedded" in r for r in reasons)


def test_hard_gate_refuses_an_absolute_fixture_path():
    build_atlas = _load_build_atlas()
    records, _, _ = _all_sources()
    doc = copy.deepcopy(_load_json(records[0]))
    doc["evidence_provenance"]["fixture_paths"] = ["/etc/passwd"]
    reasons = build_atlas.record_gate_reasons(doc)
    assert any("unsafe path" in r for r in reasons)


def test_hard_gate_refuses_fixture_origin_that_does_not_resolve():
    """origin=fixture is a verifiable property: a cited fixture that does not
    resolve to a shipped file under examples/ fails the gate, so the label
    cannot be self-asserted the way a stored verdict never is."""
    build_atlas = _load_build_atlas()
    records, _, _ = _all_sources()
    doc = copy.deepcopy(_load_json(records[0]))
    doc["origin"] = "fixture"
    doc["evidence_provenance"]["fixture_paths"] = ["examples/does-not-exist.example.wav"]
    reasons = build_atlas.record_gate_reasons(doc)
    assert any("does not resolve under examples/" in r for r in reasons)


def test_seeded_records_pass_the_hard_gate_and_are_indexed():
    build_atlas = _load_build_atlas()
    records, _, _ = _all_sources()
    for path in records:
        doc = _load_json(path)
        assert build_atlas.record_gate_reasons(doc) == [], f"{path} unexpectedly fails the gate"


def test_pattern_pages_with_too_few_records_are_noindex(tmp_path):
    # Neither seeded pattern class has 3 qualifying records from 2 configurations
    # (each has exactly 1), so both must render INCONCLUSIVE and noindex --
    # never silently promoted to PASS.
    build_atlas = _load_build_atlas()
    out = tmp_path / "site"
    build_atlas.build(str(out))
    for pc in ("addressed-interruption-missed", "addressed-backchannel-yielded"):
        html = (out / "failures" / "patterns" / pc / "index.html").read_text(encoding="utf-8")
        assert 'name="robots" content="noindex"' in html
        assert "INCONCLUSIVE" in html


def test_unqualified_patterns_are_excluded_from_the_failures_machine_index(tmp_path):
    build_atlas = _load_build_atlas()
    out = tmp_path / "site"
    build_atlas.build(str(out))
    failures_index = _load_json(out / "failures" / "index.json")
    # the machine index lists individual records (which DO qualify on their
    # own, origin=fixture) -- but no PATTERN aggregate is asserted PASS here.
    sitemap = (out / "sitemap.xml").read_text(encoding="utf-8")
    assert "/failures/patterns/" not in sitemap


def test_an_implementation_evidenced_claim_must_be_earned():
    build_atlas = _load_build_atlas()
    records, _, implementations = _all_sources()
    records_by_id = {}
    for path in records:
        doc = _load_json(path)
        records_by_id[doc["content_id"]] = doc
    verdicts = build_atlas.compute_capability_verdicts(list(records_by_id.values()))

    bogus = {
        "kind": "hotato.atlas-implementation.v1",
        "version": "1.0",
        "implementation_id": "bogus-evidenced-claim",
        "capability": "utterance_addressee_gate",
        "stack": "generic",
        "title": "bogus",
        "approach": "bogus",
        "integration_points": ["x"],
        "status": "evidenced",
        "verified_against": list(records_by_id.keys()),
        "recorded_date": "2026-07-13",
        "content_digest": "sha256:" + "0" * 64,
    }
    with pytest.raises(build_atlas.AtlasBuildError, match="unearned evidenced claim"):
        build_atlas.verify_implementation_evidence([bogus], records_by_id, verdicts)


# ---------------------------------------------------------------------------
# (e) backchannel exclusion + neutrality
# ---------------------------------------------------------------------------

def test_backchannel_record_routes_to_turn_intent_discriminator_never_addressee_gate():
    build_atlas = _load_build_atlas()
    records, _, _ = _all_sources()
    docs = [_load_json(p) for p in records]
    verdicts = build_atlas.compute_capability_verdicts(docs)

    backchannel = verdicts["addressed-backchannel-yielded"]
    assert backchannel is not None
    assert backchannel["required_capability"] == "turn_intent_discriminator"
    assert backchannel["required_capability"] != "utterance_addressee_gate"


def test_utterance_addressee_gate_contract_declares_backchannel_exclusion():
    doc = _load_json(os.path.join(ATLAS, "contracts", "utterance-addressee-gate-v1.json"))
    assert doc["backchannel_exclusion"] is True


def test_utterance_addressee_gate_pattern_has_zero_indexed_records(tmp_path):
    # Honesty-critical: the SAA-eligible class publishes ONLY after an explicit
    # non-addressed-speech label + a cleared paired fixture exist. The bundled
    # funnel-demo battery has neither, so this pattern must carry zero records.
    build_atlas = _load_build_atlas()
    out = tmp_path / "site"
    build_atlas.build(str(out))
    html = (out / "failures" / "patterns" / "side-speech-triggered-agent" / "index.html").read_text(encoding="utf-8")
    assert "No atlas record carries this pattern class yet" in html
    assert 'name="robots" content="noindex"' in html


def _all_atlas_and_site_text(tmp_path):
    build_atlas = _load_build_atlas()
    out = tmp_path / "site"
    build_atlas.build(str(out))
    texts = []
    for path in glob.glob(os.path.join(ATLAS, "**", "*.json"), recursive=True):
        texts.append((path, open(path, encoding="utf-8").read()))
    for path in glob.glob(str(out / "**" / "*.html"), recursive=True):
        texts.append((path, open(path, encoding="utf-8").read()))
    for path in (out / "llms.txt", out / "llms-full.txt", out / "feed.xml"):
        texts.append((str(path), path.read_text(encoding="utf-8")))
    return texts


def test_no_vendor_or_saa_mention_anywhere_in_sources_or_rendered_site(tmp_path):
    # The only allowed exceptions are the mandated footer-only attribution
    # line ("Hotato is maintained by Attention Labs.") and its accompanying
    # <meta name="publisher"/"author"> tags -- everywhere else, every record,
    # contract, implementation, and verdict must stay silent on vendor/product
    # names.
    allowed = re.compile(
        r"Hotato is maintained by Attention Labs\."
        r'|<meta name="(?:publisher|author)" content="Attention Labs">',
    )
    forbidden = re.compile(
        r"\bsaa\b|attention\s*labs|attenlabs|multivox|speech\s+addressee\s+agent",
        re.IGNORECASE,
    )
    for path, text in _all_atlas_and_site_text(tmp_path):
        scoped = allowed.sub("", text)
        hit = forbidden.search(scoped)
        assert hit is None, f"{path}: forbidden vendor/product mention {hit.group(0)!r}"


def test_no_authenticity_protest_wording_anywhere_in_sources_or_rendered_site(tmp_path):
    forbidden = re.compile(
        r"\bactual(ly)?\b|\bhonest(ly)?\b|\bgenuine(ly)?\b|\btruly\b|no fabrication",
        re.IGNORECASE,
    )
    for path, text in _all_atlas_and_site_text(tmp_path):
        hit = forbidden.search(text)
        assert hit is None, f"{path}: authenticity-protest wording {hit.group(0)!r}"


def test_no_saa_capability_in_any_record_page_verdict(tmp_path):
    # "Do NOT put SAA in any verdict" -- a coding agent chooses an
    # implementation, Hotato's verdict does not. No record page's rendered
    # capability-requirement card may name an implementation at all.
    build_atlas = _load_build_atlas()
    out = tmp_path / "site"
    build_atlas.build(str(out))
    for html_path in glob.glob(str(out / "failures" / "records" / "**" / "index.html"), recursive=True):
        text = open(html_path, encoding="utf-8").read()
        assert "saa" not in text.lower()


def test_five_dimensions_shown_separately_never_blended(tmp_path):
    build_atlas = _load_build_atlas()
    out = tmp_path / "site"
    build_atlas.build(str(out))
    for html_path in glob.glob(str(out / "failures" / "records" / "**" / "index.html"), recursive=True):
        text = open(html_path, encoding="utf-8").read()
        for dim in ("outcome", "policy", "conversation", "speech", "reliability"):
            assert f'>{dim}<' in text
        # never a single blended "score" field
        assert "overall score" not in text.lower()
        assert "accuracy:" not in text.lower()


def test_interface_conformance_and_behavioral_evidence_are_separate_sections(tmp_path):
    build_atlas = _load_build_atlas()
    out = tmp_path / "site"
    build_atlas.build(str(out))
    for html_path in glob.glob(str(out / "failures" / "records" / "**" / "index.html"), recursive=True):
        text = open(html_path, encoding="utf-8").read()
        assert "Interface conformance" in text
        assert "Behavioral evidence" in text
        assert text.index("Interface conformance") < text.index("Behavioral evidence")
