#!/usr/bin/env python3
"""Build hotato's real-audio example set from the AMI Meeting Corpus.

Source: AMI Meeting Corpus (CC BY 4.0), individual headset microphones (IHM),
one close-talking channel per participant, plus the corpus's manual word-level
alignments. Provenance, license quotes, and attribution: see LICENSES.md and
README.md next to this file.

What this produces, deterministically:
  corpus/real/scenarios/<id>.json     scenario labels (annotation-derived)
  corpus/real/audio/<id>.example.wav  2-channel 16 kHz PCM clips
  corpus/real/manifest.json           sources, checksums, clips, measurements

Pipeline:
  1. Download the pinned source files into corpus/real/cache/ (sha256-verified,
     cached; nothing is re-fetched once the checksum matches).
  2. For each pinned clip spec (curated from `--scan` output), cut the SAME
     time window from BOTH speakers' headset channels: ch0 = the party that
     starts talking (the "caller" role), ch1 = the party already holding the
     floor (the "agent" role).
  3. Write the scenario JSON with the REAL onset time taken from the AMI word
     alignments, the word-derived speech segments, the transcript window, and
     the full provenance + attribution chain.
  4. Score every clip with the packaged scorer and record the measurements in
     the manifest, next to the annotation-derived ground truth.

Labels are honest about what they are: `category` states what the HUMAN floor
holder actually did (yielded to a genuine floor-take, or kept talking through a
backchannel), derived from the human transcript timings. Both parties are
human. No accuracy percentage is derived or claimed anywhere.

Usage:
  python3 corpus/real/build_real.py            # build (downloads on first run)
  python3 corpus/real/build_real.py --check    # rebuild to a temp dir, byte-compare
  python3 corpus/real/build_real.py --scan     # mine candidate events (dev tool)

Stdlib only. Network is used only to download the pinned AMI files.
"""

from __future__ import annotations

import argparse
import array
import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.request
import wave
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.abspath(__file__))  # corpus/real
REPO = os.path.dirname(os.path.dirname(ROOT))
CACHE = os.path.join(ROOT, "cache")
SCENARIOS_DIR = os.path.join(ROOT, "scenarios")
AUDIO_DIR = os.path.join(ROOT, "audio")
MANIFEST = os.path.join(ROOT, "manifest.json")

TARGET_RATE = 16000
WIN_LEAD_SEC = 3.0   # seconds of context before the annotated onset
WIN_DUR_SEC = 9.0    # total clip length
AUDIO_SUFFIX = ".example.wav"  # run_suite's default suffix convention

# --- pinned sources ---------------------------------------------------------
# The AMI corpus publishes no checksums; these sha256 values pin the exact
# bytes this tree was built from (access date in LICENSES.md). A re-download
# that does not match fails loudly instead of building from different data.

ANNOT_URL = (
    "https://groups.inf.ed.ac.uk/ami/AMICorpusAnnotations/"
    "ami_public_manual_1.6.2.zip"
)
ANNOT_SHA256 = "b56e5babb2496b8795deeeda7e71178d7fbc9963f94276cf2a3f4b56ebbc9f9d"
ANNOT_ZIP = os.path.join(CACHE, "ami_public_manual_1.6.2.zip")

AUDIO_URL_TMPL = (
    "https://groups.inf.ed.ac.uk/ami/AMICorpusMirror/amicorpus/"
    "{meeting}/audio/{meeting}.Headset-{ch}.wav"
)

# sha256 of each source headset WAV actually used, pinned after the first
# verified download (filled in by --pin-audio, committed with this script).
AUDIO_SHA256 = {
    "EN2002b.Headset-2.wav": "2f01166f3895c987e91f4ad47426ac38ac80e900d4b6ee4da125112d7292b197",
    "EN2002b.Headset-3.wav": "68f495547ef9674d1d6848c12d7358829cc6e6d302268c0c51c3e3c470b7f692",
    "ES2002a.Headset-1.wav": "285ed5b5eaa4f871cc146900d0cf87487ca757aa57a7062def2eb49feaabc23d",
    "ES2002a.Headset-3.wav": "fa65d53a97074e31bf83da4ab4cc6235f6e7625d6d40bd223158467d7df9f581",
}

LICENSE_ID = "CC-BY-4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"
DATASET_NAME = "AMI Meeting Corpus"
DATASET_URL = "https://groups.inf.ed.ac.uk/ami/corpus/"

# Short acknowledgement tokens. A caller event whose words are ALL in this set
# (and short, and mid-floor) is labelled a backchannel, per the transcript.
BACKCHANNEL_WORDS = {
    "mm-hmm", "mm", "mmm", "hmm", "hm", "uh-huh", "mhm", "yeah", "yep",
    "right", "okay", "ok", "oh", "sure", "yes",
}

# --- curated clips ----------------------------------------------------------
# Pinned specs curated from `--scan` output. Every number here is an AMI
# word-alignment time in GLOBAL meeting seconds. `agent` is the speaker who
# holds the floor at the onset (clip channel 1); `caller` is the speaker who
# starts talking at `onset` (clip channel 0). `floor_end` (floor-takes only)
# is when the floor holder's spurt actually ended, per the same alignments.


def _take(clip_id, meeting, agent, caller, onset, floor_end, why, notes=None):
    return {
        "id": clip_id, "category": "should_yield", "meeting": meeting,
        "agent": agent, "caller": caller, "onset": onset,
        "floor_end": floor_end,
        "title": f"Real floor-take: caller interrupts and takes the turn ({meeting})",
        "tags": ["real-audio", "ami", "meeting", "interruption", "barge-in"],
        "why": why, "notes": notes,
    }


def _bc(clip_id, meeting, agent, caller, onset, text, why, notes=None):
    return {
        "id": clip_id, "category": "should_not_yield", "meeting": meeting,
        "agent": agent, "caller": caller, "onset": onset,
        "title": f"Real backchannel: {text!r} while the floor holder talks ({meeting})",
        "tags": ["real-audio", "ami", "meeting", "backchannel", "false-trigger"],
        "why": why, "notes": notes,
    }


# A curation pass dropped four extracted candidates whose measurement was not
# meaningful on the energy reference (agent read as silent at the labeled
# onset, or headset bleed flooding the quiet channel): ami-es2002a-take-0615,
# ami-es2002a-take-1073, ami-es2002a-bc-0067, ami-es2002a-bc-0866. The kept
# set below is what survived; known imperfections are stated per clip.
_BLEED_NOTE = (
    "Headset bleed: the floor holder's voice is audible on the quiet caller "
    "channel, so onset AUTO-detection fires early on this clip. The labeled "
    "caller_onset_sec (from the word alignments) is the usable onset."
)
_MICROPAUSE_NOTE = (
    "The human floor holder pauses briefly right around the backchannel; the "
    "energy VAD counts a pause of 0.25 s or more with the caller nearby as a "
    "yield, so this clip measures did_yield=true even though the transcript "
    "shows the floor holder kept the turn. Real behavior, honestly labeled."
)

CLIPS = [
    # --- ES2002a (scenario meeting; speakers FEE005 "B" and MEE008 "D")
    _take("ami-es2002a-take-0677", "ES2002a", "D", "B", 677.15, 677.53,
          "B breaks in with a question about remote control cost while D is "
          "talking; D stops almost immediately (0.38 s per the alignments).",
          notes=_BLEED_NOTE),
    _bc("ami-es2002a-bc-0526", "ES2002a", "B", "D", 526.79, "Okay",
        "D drops one 'Okay' inside B's turn; B keeps the floor per the "
        "transcript.", notes=_MICROPAUSE_NOTE),
    _bc("ami-es2002a-bc-0687", "ES2002a", "B", "D", 687.72, "Mm-hmm",
        "D acknowledges mid-turn; B keeps talking per the transcript.",
        notes=_MICROPAUSE_NOTE),
    _bc("ami-es2002a-bc-1049", "ES2002a", "D", "B", 1049.54, "Mm-hmm",
        "B acknowledges while D talks; D keeps the floor per the transcript.",
        notes=_MICROPAUSE_NOTE),
    # --- EN2002b (non-scenario meeting; speakers FEO072 "C" and MEE073 "D")
    _take("ami-en2002b-take-0149", "EN2002b", "C", "D", 149.11, 150.21,
          "D takes the floor with a long explanation while C is mid-utterance; "
          "C stops within 1.1 s."),
    _take("ami-en2002b-take-0772", "EN2002b", "C", "D", 772.04, 772.64,
          "D breaks in ('Um there is but I haven't made a lot of changes...') "
          "while C is talking; C stops in 0.6 s."),
    _take("ami-en2002b-take-0913", "EN2002b", "C", "D", 913.03, 913.54,
          "D pushes back while C is talking; C stops in about half a second."),
    _take("ami-en2002b-take-0930", "EN2002b", "D", "C", 930.80, 933.07,
          "C comes in with a counter-proposal while D is talking; D trails "
          "off within 2.3 s per the alignments. The trailing words are "
          "quiet: the energy VAD reads the agent as done at the onset "
          "itself, earlier than the word alignment suggests."),
    _take("ami-en2002b-take-1069", "EN2002b", "D", "C", 1069.55, 1070.60,
          "C takes the turn while D is mid-utterance; D stops within 1.1 s."),
    _bc("ami-en2002b-bc-0859", "EN2002b", "D", "C", 859.80, "Mm-hmm",
        "C acknowledges while D explains; D talks through it per the "
        "transcript.", notes=_MICROPAUSE_NOTE),
    _bc("ami-en2002b-bc-1049", "EN2002b", "D", "C", 1049.91, "Oh mm-hmm",
        "C acknowledges mid-turn; D keeps the floor for many more seconds "
        "per the transcript.", notes=_MICROPAUSE_NOTE),
    _bc("ami-en2002b-bc-1114", "EN2002b", "C", "D", 1114.86, "Okay",
        "D drops one 'Okay' inside C's turn; C keeps talking. The energy "
        "reference also measures no yield here."),
    _bc("ami-en2002b-bc-1416", "EN2002b", "D", "C", 1416.84, "Oh right",
        "C reacts with a short 'Oh right' while D explains; D keeps the "
        "floor for a long stretch per the transcript.",
        notes=_MICROPAUSE_NOTE),
]


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, dest: str, sha256: str | None) -> None:
    if os.path.exists(dest) and (sha256 is None or _sha256(dest) == sha256):
        return
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    print(f"downloading {url}", file=sys.stderr)
    with urllib.request.urlopen(url) as resp, open(tmp, "wb") as out:
        shutil.copyfileobj(resp, out)
    got = _sha256(tmp)
    if sha256 is not None and got != sha256:
        os.remove(tmp)
        raise SystemExit(
            f"sha256 mismatch for {url}\n  expected {sha256}\n  got      {got}\n"
            "The upstream file changed; re-verify the source before rebuilding."
        )
    os.replace(tmp, dest)


def _ensure_annotations() -> None:
    _download(ANNOT_URL, ANNOT_ZIP, ANNOT_SHA256)
    words_dir = os.path.join(CACHE, "words")
    res_dir = os.path.join(CACHE, "corpusResources")
    if not (os.path.isdir(words_dir) and os.path.isdir(res_dir)):
        import zipfile

        with zipfile.ZipFile(ANNOT_ZIP) as zf:
            members = [
                m for m in zf.namelist()
                if m.startswith("words/") or m.startswith("corpusResources/")
            ]
            zf.extractall(CACHE, members)


def _headset_path(meeting: str, ch: int) -> str:
    return os.path.join(CACHE, f"{meeting}.Headset-{ch}.wav")


def _ensure_headset(meeting: str, ch: int) -> str:
    dest = _headset_path(meeting, ch)
    key = f"{meeting}.Headset-{ch}.wav"
    _download(AUDIO_URL_TMPL.format(meeting=meeting, ch=ch), dest,
              AUDIO_SHA256.get(key))
    return dest


# --- annotations ------------------------------------------------------------

def channel_map(meeting: str) -> dict:
    """nxt agent letter -> headset channel number, from corpusResources/meetings.xml."""
    path = os.path.join(CACHE, "corpusResources", "meetings.xml")
    root = ET.parse(path).getroot()
    for m in root.iter("meeting"):
        if m.get("observation") == meeting:
            out = {}
            for spk in m.iter("speaker"):
                out[spk.get("nxt_agent")] = {
                    "channel": int(spk.get("channel")),
                    "global_name": spk.get("global_name"),
                }
            return out
    raise SystemExit(f"meeting {meeting!r} not found in meetings.xml")


def load_words(meeting: str, agent: str) -> list:
    """[(start, end, word), ...] real words only (no punctuation tokens)."""
    path = os.path.join(CACHE, "words", f"{meeting}.{agent}.words.xml")
    root = ET.parse(path).getroot()
    out = []
    for el in root:
        if el.tag.split("}")[-1] != "w" or el.get("punc"):
            continue
        st, en = el.get("starttime"), el.get("endtime")
        if st is None or en is None:
            continue
        st_f, en_f = float(st), float(en)
        if en_f <= st_f:
            continue
        out.append((st_f, en_f, (el.text or "").strip()))
    out.sort()
    return out


def spurts(words: list, gap: float = 0.4) -> list:
    """Merge words into talk spurts: [{start, end, words:[(s,e,w),...]}, ...]."""
    out = []
    for w in words:
        if out and w[0] - out[-1]["end"] <= gap:
            out[-1]["end"] = max(out[-1]["end"], w[1])
            out[-1]["words"].append(w)
        else:
            out.append({"start": w[0], "end": w[1], "words": [w]})
    return out


def _norm(word: str) -> str:
    return "".join(c for c in word.lower() if c.isalnum() or c == "-")


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


# --- candidate mining (--scan) ----------------------------------------------

def _others_speech(all_words: dict, exclude: tuple, t0: float, t1: float) -> float:
    total = 0.0
    for agent, words in all_words.items():
        if agent in exclude:
            continue
        for s, e, _ in words:
            total += _overlap(s, e, t0, t1)
    return total


def scan_meeting(meeting: str) -> list:
    agents = sorted(
        f.split(".")[1]
        for f in os.listdir(os.path.join(CACHE, "words"))
        if f.startswith(meeting + ".") and f.endswith(".words.xml")
    )
    all_words = {a: load_words(meeting, a) for a in agents}
    all_spurts = {a: spurts(w) for a, w in all_words.items()}
    cands = []
    for floor in agents:          # the party holding the floor ("agent" role)
        for caller in agents:     # the party who starts talking ("caller" role)
            if caller == floor:
                continue
            cw = all_words[caller]
            for sb in all_spurts[caller]:
                onset = sb["start"]
                dur = sb["end"] - sb["start"]
                texts = [_norm(w[2]) for w in sb["words"]]
                # caller channel quiet before the onset
                if any(_overlap(s, e, onset - 3.2, onset - 0.15) > 0
                       for s, e, _ in cw):
                    continue
                # the floor holder's spurt spanning the onset
                sa = next(
                    (s for s in all_spurts[floor]
                     if s["start"] <= onset - 1.0 and s["end"] >= onset + 0.30),
                    None,
                )
                if sa is None:
                    continue
                win0, win1 = onset - WIN_LEAD_SEC, onset - WIN_LEAD_SEC + WIN_DUR_SEC
                if win0 < 0:
                    continue
                others_crit = _others_speech(
                    all_words, (floor, caller), onset - 1.0, onset + 3.0)
                others_win = _others_speech(
                    all_words, (floor, caller), win0, win1)
                if others_crit > 0.05 or others_win > 0.75:
                    continue
                nxt = next((s for s in all_spurts[floor] if s["start"] > sa["end"]),
                           None)
                is_bc = (dur <= 1.2 and len(texts) <= 3
                         and all(t in BACKCHANNEL_WORDS for t in texts))
                if is_bc and sa["end"] >= sb["end"] + 1.2:
                    # floor holder keeps talking: max internal word gap near the event
                    gaps = []
                    ws = sa["words"]
                    for i in range(1, len(ws)):
                        g0, g1 = ws[i - 1][1], ws[i][0]
                        if _overlap(g0, g1, onset - 0.7, sb["end"] + 0.7) > 0:
                            gaps.append(round(g1 - g0, 2))
                    cands.append({
                        "kind": "backchannel", "meeting": meeting,
                        "agent": floor, "caller": caller,
                        "onset": round(onset, 2),
                        "caller_dur": round(dur, 2),
                        "caller_text": " ".join(w[2] for w in sb["words"]),
                        "floor_start": round(sa["start"], 2),
                        "floor_end": round(sa["end"], 2),
                        "max_gap_near_event": max(gaps) if gaps else 0.0,
                        "others_win": round(others_win, 2),
                    })
                elif (not is_bc and dur >= 1.2 and len(texts) >= 4
                      and onset + 0.30 <= sa["end"] <= onset + 2.5
                      and (nxt is None or nxt["start"] >= onset + 3.0)):
                    cands.append({
                        "kind": "floor-take", "meeting": meeting,
                        "agent": floor, "caller": caller,
                        "onset": round(onset, 2),
                        "caller_dur": round(dur, 2),
                        "caller_text": " ".join(w[2] for w in sb["words"])[:70],
                        "floor_start": round(sa["start"], 2),
                        "floor_end": round(sa["end"], 2),
                        "annot_yield_sec": round(sa["end"] - onset, 2),
                        "others_win": round(others_win, 2),
                    })
    return cands


def cmd_scan(meetings: list) -> int:
    _ensure_annotations()
    for meeting in meetings:
        try:
            cands = scan_meeting(meeting)
        except FileNotFoundError:
            print(f"{meeting}: no words files", file=sys.stderr)
            continue
        for c in cands:
            print(json.dumps(c, ensure_ascii=False))
    return 0


# --- audio ------------------------------------------------------------------

def _read_window(path: str, start_sec: float, dur_sec: float) -> tuple:
    """Read a mono 16-bit window; returns (array('h'), sample_rate)."""
    with wave.open(path, "rb") as wf:
        if wf.getsampwidth() != 2:
            raise SystemExit(f"{path}: expected 16-bit PCM")
        if wf.getnchannels() != 1:
            raise SystemExit(f"{path}: expected mono headset channel")
        rate = wf.getframerate()
        n0 = int(round(start_sec * rate))
        n = int(round(dur_sec * rate))
        wf.setpos(min(n0, wf.getnframes()))
        raw = wf.readframes(n)
    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder == "big":
        samples.byteswap()
    if len(samples) < n:
        samples.extend([0] * (n - len(samples)))
    return samples, rate


def _resample_to_16k(samples: array.array, rate: int) -> array.array:
    """Deterministic linear resample; AMI IHM is already 16 kHz (no-op)."""
    if rate == TARGET_RATE:
        return samples
    n_out = int(len(samples) * TARGET_RATE / rate)
    out = array.array("h", [0] * n_out)
    for i in range(n_out):
        pos = i * rate / TARGET_RATE
        j = int(pos)
        frac = pos - j
        a = samples[j] if j < len(samples) else 0
        b = samples[j + 1] if j + 1 < len(samples) else a
        out[i] = int(round(a * (1.0 - frac) + b * frac))
    return out


def _write_stereo(path: str, caller: array.array, agent: array.array) -> None:
    n = min(len(caller), len(agent))
    inter = array.array("h", [0] * (2 * n))
    inter[0::2] = caller[:n]
    inter[1::2] = agent[:n]
    if sys.byteorder == "big":
        inter = array.array("h", inter)
        inter.byteswap()
    with wave.open(path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(TARGET_RATE)
        wf.writeframes(inter.tobytes())


def _rms(samples) -> float:
    if not len(samples):
        return 0.0
    return (sum(s * s for s in samples) / len(samples)) ** 0.5


def _verify_channel_mapping(meeting: str, agent_letter: str, claimed_ch: int,
                            other_ch: int, words: list) -> None:
    """Honesty guard: the claimed headset channel must carry more energy inside
    the speaker's own annotated word segments than the other extracted channel.
    Catches a wrong meetings.xml reading before it can mislabel a clip."""
    segs = [w for w in words if w[1] - w[0] >= 0.25][:40]
    if not segs:
        raise SystemExit(f"{meeting}.{agent_letter}: no word segments to verify")
    own, other = [], []
    for s, e, _ in segs:
        a, _r = _read_window(_headset_path(meeting, claimed_ch), s, e - s)
        b, _r = _read_window(_headset_path(meeting, other_ch), s, e - s)
        own.append(_rms(a))
        other.append(_rms(b))
    own_m = sum(own) / len(own)
    other_m = sum(other) / len(other)
    if not own_m > other_m:
        raise SystemExit(
            f"channel mapping check FAILED for {meeting} speaker {agent_letter}: "
            f"claimed Headset-{claimed_ch} rms {own_m:.1f} <= "
            f"Headset-{other_ch} rms {other_m:.1f}"
        )


# --- label + build ------------------------------------------------------------

def _clip_segments(spurt_list: list, win0: float, win1: float) -> list:
    out = []
    for s in spurt_list:
        o = _overlap(s["start"], s["end"], win0, win1)
        if o <= 0.05:
            continue
        out.append([round(max(s["start"], win0) - win0, 2),
                    round(min(s["end"], win1) - win0, 2)])
    return out


def _clip_transcript(words: list, win0: float, win1: float) -> str:
    return " ".join(w for s, e, w in words if _overlap(s, e, win0, win1) > 0)


def build_clip(spec: dict, tmp_scen: str, tmp_audio: str, verified: set) -> dict:
    meeting = spec["meeting"]
    cmap = channel_map(meeting)
    agent_ch = cmap[spec["agent"]]["channel"]
    caller_ch = cmap[spec["caller"]]["channel"]
    agent_words = load_words(meeting, spec["agent"])
    caller_words = load_words(meeting, spec["caller"])
    _ensure_headset(meeting, agent_ch)
    _ensure_headset(meeting, caller_ch)
    for letter, ch, other, words in (
        (spec["agent"], agent_ch, caller_ch, agent_words),
        (spec["caller"], caller_ch, agent_ch, caller_words),
    ):
        key = (meeting, letter)
        if key not in verified:
            _verify_channel_mapping(meeting, letter, ch, other, words)
            verified.add(key)

    onset = spec["onset"]
    win0 = round(onset - WIN_LEAD_SEC, 2)
    win1 = round(win0 + WIN_DUR_SEC, 2)
    onset_rel = round(onset - win0, 2)

    caller_pcm, r1 = _read_window(_headset_path(meeting, caller_ch), win0, WIN_DUR_SEC)
    agent_pcm, r2 = _read_window(_headset_path(meeting, agent_ch), win0, WIN_DUR_SEC)
    caller_pcm = _resample_to_16k(caller_pcm, r1)
    agent_pcm = _resample_to_16k(agent_pcm, r2)

    wav_name = spec["id"] + AUDIO_SUFFIX
    wav_path = os.path.join(tmp_audio, wav_name)
    _write_stereo(wav_path, caller_pcm, agent_pcm)

    should_yield = spec["category"] == "should_yield"
    expected = {"yield": should_yield}
    if should_yield:
        annot_yield = round(spec["floor_end"] - onset, 2)
        # bounds derived from what the human floor holder actually did, plus a
        # stated 0.75 s margin, capped at the scorer's 3.0 s search window
        expected["max_time_to_yield_sec"] = min(3.0, round(annot_yield + 0.75, 2))
        expected["max_talk_over_sec"] = min(3.0, round(annot_yield + 0.75, 2))
    else:
        expected["max_time_to_yield_sec"] = None
        expected["max_talk_over_sec"] = None

    label = {
        "id": spec["id"],
        "title": spec["title"],
        "category": spec["category"],
        "source_type": "real",
        "tags": spec["tags"],
        "audio": wav_name,
        "channels": {"caller_channel": 0, "agent_channel": 1},
        "sample_rate": TARGET_RATE,
        "duration_sec": WIN_DUR_SEC,
        "caller_onset_sec": onset_rel,
        "expected": expected,
        "reference_render": {
            "caller_segments_sec": _clip_segments(
                spurts(caller_words), win0, win1),
            "agent_segments_sec": _clip_segments(
                spurts(agent_words), win0, win1),
        },
        "transcript": {
            "caller": _clip_transcript(caller_words, win0, win1),
            "agent": _clip_transcript(agent_words, win0, win1),
        },
        "related_signals": ["did_yield"],
        "license": LICENSE_ID,
        "attribution": {
            "dataset": DATASET_NAME,
            "dataset_url": DATASET_URL,
            "license": LICENSE_ID,
            "license_url": LICENSE_URL,
            "notice": (
                "Contains audio from the AMI Meeting Corpus "
                "(https://groups.inf.ed.ac.uk/ami/corpus/), licensed under "
                "CC BY 4.0. Changes: cut to a 9 second window, two headset "
                "channels paired as caller/agent, 16 kHz 16-bit PCM."
            ),
        },
        "provenance": {
            "meeting": meeting,
            "window_global_sec": [win0, win1],
            "caller": {
                "nxt_agent": spec["caller"],
                "global_name": cmap[spec["caller"]]["global_name"],
                "headset_wav": f"{meeting}.Headset-{caller_ch}.wav",
            },
            "agent": {
                "nxt_agent": spec["agent"],
                "global_name": cmap[spec["agent"]]["global_name"],
                "headset_wav": f"{meeting}.Headset-{agent_ch}.wav",
            },
            "annotation_files": [
                f"words/{meeting}.{spec['caller']}.words.xml",
                f"words/{meeting}.{spec['agent']}.words.xml",
            ],
            "label_provenance": (
                "caller_onset_sec, segments, and the category come from the AMI "
                "manual word alignments; the category states what the human "
                "floor holder actually did in the recording."
            ),
            "description": spec["why"],
        },
    }
    if spec.get("notes"):
        label["provenance"]["notes"] = spec["notes"]
    label.update({
        "attestation": {
            "contributor": "build_real.py (AMI Meeting Corpus extraction)",
            "consent_on_file": True,
            "consent_note": (
                "AMI participants consented to recording and public release; "
                "the corpus is published under CC BY 4.0 by its creators."
            ),
            "pii_removed": True,
            "pii_note": (
                "Speakers are identified only by AMI's anonymised codes "
                "(e.g. FEE005). Audio content is the corpus's published, "
                "publicly released speech."
            ),
            "no_phi": True,
            "right_to_release_mit": False,
            "release_note": (
                "These clips remain CC BY 4.0 (they are adapted AMI material); "
                "they are NOT relicensed MIT and are not part of the MIT "
                "contribution corpus. See LICENSES.md."
            ),
        },
    })
    with open(os.path.join(tmp_scen, spec["id"] + ".json"), "w",
              encoding="utf-8") as fh:
        json.dump(label, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return {
        "id": spec["id"],
        "category": spec["category"],
        "audio": "audio/" + wav_name,
        "label": "scenarios/" + spec["id"] + ".json",
        "meeting": meeting,
        "window_global_sec": [win0, win1],
        "caller_onset_sec": onset_rel,
        "sha256": _sha256(wav_path),
        "bytes": os.path.getsize(wav_path),
    }


def _score_clip(wav_path: str, label: dict) -> dict:
    src = os.path.join(REPO, "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from hotato.core import run_single

    expect = "yield" if label["category"] == "should_yield" else "hold"
    env = run_single(
        stereo=wav_path,
        onset_sec=label["caller_onset_sec"],
        expect=expect,
        max_time_to_yield_sec=label["expected"]["max_time_to_yield_sec"],
        max_talk_over_sec=label["expected"]["max_talk_over_sec"],
    )
    ev = env["events"][0]
    env_free = run_single(stereo=wav_path, onset_sec=None, expect=expect)
    ev_free = env_free["events"][0]
    detected = ev_free["measurements"].get("caller_onset_sec")
    return {
        "passed": ev["verdict"]["passed"],
        "did_yield": ev["verdict"]["did_yield"],
        "seconds_to_yield": ev["verdict"]["seconds_to_yield"],
        "talk_over_sec": ev["verdict"]["talk_over_sec"],
        "agent_talking_at_onset": ev["measurements"]["agent_talking_at_onset"],
        "detected_onset_sec": detected,
        "onset_delta_sec": (
            None if detected is None
            else round(detected - label["caller_onset_sec"], 3)
        ),
    }


def cmd_build(check: bool = False) -> int:
    if not CLIPS:
        raise SystemExit("CLIPS is empty; curate specs from --scan output first")
    _ensure_annotations()
    tmp = tempfile.mkdtemp(prefix="hotato-real-") if check else ROOT
    tmp_scen = os.path.join(tmp, "scenarios")
    tmp_audio = os.path.join(tmp, "audio")
    os.makedirs(tmp_scen, exist_ok=True)
    os.makedirs(tmp_audio, exist_ok=True)

    verified: set = set()
    entries = []
    for spec in CLIPS:
        entries.append(build_clip(spec, tmp_scen, tmp_audio, verified))

    # score each clip with the packaged scorer; measurements go in the manifest
    for spec, entry in zip(CLIPS, entries):
        with open(os.path.join(tmp_scen, spec["id"] + ".json"),
                  encoding="utf-8") as fh:
            label = json.load(fh)
        entry["measured"] = _score_clip(
            os.path.join(tmp_audio, spec["id"] + AUDIO_SUFFIX), label)

    sources = [{
        "file": os.path.basename(ANNOT_ZIP),
        "url": ANNOT_URL,
        "sha256": ANNOT_SHA256,
        "bytes": os.path.getsize(ANNOT_ZIP),
    }]
    for key in sorted(AUDIO_SHA256):
        meeting = key.split(".")[0]
        path = os.path.join(CACHE, key)
        sources.append({
            "file": key,
            "url": AUDIO_URL_TMPL.format(meeting=meeting,
                                         ch=key.split("-")[1].split(".")[0]),
            "sha256": AUDIO_SHA256[key],
            "bytes": os.path.getsize(path) if os.path.exists(path) else None,
        })

    manifest = {
        "name": "hotato real-audio example set",
        "dataset": DATASET_NAME,
        "dataset_url": DATASET_URL,
        "license": LICENSE_ID,
        "license_url": LICENSE_URL,
        "builder": "corpus/real/build_real.py",
        "onset_provenance": "AMI manual word alignments (words/*.words.xml)",
        "sources": sources,
        "clip_count": len(entries),
        "clips": entries,
    }
    with open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    if check:
        ok = True
        pairs = [("manifest.json", "manifest.json")]
        pairs += [(e["label"], e["label"]) for e in entries]
        pairs += [(e["audio"], e["audio"]) for e in entries]
        for rel, _ in pairs:
            a, b = os.path.join(ROOT, rel), os.path.join(tmp, rel)
            if not os.path.exists(a):
                print(f"MISSING committed {rel}")
                ok = False
                continue
            with open(a, "rb") as fa, open(b, "rb") as fb:
                if fa.read() != fb.read():
                    print(f"DIFFERS {rel}")
                    ok = False
        shutil.rmtree(tmp)
        print("check: byte-identical" if ok else "check: FAILED")
        return 0 if ok else 1

    print(f"built {len(entries)} clips into {os.path.relpath(tmp)}")
    return 0


def cmd_pin_audio() -> int:
    """Print sha256 lines for every cached headset WAV (paste into AUDIO_SHA256)."""
    for name in sorted(os.listdir(CACHE)):
        if name.endswith(".wav") and ".Headset-" in name:
            print(f'    "{name}": "{_sha256(os.path.join(CACHE, name))}",')
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--scan", nargs="*", metavar="MEETING",
                   help="mine candidate events from the word annotations")
    p.add_argument("--check", action="store_true",
                   help="rebuild to a temp dir and byte-compare")
    p.add_argument("--pin-audio", action="store_true",
                   help="print sha256 pins for cached headset WAVs")
    args = p.parse_args(argv)
    if args.pin_audio:
        return cmd_pin_audio()
    if args.scan is not None:
        meetings = args.scan or ["ES2002a", "ES2002b", "ES2002c", "ES2002d"]
        return cmd_scan(meetings)
    return cmd_build(check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
