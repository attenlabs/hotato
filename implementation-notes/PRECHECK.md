# PRECHECK — read-only baseline (2026-07-13)

Repo: /home/david-mf1/Projects/hotato @ 544a7d1 (branch main), version 1.3.2
(pyproject.toml:8, src/hotato/__init__.py:60; PyPI 1.3.2 live).

## Commands run
- `bash <pack>/scripts/preflight.sh ~/Projects/hotato` (script's bare `python`
  is absent on this box; pytest sections re-run with python3, see below)
- `PYTHONPATH=src python3 -m hotato start --demo` in a scratch dir + JSON parse
  of the produced hotato-sweep.json
- targeted greps quoted below

## Baseline findings, confirmed or corrected
1. CONFIRMED hard-coded demo candidate selection: `_DEMO_CONTRACT_CANDIDATE = 2`
   (src/hotato/start.py:37), used at start.py:118 as `from_candidate=...#2`.
2. CONFIRMED contradictory scorable state: live sweep candidate order is
   [agent_stop_no_caller(fd-02), agent_stop_no_caller(fd-01),
    overlap_while_agent_talking(fd-02), overlap x2 (fd-01 t=5.32, t=2.00)].
   Rank 2 is an agent_stop_no_caller event; the demo contract it creates
   verifies as `did_yield=False seconds_to_yield=None talk_over=0.0` while the
   demo narrates "the agent talked over the caller" (talk_over=0.0 contradicts
   the story). Reproduced on this commit; also observed by the operator on a
   clean macOS install of 1.3.2.
3. CONFIRMED no root consumer Action: no action.yml/action.yaml at repo root
   (preflight: "root action absent").
4. CONFIRMED core dependencies: `dependencies = []` (pyproject.toml:40).
5. CONFIRMED semantic ground truth for selection: the packaged scenario
   src/hotato/data/demo/failing/scenarios/fd-01-missed-interruption.json
   declares category=should_yield, audio=fd-01-missed-interruption.example.wav,
   caller_onset_sec=2.0, expected.yield=true. The matching sweep candidate is
   kind=overlap_while_agent_talking, source=fd-01..., t_sec=2.00 (rank 5 in the
   current ordering). Selection by rank is therefore wrong whenever ordering
   shifts, and is wrong TODAY (rank 2 != the declared event).
6. Version drift: repo/PyPI both 1.3.2; hotato.dev shows 1.3.1 in spots pending
   the current in-flight site pass (separate workstream).
7. UI/server: src/hotato/serve/{app,data,render,security}.py, stdlib-only,
   token-auth, five views; an in-flight worktree adds auto-open + landing page
   (reconcile with P2A; read-only posture unchanged).
8. Reference matrix + environments (pack claims accepted, to re-verify in P8):
   examples/reference-agent is scripted-fixture + mocked trace/state;
   clean/cafe/street labels are metadata in that path; synth.py provides real
   waveform perturbations not yet bound to those labels. Not re-verified in
   depth in this baseline; scheduled with P8.

## Dirty-tree / in-flight reconciliation
- Committed just prior to baseline: 634f47f (paste-safe demo next-steps),
  544a7d1 (speak-from-strength hero assets). Neither touches candidate
  selection; no overlap with P0.
- In-flight worktrees (paste-safety sweep, serve polish, copy passes) do not
  touch start.py's contract-selection path. P0 proceeds on main.

## Preflight quirk
- scripts/preflight.sh uses `python`/`rg`; this box has `python3`/`ugrep`. The
  targeted test files it lists were run green earlier on this commit
  (test_start_cli 17 passed; full suite 2767 passed on 472af91's tree state).
