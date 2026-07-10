# Release checklist

Run top to bottom for every release. Each item is a gate: green means proceed.

## Code and tests

- [ ] `python3 -m pytest -q` fully green on a clean checkout.
- [ ] `python3 sync_engine.py --check` passes: the vendored `_engine` is byte-identical to upstream.
- [ ] CI is green on the release commit (`.github/workflows/tests.yml`).
- [ ] Version bumped in EVERY lockstep site: `pyproject.toml`, `src/hotato/__init__.py` (`__version__` -- this is what `hotato --version`, `hotato describe`, and stackbench provenance self-report), `server.json` (both `version` fields), `CITATION.cff` (`version` + `date-released`), the `llms.txt` version line; `CHANGELOG.md` has a dated entry for it. `tests/test_version_lockstep.py` gates the ones tests can see.

## README and assets

- [ ] Regenerate README assets before release: `python3 scripts/render_readme_assets.py`.
- [ ] Verify the README screenshot renders: `docs/assets/hotato-demo-report.png` is current, shows the failing demo summary, a timeline, and a fix card, and displays correctly in the GitHub README preview.
- [ ] README badges resolve (the `tests.yml` workflow badge reflects the real run).
- [ ] Verify the site hero matches the README: same tagline, same demo screenshot, same install command on hotato.dev.

## Adapters

- [ ] Adapter last-verified dates are fresh in `docs/ADAPTER-STATUS.md`: re-verify each capture path against the vendor's current API docs and update the date column.
- [ ] Fixmap knob names match the same verified APIs (`src/hotato/fixmap.py`).

## Package

- [ ] Build sdist and wheel from a clean tree; install the wheel in a fresh venv and run `hotato run --suite barge-in`.
- [ ] Publish to PyPI. **Current mechanism: manual upload with an API token** (`twine upload dist/*` using a token scoped to the `hotato` project). This is the only working path today.
- [ ] Verify `uvx hotato demo` works from the published package (fresh machine or cleared uv cache): it renders and opens the failing demo report.
- [ ] Verify `uvx hotato doctor` (self-test path) works from the published package.
- [ ] Tag the release commit and push the tag; GitHub release notes point at the CHANGELOG entry.

### PyPI Trusted Publishing (OIDC) -- exists, not yet active

`.github/workflows/publish-pypi-oidc.yml` is a `workflow_dispatch`-only workflow
that builds, runs the full test suite, `twine check`s the artifacts, and
publishes via [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/)
(short-lived GitHub OIDC token, no stored API token). It requires a `version`
input matching `pyproject.toml` and a `confirm` input equal to `PUBLISH`, and
the publish step runs under the `pypi` GitHub Environment with `id-token:
write` granted only on that job.

It cannot publish anything yet: PyPI rejects the OIDC token until an operator
registers this exact repo + workflow + environment as a trusted publisher for
the `hotato` project. **hotato is already on PyPI**, so this is the existing-project
flow (not the pending-publisher flow for unpublished names). One-time operator
setup:

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
5. To cut a release this way: dispatch `publish-pypi-oidc.yml` with
   `version` set to the release's `pyproject.toml` version and `confirm` set
   to `PUBLISH`.

Until step 3 is done, dispatching the workflow will build and test cleanly
and then fail at the publish step with an OIDC trust error -- expected and
safe, since there is no credential to leak and nothing gets uploaded. The
manual token-based step above remains the release path until an operator
completes this setup and chooses to switch.

## After release

- [ ] Smoke-test the README quickstart commands exactly as written.
- [ ] File follow-ups for anything that needed a manual touch, so the next release is one pass.
