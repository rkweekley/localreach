"""Voice hygiene for outbound copy.

Models (and template fallbacks) converge on a narrow set of shapes: a medium
sentence length held for the whole email, three-item lists, "not just X, but Y",
em dashes used as pauses, hedged openers that clear their throat before saying
anything, and a small dictionary of words nobody says out loud. None of those is
wrong once. Repeated, they read as machine-written, which is the one thing a
first-touch email cannot afford.

Two jobs here, deliberately separated:

  fix(subject, body)   meaning-preserving rewrites only. Safe to run on copy a
                       human wrote, so it runs on every draft at the write point.
  lint(subject, body)  reports what a rewrite cannot safely repair, so a human
                       (or a re-draft) can act. Never mutates.

`fix` is conservative on purpose: a voice pass that invents or deletes a claim
is worse than the tic it removes, because the email goes out under the client's
name and only facts read from a source are allowed in it.
"""

from __future__ import annotations

import re
import statistics

# ---------------------------------------------------------------- constants

# Phrases that only ever appear in copy written to fill space. Deleted when they
# open a sentence or a paragraph; flagged anywhere else.
THROAT_CLEARING = [
    "i hope this email finds you well",
    "i hope this finds you well",
    "hope this finds you well",
    "hope you're doing well",
    "hope you are doing well",
    "i hope you are well",
    "it is important to note that",
    "it is worth noting that",
    "it's worth noting that",
    "it should be noted that",
    "in today's fast-paced",
    "in today's rapidly",
    "i wanted to reach out",
    "i am reaching out",
    "i'm reaching out",
    "just wanted to reach out",
    "i wanted to touch base",
    "touching base",
    "circling back",
    "i came across your",
    "i couldn't help but notice",
]

# Single words swapped for the word a person would actually say.
WORD_SWAPS = {
    "utilize": "use",
    "utilizes": "uses",
    "utilizing": "using",
    "utilization": "use",
    "leverage": "use",
    "leverages": "uses",
    "leveraging": "using",
    "facilitate": "help",
    "facilitates": "helps",
    "endeavor": "try",
    "endeavour": "try",
    "commence": "start",
    "ascertain": "find out",
    "in order to": "to",
    "at this point in time": "now",
    "at this time": "now",
    "due to the fact that": "because",
    "for the purpose of": "to",
    "in the event that": "if",
    "a myriad of": "many",
    "plethora of": "plenty of",
}

# Marketing filler that reads as generated. Flagged, never auto-deleted, because
# removing one can leave a sentence with no subject.
TELL_WORDS = [
    "delve", "robust", "seamless", "seamlessly", "streamline", "streamlined",
    "elevate", "elevated", "unlock", "unlocking", "empower", "empowering",
    "cutting-edge", "state-of-the-art", "best-in-class", "world-class",
    "game-changer", "revolutionary", "innovative", "synergy", "holistic",
    "comprehensive", "bespoke", "curated", "tapestry", "testament",
    "underscore", "underscores", "foster", "fosters", "navigate the",
    "landscape", "realm", "myriad", "paradigm", "transformative",
    "impactful", "actionable", "robustness", "future-proof",
]

# Email clichés that spend the reader's attention without earning it.
EMAIL_CLICHES = [
    "quick question",
    "quick call",
    "touch base",
    "circle back",
    "hop on a call",
    "jump on a quick",
    "worth a quick",
    "let me know your thoughts",
    "let me know if this is of interest",
    "no strings attached",
    "at your earliest convenience",
    "please don't hesitate",
    "don't hesitate to",
    "looking forward to hearing",
    "i look forward to hearing",
]

HEDGES = [
    "potentially", "possibly", "perhaps", "may be able to", "might be able to",
    "could potentially", "in some cases", "generally speaking", "arguably",
]

EM_DASH = "\u2014"
EN_DASH = "\u2013"

# Bare labels that sit on the left of a subject colon without being the
# subject: "Intro: sawmill it" is about the sawmill, not about the intro.
SUBJECT_LABELS = {
    "intro", "introduction", "question", "quick question", "quick q", "fyi",
    "re", "hello", "hi", "hey", "following up", "checking in", "note",
}

# Sentence splitter that keeps the terminator out of the fragment.
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"[A-Za-z0-9'\-]+")

# Where a long sentence may be broken in two: at a clause join, never at a
# comma that merely separates list items.
_RHYTHM_SPLITS = (
    ", and ", ", but ", ", so ", ", while ", ", which ", "; ",
    ", mostly ", ", especially ", ", usually ", ", often ", ", ",
)


# ------------------------------------------------------------------ helpers

def sentences(body: str) -> list[str]:
    """Split into sentences, dropping empties. Greeting/sign-off included."""
    return [s.strip() for s in _SENT_SPLIT.split(body or "") if s.strip()]


def word_count(text: str) -> int:
    return len(_WORD.findall(text or ""))


def _lower(text: str) -> str:
    return (text or "").lower()


# ---------------------------------------------------------------- rewriting

def fix_subject(subject: str) -> str:
    """Trim a subject to something a colleague would type.

    Never blanks a subject that still carries meaning: "Quick question: your
    new space" becomes "your new space", not "". An empty result is reserved
    for a subject that was nothing but the cliché.
    """
    s = (subject or "").replace(EM_DASH, ",").replace(EN_DASH, "-").strip()
    s = re.sub(r"\s+", " ", s)

    # A merge template is a form, not copy. Rewriting around {{business_name}}
    # silently breaks every send that fills it in, so leave it exactly as typed.
    if "{{" in s:
        return s

    # A colon makes it read like a headline. Drop a bare label on the left
    # ("Quick question: X", "Intro: X") and keep the side that names the thing.
    if ":" in s:
        left, right = (p.strip() for p in s.split(":", 1))
        if right and (left.lower() in SUBJECT_LABELS or left.lower() in EMAIL_CLICHES):
            s = right
        elif left:
            s = left

    # A cliché opener that survived the colon rule: "Quick question about your
    # space". Drop the opener and the preposition hanging off it.
    m = re.match(r"^(?:quick question|quick q|question|fyi|re|following up)\b[\s,:;\-\u2013]*", s, re.I)
    if m and s[m.end():].strip():
        rest = re.sub(r"^(?:about|on|regarding|re|for)\s+", "", s[m.end():].strip(), flags=re.I)
        if rest:
            s = rest

    s = s.rstrip(".!,")
    # An empty subject is honest: the copy had nothing but the label.
    if not s or _lower(s) in SUBJECT_LABELS or _lower(s) in EMAIL_CLICHES:
        return ""
    return s


def _split_once(text: str, target_min: int) -> str:
    """Split the longest splittable sentence in two, without losing a word.

    Only accepts a break that leaves a shorter sentence than the draft already
    has, and only at a real clause join, so the result is two sentences rather
    than a clause and a fragment. Returns the text unchanged when no break helps.
    """
    best = None
    for m in re.finditer(r"[^.?!]+[.?!]", text):
        sent = m.group(0)
        n = word_count(sent)
        # A question is the ask: splitting it would break the one thing the
        # email exists to do. Short sentences have nothing to give away.
        if n < 11 or "?" in sent:
            continue
        for marker in _RHYTHM_SPLITS:
            i = sent.find(marker)
            if i < 0:
                continue
            head, tail = sent[:i].strip(), sent[i + len(marker):].strip()
            if word_count(head) < 4 or word_count(tail) < 4:
                continue
            # The joining word stays and counts toward the new clause.
            join_words = 0 if marker.strip(" ,;") == "" else 1
            if min(word_count(head), word_count(tail) + join_words) >= target_min:
                continue
            if best is None or n > best[0]:
                best = (n, m.start(), m.end(), head, tail, marker)
            break
    if best is None:
        return text
    _, start, end, head, tail, marker = best
    conj = marker.strip(" ,;")
    if conj:
        # "And the building holds..." — the joining word leads the new clause,
        # so the tail keeps its own lower-case opener.
        lead = f"{conj[0].upper()}{conj[1:]} "
    else:
        tail = tail[0].upper() + tail[1:]
        lead = ""
    return text[:start] + f"{head}. {lead}{tail}" + text[end:]


def fix_body(body: str) -> str:
    """Meaning-preserving rewrite. Never adds a fact, never drops a claim.

    Returns the body with machine tells removed. Facts, names, numbers and the
    ask are left exactly as written.
    """
    text = (body or "").replace("\r\n", "\n")

    # A merge template is a form, not copy: a split around {{city}} breaks the
    # fill-in behind it, exactly like rewriting the subject line would.
    if "{{" in text:
        return text.strip()

    # One em dash in a short email is how people write. Two or more is the tell,
    # so only strip when the draft leans on them.
    if text.count(EM_DASH) > 1:
        text = text.replace(f" {EM_DASH} ", ", ").replace(EM_DASH, ", ")
    text = text.replace(EN_DASH, "-")

    # Exclamation marks read as broadcast copy in a first touch.
    text = re.sub(r"!+", ".", text)

    for phrase, repl in WORD_SWAPS.items():
        text = re.sub(rf"\b{re.escape(phrase)}\b", repl, text, flags=re.IGNORECASE)

    # Paragraph by paragraph, so the shape a person wrote survives: delete only
    # sentences that exist to warm up, and a greeting is not one of them.
    paras = []
    for para in text.split("\n"):
        if not para.strip():
            continue
        kept = [
            sent
            for sent in sentences(para)
            if not any(
                _lower(sent).strip(" ,.").startswith(p)
                or _lower(sent).strip(" ,.") == p
                for p in THROAT_CLEARING
            )
        ]
        if kept:
            paras.append(" ".join(kept))

    text = "\n\n".join(paras)
    text = re.sub(r"[ \t]+", " ", text)

    # Uniform sentence length is the loudest machine tell and the one a prompt
    # cannot reliably prevent. Repair only a draft with no short beat at all,
    # at most twice, and only where a clause join lets the split keep every
    # word: chopping every long sentence reads worse, not more human.
    for _ in range(2):
        lens = [word_count(s) for s in sentences(text)]
        if len(lens) < 3 or statistics.pstdev(lens) >= 4.0 or min(lens) < 9:
            break
        wider = _split_once(text, min(lens))
        if wider == text:
            break
        text = wider

    return text.strip()


# ------------------------------------------------------------------ linting

def _tricolons(body: str) -> list[str]:
    """Find 'a, b, and c' lists whose items are short and parallel.

    Three-item lists are legitimate; the tell is a whole email built from them.
    Only matches list items of one to four words so prose commas are ignored.
    """
    hits = []
    pattern = re.compile(
        r"\b([\w'\-]+(?:\s+[\w'\-]+){0,3}),\s+([\w'\-]+(?:\s+[\w'\-]+){0,3}),\s+and\s+([\w'\-]+(?:\s+[\w'\-]+){0,3})"
    )
    for m in pattern.finditer(body or ""):
        items = [m.group(1), m.group(2), m.group(3)]
        lens = [len(i.split()) for i in items]
        # Parallel length is the giveaway; genuinely varied lists are not.
        if max(lens) - min(lens) <= 1:
            hits.append(m.group(0))
    return hits


def lint(subject: str, body: str) -> dict:
    """Report what makes this copy read as machine-written.

    Returns {"ok": bool, "flags": [{"code","detail"}], "stats": {...}}.
    `ok` is False when any flag of severity "hard" fires. Soft flags are advice.
    """
    body = body or ""
    low = _lower(body)
    flags: list[dict] = []

    def add(code: str, detail: str, hard: bool = True) -> None:
        flags.append({"code": code, "detail": detail, "hard": hard})

    sents = sentences(body)
    lens = [word_count(s) for s in sents]
    words = word_count(body)

    # One em dash in a short email is how a person writes; two is the pattern
    # that reads as generated. Matches fix_body, which only strips the many.
    dashes = body.count(EM_DASH)
    if dashes:
        add(
            "em_dash",
            f"{dashes} em dash(es); {'use a comma or a period' if dashes > 1 else 'fine once, not twice'}",
            hard=dashes > 1,
        )

    # Longest phrase first, and skip any match sitting inside one already
    # reported: "i hope this finds you well" also contains "hope this finds you
    # well", and reporting both for one sentence buries the real signal.
    matched: list[tuple[int, int]] = []
    for phrase in sorted(THROAT_CLEARING, key=len, reverse=True):
        for m in re.finditer(re.escape(phrase), low):
            if any(m.start() < end and m.end() > start for start, end in matched):
                continue
            matched.append((m.start(), m.end()))
            add("throat_clearing", f'"{phrase}"')

    for phrase in EMAIL_CLICHES:
        if phrase in low:
            add("cliche", f'"{phrase}"')

    tell = sorted({w for w in TELL_WORDS if re.search(rf"\b{re.escape(w)}\b", low)})
    if tell:
        add("tell_words", ", ".join(tell))

    hedge = sorted({w for w in HEDGES if re.search(rf"\b{re.escape(w)}\b", low)})
    if hedge:
        add("hedging", ", ".join(hedge), hard=False)

    if re.search(r"\b(not just|isn'?t just|more than just)\b.{0,60}\bbut\b", low):
        add("not_x_but_y", '"not just X, but Y" construction')

    tri = _tricolons(body)
    if len(tri) >= 2:
        add("tricolon", f"{len(tri)} parallel three-item lists: " + "; ".join(tri[:2]))
    elif tri:
        add("tricolon", f"parallel three-item list: {tri[0]}", hard=False)

    if re.search(r"[\u2018\u2019\u201c\u201d]", body) and False:  # reserved
        pass

    # A person writes at least one clipped sentence; a model writes four of the
    # same length. Flag the absence of any beat (every sentence nine words or
    # longer), not a low variance on its own: three tidy 9/16/13 sentences are
    # ordinary human copy and flagging them trains the operator to ignore this.
    if words and len(sents) >= 3 and statistics.pstdev(lens) < 4.0 and min(lens) >= 9:
        add(
            "flat_rhythm",
            f"sentence lengths {lens} have no short beat; one sentence under nine words "
            "next to a long one is what reads like a person",
        )

    if not 45 <= words <= 105:
        add("length", f"{words} words; first touch should be 50-90", hard=words > 140)

    questions = body.count("?")
    if questions == 0:
        add("no_ask", "no question; one easy question is what gets a reply")
    elif questions > 1:
        add("two_asks", f"{questions} questions; two asks is no ask")

    if re.search(r"\b(dear|hello)\b", low) and not re.search(r"\b(hi|hey)\b", low):
        add("formal_greeting", '"Dear/Hello" where "Hi" would do', hard=False)

    subj = (subject or "").strip()
    subj_words = word_count(subj)
    if not subj:
        add("no_subject", "empty subject")
    else:
        if subj_words > 7:
            add("subject_long", f'subject is {subj_words} words; keep it to 5-7')
        if ":" in subj:
            add("subject_colon", "colons read like a headline")
        if subj.lower() in EMAIL_CLICHES or subj.lower() in {"quick question", "hello", "intro"}:
            add("subject_cliche", f'"{subj}"')
        if subj.endswith("!"):
            add("subject_exclaim", "exclamation mark in subject")

    hard = [f for f in flags if f["hard"]]
    return {
        "ok": not hard,
        "flags": flags,
        "stats": {
            "words": words,
            "sentences": len(sents),
            "sentence_lengths": lens,
            "burstiness": round(statistics.pstdev(lens), 2) if len(lens) > 1 else 0.0,
            "questions": questions,
        },
    }


def batch_lint(drafts: list[dict]) -> list[dict]:
    """Flag drafts in one batch that open the same way.

    Five emails to five businesses that all start "Saw that ..." are the loudest
    possible tell, and no single-draft check can see it.
    """
    openings: dict[str, list[int]] = {}
    for i, d in enumerate(drafts):
        first = sentences(d.get("body") or "")
        key = " ".join(_WORD.findall(_lower(first[0] if first else ""))[:3])
        if key:
            openings.setdefault(key, []).append(i)

    out = []
    for i, d in enumerate(drafts):
        notes = lint(d.get("subject", ""), d.get("body", ""))
        first = sentences(d.get("body") or "")
        key = " ".join(_WORD.findall(_lower(first[0] if first else ""))[:3])
        if key and len(openings.get(key, [])) > 1:
            notes["flags"].append({
                "code": "same_opening",
                "detail": f"{len(openings[key])} drafts open with \"{key}\"",
                "hard": True,
            })
            notes["ok"] = False
        out.append(notes)
    return out


def summarise(result: dict) -> str:
    """One line for a log or an evidence note."""
    if result["ok"] and not result["flags"]:
        return "clean"
    bits = [f"{f['code']}: {f['detail']}" for f in result["flags"]]
    return "; ".join(bits)
