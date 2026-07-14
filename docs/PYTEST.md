# Pytest: the fixture and the gate

Install hotato and the plugin registers itself: a standard `pytest11` entry
point, zero `conftest.py` wiring, zero imports. It adds one fixture and one
opt-in session gate, both scoring with the same engine as the CLI and
returning the same envelope -- the JSON result object every hotato surface
emits.

```bash
# zero-install with uvx, or: pipx install hotato
uvx hotato --help
```

## The `hotato_score` fixture

Score a recording or a suite inside any test and assert on the envelope --
the same inputs the CLI takes:

```python
def test_call_yields(hotato_score):
    env = hotato_score(stereo="call.wav", expect="yield")
    assert env["exit_code"] == 0
    assert all(e["verdict"]["passed"] for e in env["events"])

def test_split_channels(hotato_score):
    env = hotato_score(caller="caller.wav", agent="agent.wav", expect="yield")
    assert env["events"][0]["verdict"]["passed"]

def test_selftest_battery(hotato_score):
    env = hotato_score(suite="barge-in")
    assert env["exit_code"] == 0
```

The envelope is the standard machine shape (`schema_version` "1"): per event
a `verdict` (`passed`, `did_yield`, `seconds_to_yield`, `talk_over_sec`), a
`signals` bus, a `fix` on every failure, and the `limits` block. Assert on
whichever measurement your test cares about.

## The `--hotato-suite` session gate

Opt in on the command line and the battery runs after your tests, printing a
summary and failing the whole session (exit 1) on a regression:

```bash
pytest --hotato-suite                    # default suite: barge-in
```

The bundled battery is a self-test of the harness. Point the same flag at
your own labelled scenario and audio directories to gate on your own agent:

```bash
pytest --hotato-suite \
  --hotato-suite-scenarios corpus/suites/gold/scenarios \
  --hotato-suite-audio corpus/suites/gold/audio
```

Any directory in the same scenario shape works, including your own labelled
calls -- see `docs/SUITES.md` for the bundled tiers and `docs/SUBMITTING.md`
for building labelled fixtures from your own recordings.

## Where it fits

- The gate is one flag on a test run you already have: turn-taking rides
  along with every `pytest` invocation, locally and in CI.
- The GitHub workflow (`docs/CI.md`) is the other ready-made gate -- it
  scores every pull request and posts a sticky results comment. Use either,
  or both.
- The plugin runs offline, and its fixtures are deterministic and
  self-contained, so results stay stable run to run.
