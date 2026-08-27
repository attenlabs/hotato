"""``hotato scenario generate``: turn an agent's system prompt into a suite of
``hotato.scenario.v1`` files, deterministically and offline.

WHY THIS EXISTS. Authoring the first scenario is the expensive step: a new user
has a system prompt and nothing else, and ``scenario init`` hands them one file
to edit by hand. This module reads the prompt they already have and writes a
whole suite from it -- with NO model and NO network, so the same prompt always
produces byte-identical files. That is the point: a generated suite you cannot
diff is a suite you cannot review, and a suite you cannot review is one you end
up trusting for reasons you cannot state.

WHAT IT IS NOT. This is a STARTING POINT a human edits, and the tool says so
everywhere it speaks. A string-matching pass over a prompt cannot know the
agent's real domain rules, cannot know which caller behaviours actually matter
for your call, and cannot tell you the suite is complete. Passing everything it
writes means the agent survived these scripted callers under these declared
perturbations -- nothing wider. The generated ``facts`` are SYNTHETIC ground
truth (a placeholder order number is not your order number); replace them.

EXTRACTION RULES (all deterministic string/regex work, documented so a reviewer
can predict the output from the prompt):

1. NORMALISE. CRLF -> LF; leading markdown bullet/number markers stripped from
   each line; runs of whitespace collapsed; trailing space removed. A line that
   continues the previous one (the previous ends without sentence punctuation
   and this one starts lowercase) is UNWRAPPED back onto it, and a bullet list
   introduced by a colon is folded into its introducing line as a comma list --
   otherwise a hard-wrapped or bulleted "you can ..." loses every item after
   the first line break. Ids and seeds hash the NORMALISED text, so reflowing a
   prompt or renumbering its bullets does not churn the whole suite.
2. SENTENCES. Split the normalised text on line breaks and on ``. ; ! ?``.
   Every later rule runs per sentence, over a lowercased copy, and keeps the
   sentence's position, so ties break by position in the prompt rather than by
   dict order.
3. CAPABILITIES. A sentence is a capability claim when it contains one of the
   stems in :data:`_CAPABILITY_STEMS` ("you can", "your job is to", "you help
   callers", "you handle", ...). The clause AFTER the stem is split on ``,``,
   ``and``, ``or`` and ``/``; each segment's first token is the verb and the
   rest (up to 4 tokens) is the object. A segment with no object of its own
   inherits the LAST segment's object, because "book, cancel or reschedule
   appointments" attaches one object to three verbs. A segment is kept only if
   its verb is in :data:`_VERB_LEXICON` or it is the clause's only segment --
   an unknown lone verb is more likely a real capability than a parse artefact.
4. REQUIRED DATA. Sentences matching :data:`_COLLECT_PATTERNS` ("always ask for
   the order number", "collect their date of birth", "verify the zip code")
   yield the captured noun phrase, truncated at 4 tokens and stripped of
   trailing function words. Each becomes a ``facts`` key the scripted caller
   holds and volunteers when asked.
5. REFUSALS / CONSTRAINTS. Sentences matching :data:`_REFUSAL_PATTERNS`
   ("never", "do not", "must not", "under no circumstances", "refuse to")
   yield the clause after the marker. These are recorded on every scenario and
   are pushed on by one caller turn, rotating through the list by scenario
   index so the suite as a whole probes every stated constraint.
6. DOMAIN NOUNS. Tokens of 4+ characters that are not in :data:`_STOPWORDS`,
   ranked by frequency then first appearance. Used only to name a goal target
   when a capability has no object of its own.

THE CROSS. Every extracted capability is crossed with the failure taxonomy in
:data:`FAILURE_MODES` -- the seven shipped corpus classes plus the four
turn-taking failure kinds the scorer measures (barge-in, dead air, latency
spike, echo). One scenario per (capability x failure mode); each then expands
further through its ``variation_matrix``. Pairs are emitted in DIAGONAL order
(by capability index + mode index) so a ``--max`` truncation still spans both
axes instead of exhausting one capability against every mode.

DETERMINISM. Every id, seed and synthetic fact value is derived from
``sha256`` of the normalised prompt plus the scenario's own index and pair
name. Nothing reads the clock, the filesystem order, or a random source.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Dict, List, Tuple

from .scenario import KIND, VERSION, validate_scenario_doc

__all__ = [
    "FAILURE_MODES",
    "normalize_prompt",
    "prompt_digest",
    "extract",
    "generate_suite",
    "write_suite",
    "render_json_text",
    "render_text_summary",
]

# --- 1/2: normalisation + sentence split -----------------------------------

_BULLET_RE = re.compile(r"^\s*(?:[-*•–]|\d+[.)])\s+")
_WS_RE = re.compile(r"[ \t]+")
_SENTENCE_SPLIT_RE = re.compile(r"[.;!?\n]+")


def normalize_prompt(text: str) -> str:
    """The canonical form every hash and every rule reads. Bullet markers and
    whitespace are cosmetic: normalising them away means re-indenting a prompt
    does not rewrite every id in the suite."""
    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        bulleted = bool(_BULLET_RE.match(raw))
        line = _WS_RE.sub(" ", _BULLET_RE.sub("", raw)).strip()
        lines.append((line, bulleted))
    # UNWRAP hard-wrapped sentences. A prompt pasted from an editor breaks one
    # sentence over several lines, and treating each line as a sentence loses
    # exactly the capabilities listed at the end of a long clause. A line joins
    # the previous one only when the previous does not end in sentence
    # punctuation and this one is neither a bullet nor a new capitalised
    # sentence -- so a bulleted list stays one item per line.
    joined: List[str] = []
    in_colon_list = False
    for line, bulleted in lines:
        if not (line and bulleted):
            in_colon_list = False
        # A LIST INTRODUCED BY A COLON ("You can:" then bullets) is one claim
        # spread over several lines: fold the items back into the introducing
        # line as a comma list, so the stem and its items land in ONE sentence
        # and the list is read as capabilities rather than dropped. A bullet
        # following anything else stays its own line.
        if joined and line and bulleted and (in_colon_list
                                             or joined[-1].endswith(":")):
            joined[-1] = f"{joined[-1].rstrip(':')}, {line}"
            in_colon_list = True
        elif (joined and joined[-1] and line and not bulleted
                and joined[-1][-1] not in ".;:!?"
                and not line[0].isupper()):
            joined[-1] = f"{joined[-1]} {line}"
        else:
            joined.append(line)
    # Collapse runs of blank lines so edits to spacing alone are inert.
    out: List[str] = []
    for line in joined:
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    return "\n".join(out).strip()


def prompt_digest(normalized: str) -> str:
    """sha256 of the normalised prompt: the root of every id and seed."""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _sentences(normalized: str) -> List[str]:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(normalized) if s.strip()]


# --- 3: capabilities --------------------------------------------------------

_CAPABILITY_STEMS = [
    r"you can\b",
    r"you are able to\b",
    r"you're able to\b",
    r"you may\b",
    r"you must be able to\b",
    r"you should be able to\b",
    r"your job is to\b",
    r"your role is to\b",
    r"you are responsible for\b",
    r"you handle\b",
    r"you help (?:callers|customers|users|patients|people|them) (?:to )?",
    r"you assist (?:callers|customers|users|patients|people|them) (?:with|to) ",
    r"you are here to\b",
]
_CAPABILITY_STEM_RE = re.compile("|".join(_CAPABILITY_STEMS))

# Verbs a voice agent plausibly claims. The lexicon exists to reject parse
# artefacts ("you can also see the notes" is not a capability), not to be
# exhaustive -- a clause with a single unknown verb is still kept, because a
# lone verb is usually the real claim.
_VERB_LEXICON = {
    "add", "answer", "apply", "authorize", "book", "cancel", "change", "check",
    "collect", "confirm", "connect", "create", "deliver", "escalate", "explain",
    "file", "find", "handle", "issue", "locate", "look", "modify", "open",
    "order", "pay", "place", "process", "provide", "quote", "read", "record",
    "refill", "refund", "register", "renew", "replace", "report", "request",
    "reschedule", "reset", "resolve", "return", "route", "schedule", "send",
    "set", "start", "submit", "switch", "take", "track", "transfer", "update",
    "upgrade", "verify",
}

_SEGMENT_SPLIT_RE = re.compile(r",\s*|\s+and\s+|\s+or\s+|\s*/\s*")
_LEADING_FILLER_RE = re.compile(
    r"^(?:and|or|also|then|either|both|to|the|a|an|only|always|simply)\s+")
_TRAILING_FUNCTION_WORDS = {
    "for", "to", "of", "on", "in", "with", "at", "by", "the", "a", "an",
    "their", "his", "her", "its", "and", "or", "if", "when", "that", "as",
    "from",
}
_OBJECT_LEAD_WORDS = {"the", "a", "an", "their", "his", "her", "its", "any",
                      "all", "new", "existing", "up", "into", "for", "them"}


def _clean_phrase(tokens: List[str], limit: int) -> List[str]:
    out = list(tokens[:limit])
    while out and out[-1] in _TRAILING_FUNCTION_WORDS:
        out.pop()
    return out


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "unnamed"


def _extract_capabilities(sentences: List[str]) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    seen = set()
    for sent in sentences:
        low = sent.lower()
        m = _CAPABILITY_STEM_RE.search(low)
        if not m:
            continue
        clause = low[m.end():].strip()
        clause = re.sub(r"^(?:to|the)\s+", "", clause)
        # Everything after a subordinating marker states a condition, not a
        # claim -- keeping it would put "if the caller asks" inside the goal.
        clause = re.split(r"\b(?:when|if|because|so that|but|however)\b",
                          clause)[0]
        segments = [s.strip() for s in _SEGMENT_SPLIT_RE.split(clause) if s.strip()]
        if not segments:
            continue
        parsed: List[Tuple[str, List[str]]] = []
        for seg in segments:
            seg = _LEADING_FILLER_RE.sub("", seg).strip()
            tokens = [t for t in re.findall(r"[a-z0-9']+", seg) if t]
            if not tokens:
                continue
            verb = tokens[0]
            rest = tokens[1:]
            # "look up an order" / "set up a plan": the particle belongs to the
            # verb, not the object.
            if rest and rest[0] in ("up", "into", "out", "over", "through"):
                verb = f"{verb} {rest[0]}"
                rest = rest[1:]
            obj = _clean_phrase([t for t in rest if t not in _OBJECT_LEAD_WORDS], 4)
            # The SPOKEN form keeps the prompt's own articles ("check the
            # status of an order"), because the caller's opening line has to
            # sound like a person; the article-stripped form is what the slug
            # and the goal type are built from.
            spoken = " ".join(_clean_phrase([verb] + rest, 6))
            parsed.append((verb, obj, spoken))
        # One object shared across several verbs: "book, cancel or reschedule
        # appointments" names its object once, at the END of the list.
        last_obj = next((o for _, o, _s in reversed(parsed) if o), [])
        for verb, obj, spoken in parsed:
            head = verb.split()[0]
            if head not in _VERB_LEXICON and len(parsed) > 1:
                continue
            obj = obj or last_obj
            phrase = " ".join([verb] + list(obj)).strip()
            slug = _slug(phrase)
            if slug in seen:
                continue
            seen.add(slug)
            found.append({
                "slug": slug,
                "verb": verb,
                "object": " ".join(obj),
                "phrase": phrase,
                "spoken": spoken or phrase,
                "source_sentence": sent,
            })
    return found


# --- 4: required data -------------------------------------------------------

_COLLECT_PATTERNS = [
    re.compile(r"\bask (?:the caller |the customer |the patient |them |him |her )?"
               r"for (?:the |their |a |an |his |her )?([a-z0-9 '\-]{3,50})"),
    re.compile(r"\b(?:collect|obtain|request|capture|verify|confirm)"
               r"(?: the| their| a| an| his| her| caller's| customer's)? "
               r"([a-z0-9 '\-]{3,50})"),
    re.compile(r"\byou (?:must|should|always) (?:get|have) "
               r"(?:the |their )?([a-z0-9 '\-]{3,50})"),
]
_DATA_STOP_HEADS = {"that", "this", "it", "them", "they", "what", "whether",
                    "why", "how", "any", "all", "the", "and", "or", "you",
                    "with", "before", "back", "again"}


def _extract_required_data(sentences: List[str]) -> List[Dict[str, str]]:
    found: List[Dict[str, str]] = []
    seen = set()
    for sent in sentences:
        low = sent.lower()
        for pat in _COLLECT_PATTERNS:
            for m in pat.finditer(low):
                raw = re.split(r"\b(?:before|so|then|when|if|and)\b",
                               m.group(1))[0]
                tokens = _clean_phrase(re.findall(r"[a-z0-9']+", raw), 4)
                tokens = [t for t in tokens
                          if t not in ("caller's", "customer's", "patient's")]
                if not tokens or tokens[0] in _DATA_STOP_HEADS:
                    continue
                label = " ".join(tokens)
                key = _slug(label).replace("-", "_")
                if key in seen:
                    continue
                seen.add(key)
                found.append({"key": key, "label": label})
    return found


# --- 5: refusals / constraints ---------------------------------------------

_REFUSAL_PATTERNS = [
    re.compile(r"\bunder no circumstances (?:should you |may you |do you )?(.+)$"),
    re.compile(r"\byou (?:must not|may not|should never|can never) (.+)$"),
    re.compile(r"\b(?:you )?(?:do not|don't|never) (.+)$"),
    re.compile(r"\brefuse to (.+)$"),
    re.compile(r"\bare not allowed to (.+)$"),
]


def _extract_refusals(sentences: List[str]) -> List[str]:
    found: List[str] = []
    seen = set()
    for sent in sentences:
        low = sent.lower()
        for pat in _REFUSAL_PATTERNS:
            m = pat.search(low)
            if not m:
                continue
            text = " ".join(_clean_phrase(m.group(1).split(), 10))
            if not text or text in seen:
                continue
            seen.add(text)
            found.append(text)
            break
    return found


# --- 6: domain nouns --------------------------------------------------------

_STOPWORDS = {
    "about", "after", "agent", "also", "always", "answer", "anything",
    "assistant", "before", "being", "call", "caller", "callers", "cannot",
    "customer", "customers", "does", "doing", "done", "each", "else", "ever",
    "every", "from", "give", "have", "help", "here", "into", "just", "keep",
    "know", "like", "make", "many", "more", "most", "much", "must", "need",
    "never", "note", "only", "other", "over", "please", "provide", "said",
    "same", "should", "since", "some", "such", "sure", "take", "tell", "than",
    "that", "their", "them", "then", "there", "these", "they", "thing", "this",
    "those", "through", "time", "under", "user", "users", "very", "want",
    "well", "what", "when", "where", "which", "while", "will", "with",
    "without", "would", "your", "yours", "voice", "speak", "conversation",
    "response", "responses", "short", "friendly", "polite", "asks", "asked",
    "asking", "confirm", "verify", "collect",
}


def _domain_nouns(normalized: str, limit: int = 8) -> List[str]:
    counts: Dict[str, int] = {}
    first: Dict[str, int] = {}
    for i, tok in enumerate(re.findall(r"[a-z][a-z0-9'\-]{3,}", normalized.lower())):
        if tok in _STOPWORDS:
            continue
        counts[tok] = counts.get(tok, 0) + 1
        first.setdefault(tok, i)
    ranked = sorted(counts, key=lambda t: (-counts[t], first[t], t))
    return ranked[:limit]


def extract(prompt_text: str) -> Dict[str, Any]:
    """Run every extraction rule over one system prompt. Pure: same text in,
    same dict out, with no clock, no filesystem and no model involved."""
    normalized = normalize_prompt(prompt_text)
    sents = _sentences(normalized)
    return {
        "normalized": normalized,
        "sha256": prompt_digest(normalized),
        "capabilities": _extract_capabilities(sents),
        "required_data": _extract_required_data(sents),
        "refusals": _extract_refusals(sents),
        "domain_nouns": _domain_nouns(normalized),
        "sentences": len(sents),
    }


# --- the failure taxonomy ---------------------------------------------------

# The seven shipped corpus classes (corpus/classes/manifest.json) plus the four
# turn-taking failure kinds the scorer measures. Each entry declares how the
# scripted caller EXERCISES that failure: the extra turns it speaks, the
# behaviour it declares, the environment it renders in, and the variation
# matrix it expands through. Static by design -- a mode's shape must not drift
# between two runs of the generator, or the suite stops being diffable.
FAILURE_MODES: List[Dict[str, Any]] = [
    {
        "slug": "barge-in",
        "source": "scored-failure-kind",
        "note": "the caller cuts in while the agent is still speaking; the "
                "agent should yield rather than talk over",
        "turns": [{"say": "Sorry, one second, I need to change something."}],
        "behavior": {"interruptions": [{"trigger": "agent_speaking",
                                        "offset_ms": 400}]},
        "environment": {"noise": "clean"},
        "matrix": {"behavior": ["barge_in"], "speaking_rate": [1.0, 1.25],
                   "repetitions": 2},
    },
    {
        "slug": "dead-air",
        "source": "scored-failure-kind",
        "note": "the caller stops talking and waits; the agent should not "
                "leave the line silent",
        "turns": [{"after": "agent_silence",
                   "say": "Hello? Are you still there?"}],
        "behavior": {"speaking_rate": 0.85},
        "environment": {"noise": "clean"},
        "matrix": {"speaking_rate": [0.85, 1.0], "repetitions": 2},
    },
    {
        "slug": "latency-spike",
        "source": "scored-failure-kind",
        "note": "the caller speaks over a slow response path; a late reply "
                "should still land in the right turn",
        "turns": [{"say": "Sorry, did you catch that?"}],
        "behavior": {"speaking_rate": 1.3},
        "environment": {"noise": "clean"},
        "matrix": {"speaking_rate": [1.0, 1.3, 1.6], "repetitions": 2},
    },
    {
        "slug": "echo",
        "source": "scored-failure-kind",
        "note": "the agent's own audio returns on the caller channel; it must "
                "not be treated as a caller turn",
        "turns": [{"say": "There is an echo on the line, can you hear me?"}],
        "behavior": {"backchannels": {"probability": 0.0}},
        "environment": {"noise": "echo"},
        "matrix": {"noise": ["clean", "echo"], "repetitions": 2},
    },
    {
        "slug": "backchannel-multilingual",
        "source": "corpus-class",
        "note": "non-English acknowledgement tokens over agent speech; the "
                "agent should NOT yield to them",
        "turns": [{"say": "mhm"}, {"say": "ja, ja"}, {"say": "hai"}],
        "behavior": {"backchannels": {"probability": 1.0}},
        "environment": {"noise": "clean"},
        "matrix": {"locale": ["en-US", "de-DE", "ja-JP"], "repetitions": 2},
    },
    {
        "slug": "leading-edge-onset",
        "source": "corpus-class",
        "note": "a short leading burst at the interruption boundary; dropped "
                "leading audio is measurable against the ground-truth onset",
        "turns": [{"say": "Wait--"}, {"say": "Sorry, go on."}],
        "behavior": {"interruptions": [{"trigger": "agent_speaking",
                                        "offset_ms": 80}]},
        "environment": {"noise": "clean"},
        "matrix": {"behavior": ["barge_in"], "repetitions": 3},
    },
    {
        "slug": "mid-utterance-pause",
        "source": "corpus-class",
        "note": "a thinking pause mid-utterance; the agent should not "
                "endpoint on it",
        "turns": [{"say": "It is, hold on, let me find it."},
                  {"say": "Yes, that is the one."}],
        "behavior": {"speaking_rate": 0.8},
        "environment": {"noise": "clean"},
        "matrix": {"speaking_rate": [0.8, 1.0], "repetitions": 2},
    },
    {
        "slug": "noise-hold",
        "source": "corpus-class",
        "note": "sustained ambient energy on the caller channel; the agent "
                "should NOT yield to it",
        "turns": [{"say": "Sorry about the background, I am out on the street."}],
        "behavior": {"backchannels": {"probability": 0.0}},
        "environment": {"noise": "cafe"},
        "matrix": {"noise": ["cafe", "street"], "repetitions": 2},
    },
    {
        "slug": "structured-utterance",
        "source": "corpus-class",
        "note": "the caller reads structured data with intra-item gaps; an "
                "intra-item pause is not the end of the turn",
        "turns": [{"say": "It is five five five, zero one, double two."},
                  {"say": "And my email is d-a-n-a at example dot com."}],
        "behavior": {"speaking_rate": 0.9},
        "environment": {"noise": "clean"},
        "matrix": {"speaking_rate": [0.9, 1.0], "repetitions": 2},
    },
    {
        "slug": "telephony-degraded",
        "source": "corpus-class",
        "note": "the same conversation over a degraded 8 kHz line (mu-law "
                "plus mild packet loss)",
        "turns": [{"say": "The line is breaking up, can you repeat that?"}],
        "behavior": {"backchannels": {"probability": 0.0}},
        "environment": {"noise": "line_noise", "codec": "g711_ulaw"},
        "matrix": {"noise": ["line_noise", "clean"], "repetitions": 2},
    },
    {
        "slug": "browser-telephony-parity",
        "source": "corpus-class",
        "note": "one conversation rendered on a clean browser leg and on a "
                "telephony leg; behaviour should match across both",
        "turns": [{"say": "I am calling from my laptop this time."}],
        "behavior": {"backchannels": {"probability": 0.0}},
        "environment": {"noise": "clean", "codec": "opus"},
        "matrix": {"noise": ["clean", "line_noise"], "repetitions": 2},
    },
]


# --- synthetic ground truth -------------------------------------------------

_FIRST_NAMES = ["Dana", "Priya", "Marcus", "Lena", "Omar", "Sofia", "Tobias",
                "Yuki"]
_LAST_NAMES = ["Whitfield", "Okafor", "Ramirez", "Lindqvist", "Haddad",
               "Novak", "Brennan", "Tanaka"]


def _hash_int(*parts: str) -> int:
    return int(hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest(), 16)


def _synthetic_value(label: str, salt: str) -> str:
    """A deterministic PLACEHOLDER for a datum the prompt says the agent must
    collect. Shaped by the label's own words so it reads like the real thing in
    a transcript -- and it is still a placeholder the user replaces with their
    own ground truth before the suite means anything about their data."""
    h = _hash_int(label, salt)
    low = label.lower()
    if "email" in low:
        return (f"{_FIRST_NAMES[h % len(_FIRST_NAMES)].lower()}."
                f"{_LAST_NAMES[(h // 7) % len(_LAST_NAMES)].lower()}@example.com")
    if "phone" in low or "mobile" in low or "callback" in low:
        return f"555-01{h % 100:02d}"
    if "zip" in low or "postal" in low or "postcode" in low:
        return str(10000 + (h % 89999))
    if "birth" in low or "dob" in low or "date" in low:
        return f"19{60 + (h % 40)}-{1 + (h // 3) % 12:02d}-{1 + (h // 5) % 28:02d}"
    if "name" in low:
        return (f"{_FIRST_NAMES[h % len(_FIRST_NAMES)]} "
                f"{_LAST_NAMES[(h // 11) % len(_LAST_NAMES)]}")
    if "address" in low:
        return f"{100 + h % 900} Maple Street"
    if "amount" in low or "total" in low or "price" in low:
        return f"{10 + h % 200}.{h % 100:02d}"
    return f"{chr(ord('A') + h % 26)}-{1000 + h % 9000}"


# --- generation -------------------------------------------------------------

def _pair_order(n_caps: int, n_modes: int) -> List[Tuple[int, int]]:
    """Diagonal traversal of the capability x mode grid. A --max truncation of
    a capability-major order would spend the whole budget on capability 0; the
    diagonal spends it across both axes, so a small suite still says something
    about several capabilities AND several failure modes."""
    pairs = [(c, m) for c in range(n_caps) for m in range(n_modes)]
    return sorted(pairs, key=lambda p: (p[0] + p[1], p[1], p[0]))


def _goal_target(cap: Dict[str, Any], nouns: List[str]) -> str:
    if cap["object"]:
        return cap["object"]
    return nouns[0] if nouns else "the caller's request"


def _caller_script(cap: Dict[str, Any], mode: Dict[str, Any],
                   facts: Dict[str, str], refusal: str) -> List[Dict[str, Any]]:
    script: List[Dict[str, Any]] = [{"say": f"Hi, I need to {cap['spoken']}."}]
    # The caller VOLUNTEERS its ground truth only when asked for it: whether
    # the agent asks at all is the thing under test, so the turn is gated.
    for key, value in facts.items():
        script.append({"when_agent_asks": key,
                       "say": f"My {key.replace('_', ' ')} is {value}."})
    for turn in mode["turns"]:
        script.append(dict(turn))
    if refusal:
        # Rotating one constraint probe through the suite is how a stated
        # refusal gets exercised at all: the caller ASKS for the thing the
        # prompt says the agent will not do, and a human reads what it did.
        script.append({"say": f"Also, before we finish -- can you {refusal}?"})
    script.append({"say": "That is everything, thank you."})
    return script


def _build_scenario(cap: Dict[str, Any], mode: Dict[str, Any], index: int,
                    digest: str, extraction: Dict[str, Any],
                    stack: str) -> Dict[str, Any]:
    salt = f"{digest}:{index}:{cap['slug']}:{mode['slug']}"
    suffix = hashlib.sha256(salt.encode("utf-8")).hexdigest()[:8]
    facts = {d["key"]: _synthetic_value(d["label"], salt)
             for d in extraction["required_data"]}
    refusals = extraction["refusals"]
    refusal = refusals[index % len(refusals)] if refusals else None
    env = dict(mode["environment"])
    env.setdefault("locale", "en-US")
    env["route"] = "phone"
    if stack:
        env["stack"] = stack
    return {
        "kind": KIND,
        "version": VERSION,
        "id": f"{cap['slug']}-{mode['slug']}-{suffix}",
        "goal": {"type": cap["slug"].replace("-", "_"),
                 "target": _goal_target(cap, extraction["domain_nouns"])},
        "facts": facts,
        "caller": {
            "script": _caller_script(cap, mode, facts, refusal),
            "behavior": dict(mode["behavior"]),
        },
        "environment": env,
        "variation_matrix": dict(mode["matrix"]),
        # A 31-bit seed off the same digest: a fixed prompt gives a fixed seed,
        # and two scenarios in one suite never collide by accident.
        "seed": _hash_int(salt) % (2 ** 31),
        "generated_from": {
            "generator": "hotato.scenario_gen",
            "prompt_sha256": digest,
            "capability": cap["phrase"],
            "capability_source": cap["source_sentence"],
            "failure_mode": mode["slug"],
            "failure_mode_source": mode["source"],
            "failure_mode_note": mode["note"],
            "constraint_probed": refusal,
            "note": "Generated from a system prompt by deterministic string "
                    "matching -- a starting point to edit, not a claim that "
                    "these are the right caller turns, the right ground truth, "
                    "or a complete suite for this agent.",
        },
    }


def generate_suite(prompt_text: str, stack: str = None,
                   max_scenarios: int = None) -> Dict[str, Any]:
    """Extract from ``prompt_text`` and return the generated suite: the
    extraction summary plus the ordered ``(filename, doc)`` scenarios. Every
    doc is validated HERE, so a generator bug surfaces as an exit-2 error
    rather than as a directory of files that fail at run time."""
    extraction = extract(prompt_text)
    caps = extraction["capabilities"]
    if not caps:
        raise ValueError(
            "no capabilities found in the prompt: nothing matched a capability "
            "stem (\"you can ...\", \"your job is to ...\", \"you handle ...\"). "
            "Add a sentence naming what the agent does, or author a scenario by "
            "hand with `hotato lab scenario init`"
        )
    if max_scenarios is not None and max_scenarios < 1:
        raise ValueError("--max must be 1 or greater")

    order = _pair_order(len(caps), len(FAILURE_MODES))
    if max_scenarios is not None:
        order = order[:max_scenarios]

    digest = extraction["sha256"]
    items: List[Tuple[str, Dict[str, Any]]] = []
    for index, (ci, mi) in enumerate(order):
        cap, mode = caps[ci], FAILURE_MODES[mi]
        doc = _build_scenario(cap, mode, index, digest, extraction, stack)
        validate_scenario_doc(doc)
        items.append((f"{index:03d}-{doc['id']}.scenario.json", doc))
    return {
        "prompt_sha256": digest,
        "capabilities": [c["phrase"] for c in caps],
        "required_data": [d["label"] for d in extraction["required_data"]],
        "refusals": extraction["refusals"],
        "failure_modes": [m["slug"] for m in FAILURE_MODES],
        "scenarios": items,
    }


def render_json_text(doc: Dict[str, Any]) -> str:
    """One serialisation, sorted keys, trailing newline -- so two runs of the
    generator produce byte-identical files and a reviewer can diff a suite."""
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def write_suite(suite: Dict[str, Any], out_dir: str) -> List[str]:
    """Write every scenario into ``out_dir`` (created if absent), each through
    a temp file + rename so an interrupted run never leaves a half-written
    scenario that the validator would then reject."""
    os.makedirs(out_dir, exist_ok=True)
    written = []
    for filename, doc in suite["scenarios"]:
        path = os.path.join(out_dir, filename)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(render_json_text(doc))
        os.replace(tmp, path)
        written.append(path)
    return written


def render_text_summary(suite: Dict[str, Any], out_dir: str) -> str:
    """What the generator DID -- never what the suite proves. There is no
    coverage or completeness claim here because a string matcher cannot earn
    one."""
    n_caps = len(suite["capabilities"])
    lines = [
        f"read the prompt (sha256 {suite['prompt_sha256'][:12]}): "
        f"{n_caps} capabilit{'y' if n_caps == 1 else 'ies'}, "
        f"{len(suite['required_data'])} required data field(s), "
        f"{len(suite['refusals'])} stated constraint(s)",
        f"wrote {len(suite['scenarios'])} scenario file(s) to {out_dir}, "
        f"drawn from {len(suite['failure_modes'])} failure modes",
    ]
    lines.extend(f"  capability: {p}" for p in suite["capabilities"])
    lines.append(
        "a starting point you edit: the facts are synthetic placeholders, and "
        "a generator cannot know your agent's domain rules or which caller "
        "behaviours matter for your call"
    )
    lines.append(f"next: hotato lab scenario validate {out_dir}")
    return "\n".join(lines) + "\n"
