"""Contract tests for the voice pass.

Two functions with two contracts, and both matter:

- ``fix_*`` rewrites copy at the write point. It must be safe to run on human
  writing, which means it may only remove shapes that carry no claim (a filler
  opener, the third item of a decorative list, an em dash) and must never
  invent, soften, or delete a fact. An outbound email ships under the client's
  name; a wrong sentence is worse than a stiff one.
- ``lint`` never mutates. It reports what a rewrite cannot safely repair, so a
  human sees what still reads machine-written before it goes out.

The regression these pin is real: a per-lead draft that opened "Saw that ...",
said "I hope this finds you well", listed "fast, reliable, and secure", leaned
on em dashes and ended on a second question got flagged by the operator as
sounding machine-written.
"""
from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from app import ai, voice
from app.db import connect, init_db
from app.main import app

AUTH = {"Authorization": f"Bearer {os.environ['API_TOKEN']}"}
c = TestClient(app, headers=AUTH)

BAD = """Hi there,

I hope this finds you well. Saw that Legacy740 opens at 220 Third Street the first week of December, with a 24-hour gym and a nurse practitioner-led clinic sharing one building. If the network is going in as part of the buildout, we can test the circuits before the doors open.

We are a Marietta IT firm that keeps small offices running — fast, reliable, and secure.

Who is handling the network and computers for the new space?"""

GOOD = """Hi there,

A 24-hour gym and a clinic in the same building on Third Street. Two sets of computers, two sets of records, one address, and nobody on site at 2am if the wifi drops.

We do IT for small offices around Marietta, mostly the part where the wifi, the cameras and the card reader all have to keep working on a Saturday.

Who's handling that for the new space?"""


# ---------------------------------------------------------------- fix_body

def test_fix_body_keeps_every_paragraph_a_person_wrote():
    """Flattening paragraphs is the pass damaging its own input."""
    out = voice.fix_body(BAD)
    blocks = [b for b in out.split("\n\n") if b.strip()]
    assert len(blocks) == 4, out
    assert out.count("\n\n") == 3


def test_fix_body_drops_the_filler_opener_and_keeps_the_paragraph():
    out = voice.fix_body(BAD)
    assert "I hope this finds you well" not in out
    # The fact in that same paragraph survives untouched.
    assert "220 Third Street" in out
    assert "nurse practitioner-led clinic" in out
    # The sentence it was attached to is not glued onto the opener.
    assert "Hi there. Saw" not in out


def test_fix_body_keeps_a_greeting_and_its_comma():
    assert voice.fix_body("Hi there, let me know if that helps.").startswith("Hi there,")


def test_fix_body_never_deletes_a_claim():
    """Everything checkable in the source must still be checkable after."""
    out = voice.fix_body(BAD)
    for claim in ("Legacy740", "220 Third Street", "December", "24-hour gym",
                  "Marietta", "small offices"):
        assert claim in out, claim


def test_fix_body_tolerates_one_em_dash_and_strips_a_habit():
    """One dash is how a person writes. Three is a tic."""
    once = "It is one line, and it is enough — that is the whole point."
    assert "—" in voice.fix_body(once)

    habit = "We are fast — really fast — and cheap — honestly."
    assert "—" not in voice.fix_body(habit)


def test_fix_body_normalizes_en_dashes_and_shouting():
    assert "–" not in voice.fix_body("We work Mon–Fri.")
    assert "!" not in voice.fix_body("That is great news! Call us!")
    assert "?" in voice.fix_body("Does that work?")


def test_fix_body_swaps_tell_words_for_plain_ones():
    out = voice.fix_body("We utilize a robust process to leverage your data.")
    assert "utilize" not in out.lower()
    assert "leverage" not in out.lower()


def test_fix_body_is_idempotent():
    """A second pass must not keep eating the copy."""
    once = voice.fix_body(BAD)
    assert voice.fix_body(once) == once


def test_fix_body_leaves_an_empty_body_alone():
    assert voice.fix_body("") == ""
    # Whitespace with nothing to say collapses; there is no copy to damage.
    assert voice.fix_body("   \n\n  ") == ""


# ------------------------------------------------------------- fix_subject

def test_fix_subject_strips_clickbait_openers_and_colons():
    assert "quick question" not in voice.fix_subject("Quick question: your new space").lower()
    assert ":" not in voice.fix_subject("Legacy 740: the new space")
    assert voice.fix_subject("   ") == ""


def test_fix_subject_keeps_the_meaning_behind_a_cliche_label():
    # The first version blanked the whole subject, which is worse than the
    # cliché: an empty subject is a hard flag and the operator gets nothing.
    assert voice.fix_subject("Quick question: your new space") == "your new space"
    assert voice.fix_subject("Quick question about your new space") == "your new space"
    assert voice.fix_subject("Intro: sawmill it") == "sawmill it"


def test_fix_subject_keeps_a_real_name_on_the_left_of_a_colon():
    # "Legacy 740" is the subject; "the new space" is the gloss.
    assert voice.fix_subject("Legacy 740: the new space") == "Legacy 740"


def test_fix_subject_leaves_a_good_one_alone():
    assert voice.fix_subject("legacy 740 opening") == "legacy 740 opening"


# --------------------------------------------------------------------- lint

def test_lint_flags_the_draft_the_operator_complained_about():
    res = voice.lint("quick question about your new space", BAD)
    codes = [f["code"] for f in res["flags"]]
    assert res["ok"] is False
    for code in ("throat_clearing", "em_dash", "tricolon"):
        assert code in codes, codes


def test_lint_reports_one_throat_clearing_flag_per_phrase():
    """Substring phrases used to double-report and bury the signal."""
    res = voice.lint("", "I hope this finds you well. Anything else?")
    hits = [f for f in res["flags"] if f["code"] == "throat_clearing"]
    assert len(hits) == 1, hits


def test_lint_measures_rhythm_rather_than_length():
    flat = "We are a company that does work in the area. " \
           "We have been doing this work for a very long time now. " \
           "We would like to talk with you about the work we do."
    res = voice.lint("", flat)
    assert any(f["code"] == "flat_rhythm" for f in res["flags"]), res["flags"]


def test_lint_counts_questions_because_two_asks_is_no_ask():
    two = "Would you have ten minutes? Who handles this today?"
    res = voice.lint("", two)
    assert any(f["code"] == "two_asks" for f in res["flags"])

    none = "Nothing to answer here. Just a note."
    assert any(f["code"] == "no_ask" for f in voice.lint("", none)["flags"])


def test_lint_reports_an_overlong_email():
    """50-90 words is the target; past 140 it stops being a first touch."""
    long_body = " ".join(["This sentence exists only to add words to the pile."] * 18) + " Ready?"
    flags = voice.lint("", long_body)["flags"]
    hit = [f for f in flags if f["code"] == "length"]
    assert hit and hit[0]["hard"] is True, flags


def test_lint_treats_a_slightly_long_email_as_advice_not_a_failure():
    body = " ".join(["This sentence exists only to add words to the pile."] * 13) + " Ready?"
    hit = [f for f in voice.lint("", body)["flags"] if f["code"] == "length"]
    assert hit and hit[0]["hard"] is False


def test_lint_accepts_copy_written_to_the_rules():
    """The pass must not be so eager that clean human copy fails."""
    res = voice.lint("legacy 740 opening", GOOD)
    assert res["ok"] is True, res["flags"]
    assert voice.summarise(res) == "clean"


def test_lint_does_not_mutate_its_input():
    before = (BAD, "quick question about your new space")
    voice.lint(before[1], before[0])
    assert before == (BAD, "quick question about your new space")


# ---------------------------------------------------------------- summaries

def test_summarise_is_one_line_for_a_log():
    clean = voice.summarise(voice.lint("legacy 740 opening", GOOD))
    assert clean == "clean"
    dirty = voice.summarise(voice.lint("", BAD))
    assert dirty and "\n" not in dirty


# -------------------------------------------------------------- batch view

def test_batch_lint_catches_five_drafts_that_open_the_same_way():
    drafts = [{"subject": f"opening {i}", "body": f"Saw that company {i} is opening.\n\nReady?"}
              for i in range(4)]
    out = voice.batch_lint(drafts)
    assert all(any(f["code"] == "same_opening" for f in r["flags"]) for r in out)


def test_batch_lint_leaves_distinct_openings_alone():
    drafts = [
        {"subject": "a", "body": "Two businesses, one building on Third Street.\n\nWho handles IT?"},
        {"subject": "b", "body": "Your careers page lists two operators this month.\n\nStill hiring?"},
    ]
    out = voice.batch_lint(drafts)
    assert not any(f["code"] == "same_opening" for r in out for f in r["flags"])


# --------------------------------------------------- write-point integration

@pytest.fixture(scope="module", autouse=True)
def _schema() -> None:
    init_db()


SLUG = "voice-co"
PROFILE = {
    "business_name": "Sawmill IT",
    "what_they_do": "Managed IT for small offices.",
    "industry": "IT",
    "customers": "Small offices",
    "service_area": "Marietta, OH",
    "emails": ["hello@sawmillit.com"],
    "url": "https://sawmillit.com",
    "domain": "sawmillit.com",
    "leads": [{
        "name": "Legacy 740",
        "domain": "legacy740fit.com",
        "why_now": "Ribbon cutting on the Chamber calendar for a December opening.",
        "signal_kind": "expansion",
        "signal_detail": "Chamber listing: ribbon cutting, 220 Third Street, first week of December.",
        "signal_source": "https://business.mariettachamber.com/events/details/legacy-740",
        "fit": 88,
        "email": {"subject": "Quick question: your new space", "body": BAD},
    }],
}


WS_ID = "voiceworkspace0000000000000001"


@pytest.fixture(scope="module", autouse=True)
def _ws_rows():
    """A workspace to attribute the lead to, torn down children-first."""
    conn = connect()
    try:
        if not conn.execute("SELECT id FROM workspaces WHERE slug = ?", (SLUG,)).fetchone():
            conn.execute(
                """INSERT INTO workspaces (id, slug, name, physical_address, daily_send_cap, created_at)
                   VALUES (?,?,?,?,?,datetime('now'))""",
                (WS_ID, SLUG, "Voice Co", "Marietta, OH", 10),
            )
            conn.commit()
    finally:
        conn.close()
    yield WS_ID
    conn = connect()
    try:
        # Delete the whole identity's rows. Constraint order does not matter
        # here because everything named is going, and doing it in dependency
        # order across six child tables makes the fixture lie about what the
        # product owns. The app's own write path is what proves FK order.
        conn.execute("PRAGMA foreign_keys = OFF")
        for table, col in (("contacts", "business_id"), ("list_members", "business_id"),
                           ("enrollments", "business_id"), ("messages", "business_id"),
                           ("scores", "business_id"), ("tasks", "business_id")):
            conn.execute(
                f"DELETE FROM {table} WHERE {col} IN "
                "(SELECT id FROM businesses WHERE domain IN ('legacy740fit.com', 'sawmillit.com'))")
        conn.execute("DELETE FROM client_leads WHERE workspace_id = ?", (WS_ID,))
        conn.execute("DELETE FROM research_leads WHERE workspace_id = ?", (WS_ID,))
        conn.execute("DELETE FROM research_queue WHERE workspace_id = ?", (WS_ID,))
        conn.execute("DELETE FROM workspace_clients WHERE workspace_id = ?", (WS_ID,))
        conn.execute("DELETE FROM workspace_businesses WHERE workspace_id = ?", (WS_ID,))
        conn.execute("DELETE FROM businesses WHERE domain IN ('legacy740fit.com', 'sawmillit.com')")
        conn.commit()
    finally:
        conn.close()


def test_the_stored_draft_is_cleaned_at_the_write_point(monkeypatch):
    """Whatever wrote the copy, the row that ships is run through the pass."""
    monkeypatch.setattr("app.leadguard.check_lead",
                        lambda lead, locality="": {"domain": lead.get("domain", ""),
                                                   "note": "fixture: domain printed by test"})
    ws = WS_ID
    item = c.post("/api/research/queue", json={"workspace_id": ws, "url": "legacy740fit.com"}).json()["item"]
    c.post("/api/research/queue/claim", json={"workspace_id": ws, "limit": 5})
    r = c.post(f"/api/research/queue/{item['id']}/complete",
               json={"status": "done", "profile": dict(PROFILE)})
    assert r.status_code == 200, r.text

    conn = connect()
    try:
        row = conn.execute(
            "SELECT email_subject, email_body, voice_note FROM research_leads WHERE workspace_id = ?",
            (ws,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert "I hope this finds you well" not in row["email_body"]
    assert "quick question" not in (row["email_subject"] or "").lower()
    # The body keeps its shape and its facts.
    assert "\n\n" in row["email_body"]
    assert "220 Third Street" in row["email_body"]
    # And the row carries the report of what is still not clean.
    assert row["voice_note"] and row["voice_note"] != "clean"

    # A note nobody can read is a broken feature: the lead list must carry it
    # so the operator sees the verdict next to the draft before sending.
    items = c.get(f"/api/leads?workspace_id={ws}&limit=20").json()["items"]
    lead = [i for i in items if i.get("name") == "Legacy 740"]
    assert lead, [i.get("name") for i in items]
    assert "voice_note" in lead[0]
    assert lead[0]["voice_note"] == row["voice_note"]


# ------------------------------------------------------- flat-rhythm repair

FLAT_JOINED = (
    "Legacy 740 opens in December, and the gym and the clinic both open that same "
    "week too. We keep small offices running when the wifi drops or the cameras fail "
    "late at night. Does your team already have someone handling the network for the "
    "new location?"
)


def test_flat_rhythm_is_repaired_by_breaking_one_clause_join():
    """The cheapest real fix: a 20-word sentence with two clauses becomes two."""
    out = voice.fix_body(FLAT_JOINED)
    assert out != FLAT_JOINED.strip()
    after = voice.lint("Third Street in December", out)
    assert min(after["stats"]["sentence_lengths"]) < min(
        voice.lint("Third Street in December", FLAT_JOINED)["stats"]["sentence_lengths"]
    )


def test_the_rhythm_split_loses_no_word_and_keeps_the_ask():
    """A split may move a comma; it may never drop a fact or eat the question."""
    out = voice.fix_body(FLAT_JOINED)
    assert sorted(voice._WORD.findall(FLAT_JOINED.lower())) == sorted(
        voice._WORD.findall(out.lower())
    )
    assert voice.sentences(out)[-1] == voice.sentences(FLAT_JOINED)[-1]
    assert out.count("?") == 1


def test_a_body_with_no_clause_join_is_left_alone():
    """No break available means no break taken: never fake a beat by fragmenting."""
    no_join = (
        "Legacy 740 opens at 220 Third Street the first week of December with a gym. "
        "Both the gym and the nurse practitioner-led clinic share that single address. "
        "We keep small offices running when the wifi drops late at night."
    )
    assert voice.fix_body(no_join) == no_join.strip()


def test_a_body_that_already_has_a_short_beat_is_not_touched():
    human = (
        "Legacy 740 opens in December. It has a gym and a clinic on one site, which is "
        "unusual for a building that size. We keep small offices running when things "
        "break. Want the short version?"
    )
    assert voice.fix_body(human) == human.strip()
    assert not [f for f in voice.lint("Third Street", human)["flags"] if f["hard"]]


def test_rhythm_repair_is_idempotent():
    once = voice.fix_body(FLAT_JOINED)
    assert voice.fix_body(once) == once


def test_flat_rhythm_ignores_low_variance_when_a_short_sentence_exists():
    """9/16/13 is ordinary human copy. The tell is the missing beat, not variance."""
    body = (
        "Legacy 740 opens in December. It has a gym and a clinic sharing one address, "
        "which is unusual for a building that size in this town. Want a look?"
    )
    assert not [f for f in voice.lint("Third Street", body)["flags"] if f["hard"]]


# ------------------------------------------------- lint-triggered re-ask

def test_revise_for_voice_does_nothing_without_a_hard_flag(monkeypatch):
    """Soft advice is not worth a second model call."""
    def boom(*a, **k):
        raise AssertionError("must not call the model")
    monkeypatch.setattr(ai, "_llm", boom)
    assert ai._revise_for_voice("some body", {"flags": [{"code": "length", "detail": "x", "hard": False}]}) == ""


def test_draft_email_reasks_once_when_the_lint_rejects_the_draft(monkeypatch):
    flat = (
        "Legacy 740 opens at 220 Third Street the first week of December with a gym. "
        "Both the gym and the nurse practitioner clinic share that single address. "
        "We keep small offices running when the wifi drops late at night."
    )
    better = (
        "Legacy 740 opens on Third Street in December. It puts a gym and a clinic in "
        "one building, which is why the network is worth planning now. We keep small "
        "offices running when wifi drops. Want a look before opening?"
    )
    calls = []

    def fake_llm(prompt, max_tokens=400, temperature=0.4):
        calls.append(prompt)
        return json.dumps({"subject": "Third Street in December", "body": flat}) if len(calls) == 1 else better

    monkeypatch.setattr(ai, "_llm", fake_llm)
    d = ai.draft_email(
        {"offer": "We handle IT for small offices"},
        {"name": "Legacy 740", "city": "Marietta", "state": "OH"},
        {"full_name": "Dana Reed"},
        "24-hour gym and clinic at 220 Third Street",
        signature="Ryan",
    )
    assert len(calls) == 2
    assert "Want a look before opening?" in d["body"]
    assert d["voice_ok"] is True


def test_draft_email_keeps_the_first_draft_when_the_reask_is_no_better(monkeypatch):
    flat = (
        "Legacy 740 opens at 220 Third Street the first week of December with a gym. "
        "Both the gym and the nurse practitioner clinic share that single address. "
        "We keep small offices running when the wifi drops late at night."
    )
    calls = []

    def fake_llm(prompt, max_tokens=400, temperature=0.4):
        calls.append(prompt)
        if len(calls) == 1:
            return json.dumps({"subject": "Third Street in December", "body": flat})
        return flat  # same defect, no improvement

    monkeypatch.setattr(ai, "_llm", fake_llm)
    d = ai.draft_email(
        {"offer": "We handle IT for small offices"},
        {"name": "Legacy 740", "city": "Marietta", "state": "OH"},
        {"full_name": "Dana Reed"},
        "24-hour gym and clinic at 220 Third Street",
        signature="Ryan",
    )
    assert len(calls) == 2, "one re-ask, never a loop"
    assert d["voice_ok"] is False
    assert d["voice_note"], "a draft that still reads machine-written must say why"


def test_merge_template_copy_is_never_rewritten():
    """A template with {{fields}} is a form. Splitting or trimming it breaks the
    fill-in that sends it, so both fixers must hand it back untouched."""
    subject = "Quick question about {{business_name}}"
    body = (
        "We handle the network at {{business_name}}, and we keep the wifi and cameras "
        "running after hours. Our crews work around your schedule and never block the "
        "front desk during business hours. Does that way of working fit the schedule "
        "your office keeps in {{city}}"
    )
    assert voice.fix_subject(subject) == subject
    assert voice.fix_body(body) == body.strip()


def test_a_split_that_only_reaches_nine_words_is_not_taken():
    """Nine words is not a beat. A half-fix that still reads flat just moves the
    warning without answering it, so the draft keeps its note for the human."""
    body = (
        "Legacy 740 opens at 220 Third Street in December, and the gym and the clinic "
        "both open that week too. We keep small offices running when the wifi drops or "
        "the cameras fail late at night. Does your team already have someone handling "
        "the network for the new location?"
    )
    assert voice.fix_body(body) == body.strip()
    hard = [f["code"] for f in voice.lint("Third Street in December", body)["flags"] if f["hard"]]
    assert "flat_rhythm" in hard
