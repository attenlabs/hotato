# Production call monitoring: `hotato monitor`

Connect a Vapi or Retell account and every new call is checked as it arrives.
A rule that matches sends an alert to your chat, pager or webhook, and a bad
call becomes a regression test with one command.

```
export VAPI_API_KEY=...                       # or: hotato connect vapi
export HOTATO_SLACK_WEBHOOK_URL=<your incoming-webhook URL>
hotato monitor test-alert --rules rules.json  # prove the wiring first
hotato monitor run --stack vapi --every 5m --rules rules.json --store ./monitor
hotato monitor pin CALL_ID --store ./monitor  # a bad call -> a contract
hotato prove --contracts contracts
```

## What a cycle does

1. List calls newer than the stored cursor (the first cycle looks back
   `--since`, default 1h).
2. Fetch each recording with the same adapters as `hotato pull`, so credentials
   resolve identically (flag, then `hotato connect`, then `VAPI_API_KEY` /
   `RETELL_API_KEY`).
3. Check it with the autopsy engine, unchanged.
4. Write `STORE/events/<call>.json`, evaluate every rule, send alerts.

A call is scored once: an existing event file means it is done, so restarting
resumes from the cursor without rescoring. A recording that cannot be fetched
yet (the call may still be running) is retried each cycle for 5 attempts, then
recorded with a null score. Alerts are at-least-once: a crash between a send and
the event write can repeat that one alert; the cooldown and PagerDuty's dedup
key absorb it. If more than `--limit` calls arrive between cycles the oldest are
skipped and a warning says so.

Mono recordings follow the project's usual rule: they are refused unless you
pass `--allow-mono`, and are then reported best-effort (silence timing only)
with `score: null`. Rules on `score` skip such calls; incident rules still
evaluate.

## The store

```
STORE/state.json            cursor, retry queue, call -> autopsy id, rule state
STORE/events/<call>.json    one event per call
STORE/audio/                the downloaded recordings (hotato monitor pin needs them)
STORE/autopsy/              autopsy-<id>.html report and .json envelope
```

## Event schema `hotato.monitorevent.v1`

```json
{"schema": "hotato.monitorevent.v1", "call_id": "...", "stack": "vapi",
 "agent_id": "...", "started_at": "2026-11-26T10:00:00Z", "duration_s": 12.0,
 "score": 75.0,
 "incidents": [{"type": "barge-in", "severity": "critical", "at_s": 3.2, "detail": "..."}],
 "alerts_fired": ["rule-id"], "report_path": "STORE/autopsy/autopsy-apx-....html",
 "scored_at": "2026-11-26T10:05:00Z"}
```

`agent_id`, `started_at`, `duration_s`, `score` and `report_path` are `null`
when unknown. `type` is the autopsy kind: `barge-in`, `talk-over`, `dead-air`,
`latency-spike`, `echo-suspected`; `severity` is `warning` or `critical`.

`score` is per call: 100 minus 25 per critical incident and 5 per warning
incident, floored at 0. It is a threshold for alerting, not the fleet-level
Voice Stability Score that `hotato vapi health` reports.

## Rules file

A JSON list (or `{"rules": [...]}`) of:

```json
[
  {"id": "low-score", "when": {"metric": "score", "op": "<", "value": 60},
   "channels": ["slack"], "cooldown_min": 30},
  {"id": "dead-air", "when": {"incident_type": "dead-air", "severity_at_least": "critical"},
   "channels": ["slack", "pagerduty"]},
  {"id": "barge-in-rate", "when": {"incident_type": "barge-in", "severity_at_least": "warning"},
   "window": {"calls": 20, "min_hits": 5}, "channels": ["pagerduty", "webhook"]}
]
```

| Field | Meaning |
| --- | --- |
| `id` | unique name; part of the PagerDuty dedup key |
| `when` | a metric test or an incident test |
| `window` | optional, `{"calls": N, "min_hits": M}`: fire only when at least M of the last N calls (per agent) hit |
| `channels` | any of `slack`, `pagerduty`, `webhook` |
| `cooldown_min` | minutes before a still-matching rule alerts again (default 15) |

Metric test: `{"metric", "op", "value"}`; `op` is `<`, `>`, `<=` or `>=`.
Metrics: `score`, `incident_count`, `critical_count`, `warning_count`,
`dead_air_s` (longest dead-air silence), `latency_gap_s` (longest dead-air or
latency-spike gap), `talk_over_s` (longest barge-in or talk-over overlap),
`duration_s`. Incident test: `{"incident_type", "severity_at_least"}`, where
`incident_type` is a kind above or `any`, and severity defaults to `warning`.
A call with no data for a metric (unscored, or a score on a mono call) is neither
a hit nor a clear. A rule naming a channel with no destination is refused (exit
2) before any call is touched.

State is per rule and agent. A rule alerts when it matches and is not within its
cooldown; a delivery that fails does not start the cooldown, so the next call
retries it.

## Channels

| Channel | Destination | Behaviour |
| --- | --- | --- |
| `slack` | `--slack-webhook-url` or `HOTATO_SLACK_WEBHOOK_URL` | Block Kit message: rule, call id, stack and agent, score, incidents, report link |
| `pagerduty` | `--pagerduty-routing-key` or `HOTATO_PAGERDUTY_ROUTING_KEY` (Events API v2) | `trigger` with `dedup_key` `hotato-<rule id>-<agent id>`; `resolve` with the same key when the rule stops matching |
| `webhook` | `--webhook-url` or `HOTATO_MONITOR_WEBHOOK_URL` | JSON via the same sender as `--notify`: `kind: "monitor-alert"`, rule, call, agent, score, incidents, report path |

`HOTATO_PAGERDUTY_EVENTS_URL` overrides the Events API URL (for example the EU
service region). `--report-url-base URL` makes alerts link to `URL/<report file>`
instead of a local path. URLs and keys are never written to logs. Delivery is
fail-open: a down receiver logs one line and the monitor keeps running. Alerts
carry timing numbers only, never audio, credentials or transcript text. See
`docs/EGRESS.md`.

## Test the wiring

`hotato monitor test-alert --rules rules.json` sends one labelled TEST alert per
rule and channel (PagerDuty gets a trigger and an immediate resolve of a
test-only key). Exit 0 when every send was delivered, 1 when any failed.

## Pin a bad call

`hotato monitor pin CALL_ID --store DIR [--incident N] [--out contracts]` pins the
call's top critical incident (or incident N) through `hotato pin`, with all of its
refusals: a mono recording, a call with no critical incident, or a changed source
file exits 2 with the reason.

## Exit codes

`monitor run`: 0 after the requested cycle(s) or when stopped; 2 usage error.
`monitor test-alert`: 0 all delivered, 1 any failed, 2 usage error.
`monitor pin`: 0 pinned, 2 refused.
