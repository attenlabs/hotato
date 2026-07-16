<div align="center">

<img src=".github/assets/hotato-banner.svg" alt="hotato" width="340" style="max-width:100%;height:auto;">

<p>
<a href="https://pypi.org/project/hotato/"><img src="https://img.shields.io/pypi/v/hotato?style=flat-square&color=e2470f&label=pypi" alt="PyPI version"></a>
<a href="https://pypi.org/project/hotato/"><img src="https://img.shields.io/pypi/pyversions/hotato?style=flat-square&color=6f5d44" alt="Python versions"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-6f5d44?style=flat-square" alt="License: MIT"></a>
<a href="docs/MCP.md"><img src="https://img.shields.io/badge/MCP-ready-e2470f?style=flat-square" alt="MCP ready"></a>
<img src="https://img.shields.io/badge/offline-by%20default-0f7a5f?style=flat-square" alt="Offline by default">
</p>

</div>

**hotato** turns a failed voice call into a deterministic regression test that lives in your Git and reproduces forever. Give it both channels of a recording and it measures the timing between the two voices, the talk-over and slow yields a transcript cannot see, then locks each catch into a content-addressed CI contract that returns the same verdict, exit 0 or 1, on any machine. Self-hosted conversation QA for voice agents: MIT, offline, no account.

<p align="center">
  <img src="docs/assets/hotato-demo.gif" alt="hotato demo: uvx hotato demo --fail types out and scores two recorded calls, both fail on timing, exit 1." width="720" style="max-width:100%;height:auto;">
</p>

## Get started

**Point your coding agent at it.** Paste this repo at Claude Code, Cursor, or any coding agent and ask it to try hotato on your voice agent. It reads [`AGENTS.md`](AGENTS.md) and drives the whole loop: run the demo, wire a CI gate, and re-check the numbers itself.

Or run it yourself:

```bash
uvx hotato demo --fail                 # catch a failure in 10 seconds, no account
pipx install hotato                    # keep it in a project
uvx --from "hotato[mcp]" hotato-mcp    # drive it over MCP, local stdio
```

## Five dimensions

- **Outcome**: job done, on tool-call and state evidence.
- **Policy**: required disclosures, PII handling.
- **Conversation**: did the agent yield when the caller took the floor, and how fast.
- **Speech**: response latency and turn timing.
- **Reliability**: pass@1 / pass@k / pass^k with a Wilson interval.

A call comes in two channels (caller on one, agent on the other). A mono or bad export is marked NOT SCORABLE, so a verdict measures timing, not intent.

## License

MIT ([`LICENSE`](LICENSE))

mcp-name: io.github.attenlabs/hotato
