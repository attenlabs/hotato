# Release checklist

Every gate to clear before a release ships, top to bottom: green means proceed.

## Code and tests

- [ ] `python3 -m pytest -q` fully green on a clean checkout.
- [ ] `python3 sync_engine.py --check` passes: the vendored `_engine` is byte-identical to upstream.
- [ ] CI is green on the release commit (`.github/workflows/tests.yml`).
- [ ] GitHub Actions stay SHA-pinned: every `uses:` in `.github/workflows/*.yml` references an action by full 40-char commit SHA with a trailing `# vX.Y.Z` comment, never a mutable `@v4` tag or `@release/v1` branch. Bump the SHA and the comment together; never revert to a floating tag. The same rule covers the root `action.yml` and `tests/fixtures/action-consumer/workflows/consumer.yml` (`tests/test_action_consumer.py` gates both).
- [ ] Root Action docs match the release: `docs/CI.md` ("The root Action" section) names v1.4.0 as the availability floor (the first release to ship `action.yml`, a fixed historical fact) and shows the CURRENT release as the copy-paste adoption example -- the `git ls-remote refs/tags/vX.Y.Z` resolve command, the `# vX.Y.Z` comment on the `attenlabs/hotato@<sha>` pin, and the `hotato==X.Y.Z` pip example all name the release being cut. `tests/test_version_lockstep.py::test_ci_md_adoption_example_pins_match_pyproject` gates those three; resolve the current tag with `git ls-remote` and confirm the documented command prints its SHA.
- [ ] Version bumped in EVERY lockstep site: `pyproject.toml`, `src/hotato/__init__.py` (`__version__` -- what `hotato --version`, `hotato describe`, and stackbench provenance self-report), `server.json` (both `version` fields), `CITATION.cff` (`version` + `date-released`), the `llms.txt` version line; `CHANGELOG.md` gets a dated entry. `tests/test_version_lockstep.py` gates every site tests can see -- it parses and compares `pyproject.toml`, `__init__.py`, the `describe` manifest, installed dist metadata, `llms.txt`'s `Version` line, `server.json` (top-level + each package), and `CITATION.cff`'s `version:`, so a missed bump anywhere reddens CI.

## README and assets

- [ ] Regenerate README assets before release: `python3 scripts/render_readme_assets.py`.
- [ ] Verify the README screenshot renders: `docs/assets/hotato-demo-report.png` is current, shows the failing demo summary, a timeline, and a fix card, and displays correctly in the GitHub README preview.
- [ ] README badges resolve (the `tests.yml` workflow badge reflects the current run).
- [ ] Verify the site hero matches the README: same tagline, same demo screenshot, same install command on hotato.dev.

## Adapters

- [ ] Adapter last-verified dates are fresh in `docs/ADAPTER-STATUS.md`: re-verify each capture path against the vendor's current API docs and update the date column.
- [ ] Fixmap knob names match the same verified APIs (`src/hotato/fixmap.py`).

## Package

- [ ] Build sdist and wheel from a clean tree; install the wheel in a fresh venv and run `hotato run --suite barge-in`. CI does this on every version tag in the `release.yml` `sanity` job.
- [ ] Generate and validate the SBOM(s), then attach them to the GitHub release: `python3 scripts/gen_sbom.py` writes `dist/hotato.sbom.cdx.json` -- a minimal CycloneDX bill of materials for the core package and every declared dependency, generated offline straight from `pyproject.toml`. For a per-profile breakdown, `python3 scripts/gen_sbom.py --list-profiles` enumerates `core` plus each declared extra, and `python3 scripts/gen_sbom.py --profile <name>` writes `dist/hotato.sbom.<name>.cdx.json` (core alone, or core plus that one extra). Validate each with `python3 scripts/gen_sbom.py --check <file>`, then upload `dist/*.cdx.json` alongside the sdist/wheel on the GitHub release. CI produces and validates all of these automatically: `release.yml` uploads them in the `hotato-release-dist` artifact, and `publish-pypi-oidc.yml` uploads them in the `hotato-sbom` artifact.
- [ ] Publish to PyPI via **Trusted Publishing (OIDC)**, the default path (see below): dispatch `publish-pypi-oidc.yml` with `version` = the release's `pyproject.toml` version and `confirm` = `PUBLISH`. That workflow builds reproducibly (a second build, made with a pinned backend, has its `sha256sum` compared against the first -- a mismatch fails the run), generates and validates the SBOMs, and uploads the exact built artifacts; a separate gated `publish` job then downloads those exact bytes, attests build provenance over them, and uploads them to PyPI, authenticating with a short-lived OIDC token minted fresh each run.
- [ ] Verify the build-provenance attestation for the published wheel and sdist: `gh attestation verify dist/hotato-<version>-py3-none-any.whl --repo attenlabs/hotato` (and again for `dist/hotato-<version>.tar.gz`). This checks the GitHub-signed provenance emitted by the `publish` job's `actions/attest-build-provenance` step, confirming those exact bytes were built by this repo's workflow.
- [ ] Verify the published SBOM(s): re-run `python3 scripts/gen_sbom.py --check dist/hotato.sbom.cdx.json` (and each `dist/hotato.sbom.<profile>.cdx.json`) on the files attached to the release, and confirm each SBOM's `metadata.component.version` matches the release version.
- [ ] Verify `uvx hotato demo` works from the published package (fresh machine or cleared uv cache): it renders and opens the failing demo report.
- [ ] Verify `uvx hotato doctor` (self-test path) works from the published package.
- [ ] Tag the release commit and push the tag; GitHub release notes point at the CHANGELOG entry.

### Publishing path: PyPI Trusted Publishing (OIDC) -- DEFAULT

`.github/workflows/publish-pypi-oidc.yml` is the default publish path: a
`workflow_dispatch`-only workflow that builds with a pinned, reproducible
backend, runs the full test suite, `twine check`s the artifacts, runs a
second-build `sha256sum` reproducibility check, generates and validates the
CycloneDX SBOMs, and publishes via
[PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/) using a
short-lived GitHub OIDC token minted fresh each run. It requires a `version`
input matching `pyproject.toml` and a `confirm` input equal to `PUBLISH`; the
`publish` job runs under the `pypi` GitHub Environment with `id-token: write`
and `attestations: write` granted only on that job, and attests build
provenance over the exact artifacts before uploading them.

**One-time operator action** (required before this path can publish): register
the Trusted Publisher on PyPI for `attenlabs/hotato` + workflow
`publish-pypi-oidc.yml`. Until then, PyPI rejects the OIDC token, so a
dispatch builds, tests, and attests cleanly and stops at the upload step --
safe by construction, since the workflow never holds a credential that could
leak. **hotato is already on PyPI**, so this is the existing-project flow,
distinct from the pending-publisher flow for unpublished names:

1. Sign in to PyPI as an owner/maintainer of the `hotato` project and open
   `https://pypi.org/manage/project/hotato/settings/publishing/`.
2. Under "Add a new publisher", choose GitHub and fill in:
   - Owner: `attenlabs`
   - Repository name: `hotato`
   - Workflow filename: `publish-pypi-oidc.yml`
   - Environment name: `pypi`
3. Save. Nothing is stored on either side; PyPI accepts only OIDC tokens
   minted by that exact repo/workflow/environment combination.
4. In the GitHub repo, create the `pypi` environment (Settings > Environments
   > New environment, name `pypi`) if it does not already exist. Optionally
   add required reviewers there for a human-approval gate on top of the
   `confirm`/`version` input checks already in the workflow.
5. To cut a release this way: dispatch `publish-pypi-oidc.yml` with `version`
   set to the release's `pyproject.toml` version and `confirm` set to
   `PUBLISH`.

### Fallback: manual token upload (twine)

When Trusted Publishing isn't available (the publisher isn't registered yet
and a release still has to go out), publish by hand with a project-scoped
API token. Build the SAME way the OIDC path does -- a pinned backend plus
`SOURCE_DATE_EPOCH` from the release commit -- so the hand-uploaded wheel is
byte-reproducible instead of carrying wall-clock ZIP timestamps no rebuild
can match:

```bash
python3 -m pip install "build==1.2.2.post1" "setuptools==83.0.0" "wheel==0.45.1" "twine"
SOURCE_DATE_EPOCH="$(git log -1 --pretty=%ct)" python3 -m build --no-isolation
python3 -m twine check dist/*
python3 -m twine upload dist/*    # token scoped to the `hotato` project
```

This path uses a long-lived credential and skips build-provenance
attestation. Prefer the OIDC path above; treat twine as the fallback, and
still attach the generated `dist/*.cdx.json` SBOMs to the GitHub release.

**Reproducibility scope (what holds).** The wheel is byte-for-byte
reproducible ONLY when built with the exact backend pins above and
`SOURCE_DATE_EPOCH`; both publish paths do this, and the OIDC workflow rebuilds
a second time and compares the wheel's raw `sha256sum` to prove it. The sdist
is content-reproducible, not byte-reproducible: setuptools does not normalize
the gzip mtime or the tar member mtimes, so its `.tar.gz` bytes vary
run-to-run even at a fixed `SOURCE_DATE_EPOCH`. What stays stable for the
sdist is its CONTENT (member names, modes, and file bytes), which the
workflow compares instead -- a changed or injected file still fails,
timestamp noise does not. Describe the sdist `.tar.gz` as content-reproducible,
not byte-reproducible.

## After release

- [ ] Smoke-test the README quickstart commands exactly as written.
- [ ] File follow-ups for anything that needed a manual touch, so the next release is one pass.
