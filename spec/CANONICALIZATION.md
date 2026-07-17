# Canonical bytes and content addressing, as implemented

Every digest in the format is sha256 over deterministic bytes. This file
states the exact byte rules and names the function that implements each
one, so an independent implementation can reproduce every address.

## 1. Canonical JSON

The canonical form of a JSON value is Python's `json.dumps` with:

```python
json.dumps(obj, sort_keys=True, separators=(",", ":"),
           ensure_ascii=True, allow_nan=False)
```

That is: keys sorted lexicographically at every level, no insignificant
whitespace, non-ASCII escaped to `\uXXXX`, and NaN / Infinity rejected
outright so a digest is never taken over a value that cannot round-trip
through a standard JSON reader.

Implemented, identically, in:

- `hotato.manifest.canonical_json` (`src/hotato/manifest.py`)
- `hotato.attest.canonical_json` (`src/hotato/attest.py`)

Two deliberate variants exist and are used only where stated:

- `hotato.counterexample.model.canonical_json`
  (`src/hotato/counterexample/model.py`): the same rules plus a trailing
  newline (`"\n"`); `digest_obj` hashes that string as UTF-8 and
  `prefixed_digest` prepends `"sha256:"`. Used for counterexample capsules.
- `hotato.failure_record.canonical_identity_bytes`
  (`src/hotato/failure_record.py`): sorted keys and compact separators,
  but `ensure_ascii=False` (UTF-8 text bytes), with the `record_id` field
  removed before serialization so a record's address is not an input to
  itself. `hotato.failure_record.digest_bytes` returns
  `"sha256:" + hexdigest`. Used for failure-record content addresses
  (`compute_record_id`).

## 2. The contract's canonical semantic digest

`hotato.attest.canonical_contract_digest` (`src/hotato/attest.py`) hashes a
contract's semantic identity, not its full body. The digest subject is a
flat object with exactly these keys (sorted, then serialized with the
canonical JSON of section 1 and hashed as UTF-8):

```
contract_schema, kind,
label.expected_behavior, label.label_source, label.label_revision,
label.rationale,
policy                       (the whole policy object),
source.source_audio_sha256, source.decoded_pcm_sha256,
source.bundle_audio_sha256, source.bundle_pcm_sha256,
source.recording_type, source.channels,
scorer.package_version, scorer.config_marker,
identity.created_by, identity.creator, identity.reviewer,
created_at, repo_commit
```

The embedded `attestation` block is never a digest input, so recomputing
the digest over an attested contract is stable and non-circular. The digest
is embedded at creation (`hotato.attest.embed_attestation`) and recomputed
at verify and unpack time (`hotato.attest.assess_contract`,
`hotato.attest.verify_attestation`); a mismatch is reported `tampered` and
refused. An optional detached `attestation.json` covers the digest with
HMAC-SHA256 (`hotato.attest.sign`); an unsigned bundle is always reported
`unsigned` or `unattested`, never `authenticated`.

## 3. File and audio identity

- **Raw file bytes**: `hotato.contract._sha256_file`
  (`src/hotato/contract.py`) streams the file in 1 MiB chunks into sha256
  and returns the hex digest.
- **Decoded PCM identity**: `hotato.contract._decoded_pcm_sha256` hashes
  the decoded PCM frames of a WAV (all channels, interleaved, read via the
  stdlib `wave` module in 65536-frame chunks), independent of container
  framing. Both hashes are bound into the contract (`source.
  bundle_audio_sha256`, `source.bundle_pcm_sha256`) and into the semantic
  digest of section 2, so replacing the bundled audio after creation is
  detectable even when the replacement re-encodes to different raw bytes.
- **Caller+agent mono pairs**: `hotato.contract._sha256_two_files` hashes
  the ASCII hex digest of the caller file, then the agent file, into one
  sha256 (order-stable), producing `source.source_audio_sha256` for
  two-file sources.

## 4. The packed bundle manifest

`hotato contract pack` (`hotato.contract.pack_contract`,
`src/hotato/contract.py`) writes `MANIFEST.sha256.json` as the first member
of the `.hotato` archive: a JSON object mapping every member's
forward-slash relative path to its raw-bytes sha256 (section 3), serialized
with `json.dumps(manifest, indent=2, sort_keys=True)` plus a trailing
newline. The archive itself is deterministic: members are added in sorted
order with a fixed timestamp (`date_time=(1980, 1, 1, 0, 0, 0)`), mode
0644, `create_system=3` (Unix), `ZIP_DEFLATED` at `compresslevel=6`.
Symlinks are refused before packing (`hotato.contract._iter_bundle_files`).
`hotato contract unpack` re-verifies every extracted member against the
manifest and refuses undeclared, duplicate, traversing, or oversized
members; the manifest proves internal consistency, and the semantic digest
of section 2 proves the body was not rewritten around it.

## 5. Bench content addresses

`hotato bench` (`src/hotato/bench.py`) applies the same rules to the frozen
batteries and their results:

- **Suite content hash** (`hotato.bench.suite_content_hash`): one line per
  consumed file, `<relative_name>\0<file_sha256_hex>\n`, over the sorted
  scenario JSONs and their `<id>.example.wav` recordings, hashed with
  sha256 and prefixed `"sha256:"`. This is the pin that freezes a battery.
- **Result content hash** (`hotato.bench.result_content_hash`): the
  canonical JSON of section 1 (`hotato.manifest.canonical_json`) over the
  result body with the `content_hash` field removed, hashed as UTF-8 and
  prefixed `"sha256:"`. `hotato bench verify` re-executes the pinned
  battery and compares the two addresses; the verdict is the hash
  comparison.
