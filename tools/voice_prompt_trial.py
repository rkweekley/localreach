"""Send the rewritten first-touch rules to a local model on real lead facts.

The point is to test the *prompt*, not the model: if the rules work, the draft
comes back with no hard voice flags and the linter's shape checks (sentence
variation, one question, no tricolon) hold. Output goes to stdout as JSON so the
linter can judge it in the same process.
"""
import json
import urllib.request

RULES = """You write a first-touch cold email on behalf of a small business, to one
prospect. Return ONLY JSON: {"subject": "...", "body": "..."}

Who is writing: Cyberal Solutions, an IT firm in Marietta, Ohio. They do IT for
small offices: networks, wifi, cameras, card readers.

Who it is going to: Legacy 740, opening a new location.

Facts read from a source (use only these, invent nothing):
- Legacy 740 opens at 220 Third Street the first week of December.
- The building holds a 24-hour gym and a nurse practitioner-led clinic.
- Both share the one address.

Rules:
- Shape it as four beats with these lengths, in order: a 3-7 word opener
  naming the signal. A 15-25 word sentence with one concrete detail. An 8-12
  word sentence on what the writer does. A 10-18 word question.
- "Vary your sentence length" is too vague. Make one sentence shorter than
  eight words. Every sentence running 13-20 words is the loudest sign a machine
  wrote it.
- 50-90 words total, at most two short paragraphs. Contractions. Plain verbs.
- One question in the whole email. Do not restate the email at the end; finish
  on the question, then sign as Cyberal Solutions.
- Never a three-item list. Never compliment them.
- Banned: em dashes, exclamation marks, "I hope this finds you well", "I wanted
  to reach out", "just checking in", "quick question", "let me know your
  thoughts", "streamline", "leverage", "robust", "seamless", "elevate",
  "unlock", "empower", "comprehensive", "not just X, but Y".
- Subject: 3-6 words about their signal, no colon, no clickbait.

Bad example: "Hi there, I hope this finds you well. Saw that Legacy740 opens at
220 Third Street the first week of December, with a 24-hour gym and a nurse
practitioner-led clinic sharing one building. If the network is going in as part
of the buildout, we can test the circuits before the doors open. We are a
Marietta IT firm that keeps small offices running, fast, reliable, and secure.
Who is handling the network and computers for the new space?"

Good example: "Hi there, A 24-hour gym and a clinic in the same building on
Third Street. Two sets of computers, two sets of records, one address, and
nobody on site at 2am if the wifi drops. We do IT for small offices around
Marietta, mostly the part where the wifi, the cameras and the card reader all
have to keep working on a Saturday. Who's handling that for the new space?"
"""


def gen(model: str, seed: int) -> dict:
    req = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=json.dumps({
            "model": model,
            "prompt": RULES,
            "stream": False,
            # Thinking is off on purpose: a local thinking model spends the whole
            # num_predict budget reasoning and returns an empty body. This trial
            # judges the prompt, not the reasoning.
            "think": False,
            "format": "json",
            "options": {"temperature": 0.8, "seed": seed, "num_ctx": 8192, "num_predict": 1200},
        }).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read())


if __name__ == "__main__":
    import sys
    model = sys.argv[1] if len(sys.argv) > 1 else "qwen3.5:9b-64k"
    out = gen(model, 7)
    print(json.dumps({"model": model, "raw": out.get("response", "")}, indent=2))
