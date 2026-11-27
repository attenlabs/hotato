"""Example custom metrics for hotato.

Every module-level function named ``metric_*`` in a file under
``hotato_metrics/`` is a metric; ``@metric(...)`` sets its inputs and labels.
A metric receives the call (``call.transcript``, ``call.spans``,
``call.timing``, ``call.params``) and returns ``passed`` / ``failed`` /
``unknown``. A pass must cite evidence; hotato checks every citation against
the call before it reports the verdict.
"""

import re

from hotato.metrics import failed, metric, passed, unknown


def _quote_around(text, phrase):
    """The phrase with up to two words either side, as it appears in text.

    A pass needs a quote of at least three words, so a one-word match such as
    "booked" is cited with the words around it."""
    m = re.search(r"(?:\S+\s+){0,2}" + re.escape(phrase) + r"(?:\s+\S+){0,2}",
                  text, re.IGNORECASE)
    return m.group(0) if m else None


@metric(needs=["transcript"], dimension="conversation")
def metric_greets_with_name(call):
    """The agent's first turn names the business."""
    name = call.params.get("name")
    if not name:
        return unknown("params.name is not set")
    agent_turns = call.turns(role="agent")
    if not agent_turns:
        return failed("the agent never spoke")
    index, turn = agent_turns[0]
    if name.lower() in turn["text"].lower():
        return passed([call.cite_turn(index, _quote_around(turn["text"], name))],
                      reason=f"first agent turn names {name}")
    return failed(f"first agent turn does not name {name}",
                  evidence=[call.cite_turn(index)])


@metric(needs=["transcript"], dimension="speech")
def metric_max_agent_turn_words(call):
    """No agent turn runs longer than params.max_words words (default 40)."""
    limit = int(call.params.get("max_words", 40))
    agent_turns = call.turns(role="agent")
    if not agent_turns:
        return unknown("no agent turns in the transcript")
    index, turn = max(agent_turns, key=lambda it: len(it[1]["text"].split()))
    words = len(turn["text"].split())
    measured = {"longest_agent_turn_words": words, "max_words": limit}
    if words <= limit:
        return passed([call.cite_turn(index)], measured=measured,
                      reason=f"longest agent turn is {words} words")
    return failed(f"agent turn {index} is {words} words (limit {limit})",
                  evidence=[call.cite_turn(index)], measured=measured)


@metric(needs=["transcript", "spans"], dimension="outcome")
def metric_claim_backed_by_tool(call):
    """When the agent says the claim word, the named tool call is in the trace."""
    claim = call.params.get("claim", "booked")
    tool = call.params.get("tool")
    if not tool:
        return unknown("params.tool is not set")
    pattern = re.compile(r"\b" + re.escape(claim) + r"\b", re.IGNORECASE)
    claims = [(i, t) for i, t in call.turns(role="agent") if pattern.search(t["text"])]
    if not claims:
        return unknown(f"the agent never said {claim!r}, so there is no claim to check")
    tool_spans = call.spans_of("tool_call", name=tool)
    claim_index, claim_turn = claims[0]
    quote = _quote_around(claim_turn["text"], pattern.search(claim_turn["text"]).group(0))
    if tool_spans:
        span_index, _span = tool_spans[0]
        return passed([call.cite_turn(claim_index, quote), call.cite_span(span_index)],
                      reason=f"the {claim!r} claim is backed by a {tool} call")
    return failed(f"the agent said {claim!r} but the trace has no {tool} call",
                  evidence=[call.cite_turn(claim_index, quote)])
