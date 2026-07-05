# examples/demo — the site demo, reproducible

This directory backs the walkthrough on the Hotato site
(`hotato-site/docs/demo.html`): *watch it catch an agent talking over the
caller, then point at the fix.*

It adds **no tool source** — `run-demo.sh` only *runs* the CLI against the
existing labeled-synthetic fixtures in [`../funnel-demo/`](../funnel-demo/) and
prints the exact output the page embeds.

## The three blocks on the page

| # | What the page shows | Command | Exit |
|---|---------------------|---------|------|
| 1 | The bad agent fails, out loud | `run --suite barge-in --scenarios examples/funnel-demo/scenarios --audio examples/funnel-demo/audio --format text` | `1` |
| 2 | The fix map (`fix_class`, the config knob) + the both-axes `funnel` pointer | same, `--format json` | `1` |
| 3 | The passing reference (self-test, 8/8) | `run --suite barge-in --format text` | `0` |

The two failing events are, by design:

- **fd-01** — the agent talks straight over a real interruption
  (`did_yield=False`) → `fix_class: config` (a sensitivity knob you can turn).
- **fd-02** — the agent yields to a bare "mhm" (`did_yield=True` where it must
  be false) → `fix_class: engagement-control`.

Because the battery fails on **both** axes at once — a missed interruption *and*
a false trigger — no single sensitivity threshold satisfies it, so the tool
emits its `funnel` pointer (the discrimination case). All of that is real tool
output; nothing on the page is hand-written into the terminal blocks.

> These are **labeled synthetic** bad-agent renders — a runnable floor and a
> regression guard, not a recording of a real production call.

## Run it

From anywhere (the script locates the repo root itself):

```bash
examples/demo/run-demo.sh
```

Write the raw captures to a directory (handy for a byte-diff against the page):

```bash
examples/demo/run-demo.sh --out /tmp/hotato-demo
# -> /tmp/hotato-demo/{fd-text.txt,fd-json.txt,pass-text.txt}
```

Equivalent one-liners (run from the repo root):

```bash
PYTHONPATH=src python3 -m hotato.cli run --suite barge-in \
  --scenarios examples/funnel-demo/scenarios --audio examples/funnel-demo/audio --format text

PYTHONPATH=src python3 -m hotato.cli run --suite barge-in \
  --scenarios examples/funnel-demo/scenarios --audio examples/funnel-demo/audio --format json

PYTHONPATH=src python3 -m hotato.cli run --suite barge-in --format text
```

The published equivalents are `uvx hotato run …` (once the package is
installed); this script uses the in-repo module form so it runs straight from a
checkout.
