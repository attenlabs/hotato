<div align="center">

<img src="https://raw.githubusercontent.com/attenlabs/hotato/main/.github/assets/hotato-banner.svg" alt="hotato" width="340" style="max-width:100%;height:auto;">

<p>
<a href="https://pypi.org/project/hotato/"><img src="https://img.shields.io/pypi/v/hotato?style=flat-square&color=e2470f&label=pypi" alt="PyPI version"></a>
<a href="https://pypi.org/project/hotato/"><img src="https://img.shields.io/pypi/pyversions/hotato?style=flat-square&color=6f5d44" alt="Python versions"></a>
<a href="https://github.com/attenlabs/hotato/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-6f5d44?style=flat-square" alt="License: MIT"></a>
<a href="https://github.com/attenlabs/hotato/blob/main/docs/MCP.md"><img src="https://img.shields.io/badge/MCP-ready-e2470f?style=flat-square" alt="MCP ready"></a>
<img src="https://img.shields.io/badge/offline-by%20default-0f7a5f?style=flat-square" alt="Offline by default">
</p>

</div>

**hotato** is self-hosted conversation QA for voice agents: give it both channels of a recorded call, it measures the timing between the two voices, and locks every catch into a CI contract. The name is hot potato, hold the turn too long and you drop the call. MIT, offline, no account.

<p align="center">
  <img src="https://raw.githubusercontent.com/attenlabs/hotato/main/docs/assets/hotato-demo.gif" alt="hotato demo: uvx hotato demo --fail types out and scores two recorded calls, both fail on timing, exit 1." width="720" style="max-width:100%;height:auto;">
</p>

## Install

```bash
uvx hotato demo --fail                 # zero-install, runs the bundled battery
pipx install hotato                    # keep it in a project
uvx --from "hotato[mcp]" hotato-mcp    # drive it from a coding agent over MCP, local stdio
```

## Five dimensions

- **Outcome**: job done, on tool-call and state evidence.
- **Policy**: required disclosures, PII handling.
- **Conversation**: did the agent yield when the caller took the floor, and how fast.
- **Speech**: response latency and turn timing.
- **Reliability**: pass@1 / pass@k / pass^k with a Wilson interval.

A call comes in two channels (caller on one, agent on the other). A mono or bad export is marked NOT SCORABLE, so a verdict measures timing, not intent.

## License

MIT ([`LICENSE`](https://github.com/attenlabs/hotato/blob/main/LICENSE))

mcp-name: io.github.attenlabs/hotato
