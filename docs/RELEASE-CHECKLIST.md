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
- [ ] Publish to PyPI.
- [ ] Verify `uvx hotato demo` works from the published package (fresh machine or cleared uv cache): it renders and opens the failing demo report.
- [ ] Verify `uvx hotato doctor` (self-test path) works from the published package.
- [ ] Tag the release commit and push the tag; GitHub release notes point at the CHANGELOG entry.

## After release

- [ ] Smoke-test the README quickstart commands exactly as written.
- [ ] File follow-ups for anything that needed a manual touch, so the next release is one pass.
