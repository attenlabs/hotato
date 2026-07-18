# Why does my voice agent stop talking when the caller says "mhm"?

Because the turn-taking layer treated a backchannel (a short acknowledgement,
not a bid for the floor) as an interruption: hotato measures that moment as a
yield, you label the expected behavior `hold`, and the labeled moment becomes
a contract that replays the same frozen audio deterministically in CI until
the agent holds the floor through it.

## See the failure

The bundled demo battery ships this exact defect as one of its two calls:

```console
$ hotato demo --fail --no-open
  ...
  [FAIL] fd-02-backchannel-yielded: did_yield=True seconds_to_yield=0.34s talk_over=0.32s
         fix[engagement-control]: False barge-in: a backchannel was treated as a bid for the floor
            -> a learned engagement-control / addressee-detection layer
  note: no single sensitivity threshold satisfies this battery; see funnel pointer in --format json.
```

The fix class matters: when the same battery also contains a missed real
interruption, no single sensitivity threshold fixes both, and hotato says so
instead of proposing a knob that trades one failure for the other.

## Pin it as a replayable contract

[`hotato investigate`](../INVESTIGATE.md) finds the moment; the demo
recording ships in the repo:

```console
$ hotato investigate examples/funnel-demo/audio/fd-02-backchannel-yielded.example.wav
hotato investigate [run 1]: fd-02-backchannel-yielded.example.wav
  input health: eligible for scan
  verdict path: eligible (a labeled event here can carry a real yield/hold verdict)
  most likely failure (top-ranked candidate):
    [1] t=2.19s overlap_while_agent_talking  overlap_sec=0.26
  next: label it (use --expect hold instead if the agent was right to keep talking):
    hotato investigate label '.hotato/investigate-state.json#1' --expect yield
```

The label is your policy call, and here the agent was wrong to stop, so the
expectation is `hold`:

```console
$ hotato investigate label '.hotato/investigate-state.json#1' --expect hold \
    --id demo-false-stop-backchannel-001 --out contracts \
    --rationale "caller only said mhm; the agent should have held the floor"
created hotato contract: demo-false-stop-backchannel-001
  dir:      contracts/demo-false-stop-backchannel-001.hotato
  expect:   hold
  scorable: yes
  label:    asserted (reviewer=david-mf1; no signing key configured, so this is an operator-asserted expectation, not a cryptographically signed human label)
  passed:   False
  measured: did_yield=True seconds_to_yield=0.26s talk_over=0.26s
```

The bundle carries the clipped two-channel audio, per-frame evidence, the
trust report, the label record with your rationale, and a ready CI policy
([`docs/CONTRACTS.md`](../CONTRACTS.md)).

## Replay it in CI

```console
$ hotato contract verify contracts
hotato contract verify: contracts (1 contract)
  [FAIL] demo-false-stop-backchannel-001 (expect hold): did_yield=True seconds_to_yield=0.26s talk_over=0.26s | integrity: intact
  0/1 contracts pass; exit_code=1
  These contracts pin known failures. Each stays red until you fix the agent and recapture the call, the same way a snapshot test stays red until you update the snapshot.
```

`verify` re-measures the stored evidence: running it again returns the same
numbers, and `integrity: intact` means every file still matches its recorded
digest, so a tampered bundle is a named refusal. The gate stays exit 1 on
purpose until you change the agent and recapture the moment
([`docs/RECAPTURE.md`](../RECAPTURE.md)); a backchannel the agent now talks
through flips the contract to `hold` satisfied and the gate to green.
