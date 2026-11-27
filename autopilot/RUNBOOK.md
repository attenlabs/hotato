# Hotato autopilot: runbook

**What this is.** The owner's guide to switching on, watching and stopping the autopilot: one self-running Google Cloud system that builds, maintains, checks and deploys the open-source project hotato (a free tool that checks recorded voice-agent calls for talk-over and slow yielding). Everything lives in `autopilot/`; paths below are relative to it, and commands run from the repository root. The owner's one-time setup and bootstrap scripts hold account details, so they are kept outside this repository.

## Stop everything with one command

```
printf stop | gcloud storage cp - gs://hotato-autopilot-state/flags/kill
```

Every job reads that flag first and exits. The budget function writes the same flag at the cap. To resume, delete the object: `gcloud storage rm gs://hotato-autopilot-state/flags/kill`. To also stop the schedules: `for s in atlas atlas-recheck atlas-audit atlas-watch atlas-wheel upkeep freshness builder simulate scorecard weekly-rebuild; do gcloud scheduler jobs pause hotato-$s --location us-central1; done`.

## Owner's one-time steps, in order

1. Run the one-time setup script in your terminal (creates the project, budgets, base APIs, `budget-alerts` topic, and the `github-bot-token` and `cloudflare-pages-token` secrets).
2. Copy `gates/CODEOWNERS.snippet` into `.github/CODEOWNERS` with your GitHub login. Turn on branch protection for `main`: require status checks `test`, `leak-scan`, `benchmark`, `copy-check`, require Code Owner review, no bypass for the bot.
3. Create the bot's fine-grained GitHub token (contents, pull requests, issues on the hotato repos only; no admin). Store it as `github-bot-token` if step 1 skipped it.
4. Nothing for you: the automation service account runs the bootstrap script with the alert address. It is safe to re-run and does the rest of the setup: the versioned Terraform state bucket `<project>-tfstate`, `infra/hotato.tfvars` (from `example.tfvars`, with the alert address), `terraform init` and `terraform apply`. Jobs stay in shadow mode.
5. Any later settings change: edit `infra/hotato.tfvars` and run the bootstrap script again (it also copies the settings to the state bucket, where the infra pipeline reads them).
5a. The monitor's bearer token is made by `bootstrap.sh` (one random version, only if none exists). Read it with `gcloud secrets versions access latest --secret serve-token` when you open the hosted workspace.
6. Add the three simulate secrets (`telephony-account-sid`, `telephony-auth-token`, `voice-provider-api-key`) as new versions. Then set `enable_secrets = true` in `hotato.tfvars` and re-run `bootstrap.sh`.
7. Put the build service account (Terraform output `build_service_account`) in as a Google Cloud trusted publisher for the hotato project on PyPI.
7a. Publish the private Atlas collector before the first build (the jobs image installs it, and the build stops if it is missing): `GCLOUD_CONFIGURATION=<config> autopilot/build/publish-atlas-wheel.sh <hotato-atlas checkout> --upload`. It builds the wheel from the checkout's last commit and uploads it to the private Python repository `hotato-private-py` (Terraform output `private_python_index`); the source is never published. Later collector versions ship themselves through `hotato-atlas-wheel` (section Atlas); `_ATLAS_VERSION` in `build/cloudbuild.yaml` is only the version baked into the image. Uploaded versions are never replaced.
8. Run the first build from the repository root: `gcloud builds submit --config autopilot/build/cloudbuild.yaml --substitutions _DEPLOY=true,_CF_ACCOUNT_ID=<cloudflare account id>`.
9. Fill `scorecard/competitors.json` in the bucket (repo and changelog URL per reference company) and `simulate/allowlist.json` (the only phone numbers simulated calls may reach).
10. Create `scan/banned.sha256` entries on your machine only: `python3 autopilot/scan/make_hashes.py names.txt >> autopilot/scan/banned.sha256`, commit the hashes, never the names file.
11. Yank PyPI releases 0.1.0 to 0.3.1.
12. Give the standing write grant covering pushes, PRs, deploys and releases, then list the jobs allowed to go live on their own in `auto_promote_jobs` in `hotato.tfvars` (start with `["freshness", "upkeep", "scorecard"]`) and re-run `bootstrap.sh`. Each listed job still has to earn 7 clean days in shadow first. Jobs not listed stay in shadow.
12a. Turn on self-maintenance (after step 12's grant, because the weekly rebuild redeploys): in the Cloud Console, Cloud Build > Triggers > Connect repository, choose GitHub (Cloud Build GitHub App), install the app on the hotato repo and connect it to this project. Then set `self_maintain = true` and `cloudflare_account_id = "<id>"` in `hotato.tfvars` and re-run `bootstrap.sh`. From then on infra changes merged to `main` apply themselves, pull requests touching `infra/` get a plan, and the images rebuild every Sunday.
13. After the sprint, set `phase = "monthly"` and re-run `bootstrap.sh`; delete the sprint budget in the console if you want.

## Atlas: hotato.dev/atlas keeps itself current

What runs (all on one image; `jobs/atlas_job.py` holds the design notes):

- `hotato-atlas`, hourly: collects new public reports, classifies them with Gemini on a 40 minute budget per run (a backlog drains over a few runs), runs the propose/review loop, renders the pages into a checkout of the exact site commit that is live on Cloudflare Pages, checks them, deploys through that commit's own `tools/stage-deploy.sh`, and confirms the live feed. It logs the heartbeat `atlas-publish` only when hotato.dev serves its render. It holds the writer lock from the store read to the confirmed deploy, re-reads the live deployment right before deploying, and deploys only a tree that differs from the live commit under `atlas/` and `sitemap_index.xml`.
- `hotato-atlas-recheck` (daily) and `hotato-atlas-audit` (weekly): evidence re-checks and the published-record audit. Store only, behind the same shrink guard as the hourly run; the next hourly run publishes. They hold no repository credentials.
- `hotato-atlas-watch` (every 6 hours): reads the live feed. Stale or replaced pages re-run `hotato-atlas`, at most once per 3 hours.
- `hotato-atlas-wheel` (daily): a new collector version on the collector repository's main branch is tested, built, smoke-tested, uploaded to `hotato-private-py` and offered as the candidate. The collector's tests run against the built wheel with an allow-listed environment. The hourly job promotes the candidate after one run that published (or found the site current), rolls it back when the collector itself fails, and rolls it back on its own after 3 runs that never settled (killed, out of memory, timed out). A rolled-back version is re-offered once if main still carries it at the same commit and it passes again; otherwise bump the version. upkeep opens an issue when `_ATLAS_VERSION` in `build/cloudbuild.yaml` falls behind.

The four new schedules are created paused. To start them: `for s in atlas-recheck atlas-audit atlas-watch atlas-wheel; do gcloud scheduler jobs resume hotato-$s --location us-central1; done`.

One-time setup:

1. In `infra/hotato.tfvars`: `atlas_site_repo = "<owner>/<site repo>"`, `atlas_collector_repo = "<owner>/<collector repo>"`, `cloudflare_account_id = "<id>"`. Re-run `bootstrap.sh`.
2. Read access to both private repositories: install the GitHub App on them, or add a fine-grained token with contents:read on just those two as a version of the secret `atlas-repos-token`. The bot token is the last fallback. Every run proves the credential with a real read before using it and raises `atlas_repo_access` when none works; adding the `atlas-repos-token` secret needs no code change.
3. The `cloudflare-pages-token` must be allowed to read and deploy Pages (Account: Cloudflare Pages: Edit).
4. Seed the store once if `atlas/atlas.db` is not in the state bucket: `gcloud storage cp <atlas.db> gs://<project>-state/atlas/atlas.db`.

When an alert names an Atlas kind (the live pages keep the last good render meanwhile):

| kind | meaning | step |
|---|---|---|
| `atlas_guard_shrink` | the new store would remove published entries or reports; it was quarantined in `atlas/quarantine/`, the old store kept | if the removal was deliberate (withdrawals, merges): `gcloud storage cp /dev/null gs://<project>-state/atlas/flags/allow-shrink` (lets one run through) |
| `atlas_guard_structure` | the pages failed the structure checks (page count, feed, stylesheet, preflight) | read `runs/atlas/` for the problem list; usually a site release changed its chrome: fix the donor page or the collector |
| `atlas_guard_no_baseline` | no published manifest, the live feed did not answer, and the previous store did not render: nothing to compare with, so nothing was saved | check the live site and the run log; the next run retries |
| `atlas_repo_access` | no credential can read the site or collector repository (the alert lists what was tried) | install the GitHub App on that repository (contents: read), or add a version of `atlas-repos-token` |
| `atlas_publish_superseded` | not deployed: the live site changed since the checkout, another run published meanwhile, or the lock was lost | none; the next run starts over from the new state |
| `atlas_site_unavailable` | the live site is not a pushed commit of the site repository, or the checkout or Cloudflare read failed | push the commit the site was deployed from, or deploy a pushed commit; check the credentials above |
| `atlas_publish_failed` / `atlas_live_mismatch` | the deploy failed, or the live feed is not the render | the next run retries; repeated: check the Pages project and token |
| `atlas_db_restored` / `atlas_db_unusable` | the store was corrupt: restored from an earlier generation or snapshot; or nothing sound was left | unusable: seed it (step 4) or set `atlas/flags/allow-empty-start` |
| `atlas_collector_rolled_back` | a new collector version failed in its own code, made a render the guard refused, or ran 3 times without settling; runs use the previous one | `atlas/collector.json` `bad_why` says which; fix the collector and bump its version |

Owner commands against the cloud store (withdraw, merge, dispute): `gcloud run jobs execute hotato-atlas-audit --region us-central1 --args=cli,withdraw,<id>,--reason,<why>`. The next hourly run publishes the change.

## Shadow mode (default for the first week)

`gates/policy.json` has `"shadow_mode": true`: the builder and reviewer open and review PRs, and every PR waits for the owner; nothing auto-merges. Jobs also start with `DRY_RUN=1` (no GitHub or phone writes). After a week of clean PRs, set `shadow_mode` to `false` through an owner-approved PR; only the classes marked `auto_merge` (dependencies, vendor adapters, tests, docs, Atlas content) then merge on green gates. Features, public claims and pricing always wait for the owner.

## Cost table (USD)

| Category | Sprint (6 weeks, one time) | Monthly after |
|---|---|---|
| Agents (builder, reviewer, Gemini) | 1,400 | 300 |
| Simulated calls | 300 | 100 |
| Evaluation runs | 200 | 0 |
| Infrastructure | 100 | 50 |
| Buffer | 0 | 50 |
| **Total** | **2,000** | **500** |

Enforced three ways: the budgets (one-time setup) with the kill-switch function; per-run limits (builder 25 / reviewer 5 sprint, 8 / 2 monthly); and a spend ledger in the bucket that stops the builder, reviewer and `simulate` jobs at the category cap.

## Alerts

Each alert notifies once, when it opens; nothing is sent when it resolves. Only three email `alert_email`: they need the owner now. Every other alert goes to the Pub/Sub topic `hotato-lane-alerts`; the fleet pulls its subscription `hotato-lane-alerts-fleet` (7 days of unread notices kept) and hands each notice to the lanes. Who may pull it is `lane_alert_readers` in `hotato.tfvars`.

| Alert | Goes to | Fires when |
|---|---|---|
| site / monitor down | email | the uptime check failed for 5 minutes |
| budget kill-switch error | email | the budget function errored, so spend may not stop at the cap: use the stop command above |
| Atlas publish failed | email | hotato.dev/atlas was not updated: an ERROR of kind `atlas_guard_shrink`, `atlas_guard_structure`, `atlas_publish_failed`, `atlas_live_mismatch`, `atlas_db_unusable` or `atlas_collector_rolled_back` |
| job execution failed | lanes | any `hotato-*` Cloud Run job ends failed (error exit, crash, timeout); the one notice for a failed run |
| scheduler attempt failed | lanes | Cloud Scheduler could not start a run |
| workflow execution failed | lanes | the runner that starts the jobs failed and no job failed with it (a job could not be started) |
| build failed | lanes | any Cloud Build build failed (weekly rebuild, infra apply, manual build) |
| job raised an alert | lanes | a job logged a `hotato_alert` line (contract below), except `job_crash` and `model_unavailable` (the failed run reports them) and the Atlas publish kinds at ERROR (emailed above) |
| `<job>` heartbeat missing | lanes | a job that has run before sent no heartbeat for twice its schedule interval (hourly 2 h, daily 48 h, weekly 14 days); a job that has never run raises none |

While the kill flag is set the jobs send no heartbeats, so the heartbeat alerts fire; that is the reminder that the system is stopped. Snooze them in Cloud Monitoring if the stop is planned.

**Log contract every job keeps** (one JSON object per line on stdout; Cloud Run turns it into a structured log entry):

- On success, exactly one line: `{"hotato_heartbeat": "<job>", "severity": "INFO"}`. `<job>` is the short name (`atlas`, `upkeep`, `builder`, ...), the same as in `runs/<job>/`. This feeds the log metric `hotato_heartbeat`.
- On anything the owner should see: `{"hotato_alert": "<kind>", "severity": "WARNING" or "ERROR", "detail": "...", "job": "<job>"}`. Any line with `hotato_alert` at WARNING or above raises an alert to the lanes. `<kind>` is a short stable word, for example `job_crash` or `gate_violation`.
- A new job needs a schedule in `infra/schedule.tf`; its heartbeat alert is then made automatically. A job with no schedule makes `terraform plan` print a warning.

## Upkeep that runs itself

- **Images:** every Sunday the images are rebuilt from `main` on fresh base images, after the full test and gate run, then deployed (needs step 12a). The jobs keep their `DRY_RUN` setting.
- **Infra:** a merge to `main` touching `autopilot/infra/` runs fmt, validate, plan and apply (`autopilot/build/cloudbuild-infra.yaml`). Only the main-branch trigger can apply.
- **Storage stays bounded:** the image registry keeps `latest` and the 10 newest versions of each image and deletes the rest after 30 days (untagged after 14). The Terraform state bucket never deletes the live state; an old version goes only once it is 30 days old and has 10 newer versions. The app state bucket deletes replaced versions after 14 days, run records after a year and `cache/` after 30 days.

## Reading what happened

Run records are JSON files under `runs/<job>/` in the state bucket. Weekly scorecards are under `scorecard/weekly/`.

## Jobs that go live on their own

Promotion is automatic only for the jobs the owner lists in `auto_promote_jobs` (step 12); their `DRY_RUN` is `auto`, every other job gets `1`. A job goes live after 7 clean days in shadow (scorecard: 2 weekly runs). It drops back to shadow on a gate violation, two failed runs in a row, or the kill switch. Each change raises a `promoted` or `demoted` alert. See the state with `python3 jobs/promotion.py` or under `gs://<project>-state/promotion/`. Remove a job from `auto_promote_jobs` to force it back to shadow.

For GitHub access that never expires, add the GitHub App's id, private key and installation id as versions of the `github-app-*` secrets. Until then the bot token is used, with alerts 30 and 7 days before it expires. Keep the successor model names in `gates/policy.json` current; a fallback raises `model_fallback`.
