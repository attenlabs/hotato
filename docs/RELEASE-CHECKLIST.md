# Release checklist

Run top to bottom for every release. Each item is a gate: green means proceed.

## Code and tests

- [ ] `python3 -m pytest -q` fully green on a clean checkout.
- [ ] `python3 sync_engine.py --check` passes: the vendored `_engine` is byte-identical to upstream.
- [ ] CI is green on the release commit (`.github/workflows/tests.yml`).
- [ ] GitHub Actions stay SHA-pinned: every `uses:` in `.github/workflows/*.yml` references an action by full 40-char commit SHA with a trailing `# vX.Y.Z` comment (never a mutable `@v4` tag or `@release/v1` branch). When bumping an action, update the SHA and the comment together; do not revert to a floating tag.
- [ ] Version bumped in EVERY lockstep site: `pyproject.toml`, `src/hotato/__init__.py` (`__version__` -- this is what `hotato --version`, `hotato describe`, and stackbench provenance self-report), `server.json` (both `version` fields), `CITATION.cff` (`version` + `date-released`), the `llms.txt` version line; `CHANGELOG.md` has a dated entry for it. `tests/test_version_lockstep.py` gates the ones tests can see -- it now parses and compares `pyproject.toml`, `__init__.py`, the `describe` manifest, installed dist metadata, `llms.txt`'s `Version` line, `server.json` (top-level + each package), and `CITATION.cff`'s `version:`, so a missed bump in any of those reddens CI.

## README and assets

- [ ] Regenerate README assets before release: `python3 scripts/render_readme_assets.py`.
- [ ] Verify the README screenshot renders: `docs/assets/hotato-demo-report.png` is current, shows the failing demo summary, a timeline, and a fix card, and displays correctly in the GitHub README preview.
- [ ] README badges resolve (the `tests.yml` workflow badge reflects the real run).
- [ ] Verify the site hero matches the README: same tagline, same demo screenshot, same install command on hotato.dev.

## Adapters

- [ ] Adapter last-verified dates are fresh in `docs/ADAPTER-STATUS.md`: re-verify each capture path against the vendor's current API docs and update the date column.
- [ ] Fixmap knob names match the same verified APIs (`src/hotato/fixmap.py`).

## Package

- [ ] Build sdist and wheel from a clean tree; install the wheel in a fresh venv and run `hotato run --suite barge-in`. CI does this on every version tag in the `release.yml` `sanity` job.
- [ ] Generate + validate the SBOM(s) and attach them to the GitHub release: `python3 scripts/gen_sbom.py` writes `dist/hotato.sbom.cdx.json` (a minimal CycloneDX bill of materials for the core package and every declared dependency, generated offline straight from `pyproject.toml` -- no network, no pip). For a per-profile breakdown, `python3 scripts/gen_sbom.py --list-profiles` enumerates `core` plus each declared extra, and `python3 scripts/gen_sbom.py --profile <name>` writes `dist/hotato.sbom.<name>.cdx.json` (core alone, or core plus that one extra). Validate each with `python3 scripts/gen_sbom.py --check <file>`, then upload `dist/*.cdx.json` alongside the sdist/wheel on the GitHub release. CI produces and validates all of these automatically: `release.yml` uploads them in the `hotato-release-dist` artifact, and `publish-pypi-oidc.yml` uploads them in the `hotato-sbom` artifact.
- [ ] Publish to PyPI via **Trusted Publishing (OIDC)** -- the default path (see below): dispatch `publish-pypi-oidc.yml` with `version` = the release's `pyproject.toml` version and `confirm` = `PUBLISH`. That workflow builds reproducibly (a second build is rebuilt with a pinned backend and the two builds' `sha256sum`s are compared -- a mismatch fails the run), generates + validates the SBOMs, uploads the exact built artifacts, and then in a separate gated `publish` job downloads those exact bytes, attests build provenance over them, and uploads them to PyPI. No long-lived PyPI token is stored or used.
- [ ] Verify the build-provenance attestation for the published wheel and sdist: `gh attestation verify dist/hotato-<version>-py3-none-any.whl --repo attenlabs/hotato` (and again for `dist/hotato-<version>.tar.gz`). This checks the GitHub-signed provenance emitted by the `publish` job's `actions/attest-build-provenance` step, proving those exact bytes were built by this repo's workflow.
- [ ] Verify the published SBOM(s): re-run `python3 scripts/gen_sbom.py --check dist/hotato.sbom.cdx.json` (and each `dist/hotato.sbom.<profile>.cdx.json`) on the files attached to the release, and confirm each SBOM's `metadata.component.version` matches the release version.
- [ ] Verify `uvx hotato demo` works from the published package (fresh machine or cleared uv cache): it renders and opens the failing demo report.
- [ ] Verify `uvx hotato doctor` (self-test path) works from the published package.
- [ ] Tag the release commit and push the tag; GitHub release notes point at the CHANGELOG entry.

### Publishing path: PyPI Trusted Publishing (OIDC) -- DEFAULT

`.github/workflows/publish-pypi-oidc.yml` is the default publish path. It is a
`workflow_dispatch`-only workflow that builds (with a pinned, reproducible
backend), runs the full test suite, `twine check`s the artifacts, runs a
second-build `sha256sum` reproducibility check, generates + validates the
CycloneDX SBOMs, and publishes via
[PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/) using a
short-lived GitHub OIDC token (no stored API token). It requires a `version`
input matching `pyproject.toml` and a `confirm` input equal to `PUBLISH`; the
`publish` job runs under the `pypi` GitHub Environment with `id-token: write`
and `attestations: write` granted only on that job, and attests build
provenance over the exact artifacts before uploading them.

**One-time operator action** (required before this path can publish): register
the Trusted Publisher on PyPI for `attenlabs/hotato` + workflow
`publish-pypi-oidc.yml`. Until this is done, PyPI rejects the OIDC token and a
dispatch builds/tests/attests cleanly then fails at the upload step -- safe,
since there is no credential to leak and nothing is uploaded. **hotato is
already on PyPI**, so this is the existing-project flow (not the
pending-publisher flow for unpublished names):

1. Sign in to PyPI as an owner/maintainer of the `hotato` project and open
   `https://pypi.org/manage/project/hotato/settings/publishing/`.
2. Under "Add a new publisher", choose GitHub and fill in:
   - Owner: `attenlabs`
   - Repository name: `hotato`
   - Workflow filename: `publish-pypi-oidc.yml`
   - Environment name: `pypi`
3. Save. No token or secret is created or stored anywhere for this; PyPI will
   only accept OIDC tokens minted by that exact repo/workflow/environment
   combination.
4. In the GitHub repo, create the `pypi` environment (Settings > Environments
   > New environment, name `pypi`) if it does not already exist. Optionally
   add required reviewers there for a human-approval gate in addition to the
   `confirm`/`version` input checks already in the workflow.
5. To cut a release this way: dispatch `publish-pypi-oidc.yml` with `version`
   set to the release's `pyproject.toml` version and `confirm` set to
   `PUBLISH`.

### Fallback: manual token upload (twine)

If Trusted Publishing is unavailable (e.g. the publisher is not yet registered
and a release must go out), publish by hand with a project-scoped API token:
`python3 -m twine upload dist/*` using a token scoped to the `hotato` project.
This path uses a long-lived credential and produces no build-provenance
attestation, so prefer the OIDC path above; use twine only as a fallback and
still attach the generated `dist/*.cdx.json` SBOMs to the GitHub release.

## After release

- [ ] Smoke-test the README quickstart commands exactly as written.
- [ ] File follow-ups for anything that needed a manual touch, so the next release is one pass.
