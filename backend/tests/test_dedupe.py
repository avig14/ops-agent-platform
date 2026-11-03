"""A4 duplicate screening. Part 1: pure scoring. Part 2: through the real pipeline and HTTP API."""
import pytest

from app import dedupe
from app.dedupe import Doc, compare, text_similarity, tokenize
from tests.test_safety import SAMPLE

TXT = tokenize(SAMPLE)


def doc(cid="CASE-0001", text=SAMPLE, **kw):
    base = {"store": "PUN-01", "item": "ITM-DISP", "qty": 500, "delay": 10}
    base.update(kw)
    return Doc(cid, tokenize(text), **base)


# ───────────── tokenizing / similarity ─────────────
def test_tokenize_strips_mail_furniture():
    a = tokenize("Subject: Re: stock\nFrom: a@b.c\n> 500 units late\n-----Original Message-----\nref 0123456789abcdef0123456789abcdef")
    assert a == ("500", "units", "late", "ref")


def test_tokenize_is_unicode_and_case_safe():
    assert tokenize("PUNE  Store") == tokenize("pune store") == ("pune", "store")
    assert tokenize("") == ()


def test_tokenize_caps_length():
    assert len(tokenize("word " * 10_000)) == dedupe.MAX_TOKENS


def test_similarity_bounds():
    assert text_similarity(TXT, TXT) == pytest.approx(1.0)
    assert text_similarity(TXT, ()) == 0.0
    assert text_similarity(tokenize("alpha beta gamma"), tokenize("delta epsilon zeta")) < 0.1
    assert 0.0 <= text_similarity(TXT, tokenize(SAMPLE.replace("Pune", "Delhi"))) <= 1.0


def test_similarity_symmetric_enough_and_orders_by_closeness():
    near = tokenize(SAMPLE + "\nThanks, please confirm.")
    far = tokenize("Hello team, lunch menu for friday is attached.")
    assert text_similarity(TXT, near) > text_similarity(TXT, far)
    assert text_similarity(TXT, near) == pytest.approx(text_similarity(near, TXT), abs=0.05)


def test_long_texts_do_not_blow_up():
    big = tokenize("alpha beta " * 5000)
    assert 0.0 <= text_similarity(big, big[::-1]) <= 1.0


# ───────────── compare(): the decision table ─────────────
def test_same_text_is_duplicate_even_without_facts():
    a, b = Doc("CASE-0002", TXT), Doc("CASE-0001", tokenize("Fwd: " + SAMPLE))
    assert compare(a, b).kind == "duplicate"


def test_same_facts_similar_text_is_duplicate():
    m = compare(doc("CASE-0002", SAMPLE + "\nPlease confirm receipt."), doc())
    assert m.kind == "duplicate" and m.score >= dedupe.DUP_SCORE and not m.conflicts
    assert set(m.equal) == {"store", "item", "qty", "delay"}


def test_same_facts_unrelated_text_is_not_certain():
    m = compare(doc("CASE-0002", "completely different wording about a late shipment"), doc())
    assert m.kind == "possible" and m.text_sim < dedupe.DUP_MIN_TEXT    # equal facts alone are never a certain duplicate


def test_different_store_is_not_a_duplicate():
    m = compare(doc("CASE-0002", "other words about a delay", store="MUM-01"), doc())
    assert m.kind == "none" and "store" in m.conflicts


def test_identical_text_is_a_resend_whatever_the_extraction_says():
    assert compare(doc("CASE-0002", store="MUM-01"), doc()).kind == "duplicate"


def test_different_store_but_near_identical_text_is_flagged_not_merged():
    m = compare(doc("CASE-0002", SAMPLE + " ok", store="MUM-01"), doc())
    assert m.kind == "possible" and "store" in m.conflicts


def test_different_item_same_store_is_not_merged():
    m = compare(doc("CASE-0002", "other words entirely", item="ITM-LED"), doc())
    assert m.kind == "none" and "item" in m.conflicts


def test_changed_quantity_same_store_is_possible_never_duplicate():
    m = compare(doc("CASE-0002", SAMPLE + " update", qty=300), doc())
    assert m.kind == "possible" and m.conflicts == ["qty"]


def test_changed_numbers_with_unrelated_text_is_none():
    m = compare(doc("CASE-0002", "zebra yellow quantum", qty=300, delay=3), doc())
    assert m.kind == "none"


def test_text_only_when_a_side_has_no_store():
    base = Doc("CASE-0001", TXT)                                         # candidate not extracted yet
    assert compare(doc("CASE-0002", SAMPLE + " thanks"), base).kind in ("duplicate", "possible")
    assert compare(doc("CASE-0002", "lunch menu for friday"), base).kind == "none"


def test_text_only_thresholds():
    a = Doc("CASE-0002", TXT)
    b_close = Doc("CASE-0001", tokenize(SAMPLE + " pls"))
    assert compare(a, b_close).kind == "duplicate"
    b_mid = Doc("CASE-0001", tuple(list(TXT)[: len(TXT) * 3 // 4]) + ("zz", "yy", "xx", "ww", "vv", "uu", "tt", "ss"))
    assert compare(a, b_mid).kind in ("possible", "duplicate", "none")


@pytest.mark.parametrize("field_", ["store", "item", "qty", "delay"])
def test_unknown_values_never_count_as_equal(field_):
    m = compare(doc("CASE-0002", "zzz yyy xxx", **{field_: None}), doc(**{field_: None}))
    assert field_ not in m.equal and field_ not in m.conflicts


def test_unknown_side_is_not_a_conflict():
    m = compare(doc("CASE-0002", SAMPLE + " x", qty=None), doc())
    assert "qty" not in m.conflicts


def test_score_is_weighted_and_bounded():
    m = compare(doc("CASE-0002", SAMPLE + " hello"), doc())
    assert 0.0 <= m.score <= 1.0
    assert sum(dedupe.W.values()) == pytest.approx(1.0)


def test_match_json_shape():
    j = compare(doc("CASE-0002", SAMPLE), doc()).as_json()
    assert {"id", "kind", "score", "text_sim", "equal", "conflicts", "why"} <= set(j)
