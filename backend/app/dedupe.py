"""Duplicate screening (edge case A4). Code only, no LLM: it cannot be talked into merging or splitting anything.

A new case is compared with recent active cases. Structured facts carry most of the weight (store, item, quantity, delay);
the text of the message is a supporting signal. A value counts as "equal" only when BOTH sides state it, and a value that
differs on both sides is a conflict, so "same store, different quantity" is never auto-merged.

  DUPLICATE  no conflicts and a high score (or the same text): the new case is linked to the original, nothing else happens
  POSSIBLE   related but not certain: the case continues, a human is told (policy rule POSSIBLE_DUPLICATE), nothing is merged
  NONE       unrelated

Only cases with a LOWER sequence number than the new one are candidates. Two identical emails that arrive together are
therefore resolved the same way every time: the older one stays the original, the newer one is the duplicate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from . import adapters, db, safety
from .extract import Field, to_decimal

WINDOW_DAYS = 14
MAX_CANDIDATES = 50
MAX_TOKENS = 3000            # tokens kept per message
MAX_SEQ_TOKENS = 1500        # SequenceMatcher is quadratic in the worst case, so it only sees the head of the text
SKIP_STATUSES = ("DUPLICATE", "CANCELLED", "CLOSED", "SUPERSEDED", "SPLIT")
W = {"store": 0.35, "item": 0.20, "qty": 0.15, "delay": 0.10, "text": 0.20}
DUP_SCORE, POSSIBLE_SCORE = 0.80, 0.55
TEXT_ONLY_DUP, TEXT_ONLY_POSSIBLE = 0.92, 0.70      # one side has no store yet (not extracted): only the text can speak
CONFLICT_TEXT_POSSIBLE = 0.85                        # different store/item but near-identical text: still worth a human look
QTY_DELAY_CONFLICT_TEXT = 0.40                       # same store and item but a changed number: possibly an update or a repeat
DUP_MIN_TEXT = 0.30                                  # facts alone never make a certain duplicate: the wording must overlap too

_HEADER = re.compile(r"^\s*(?:from|to|cc|bcc|sent|date|subject)\s*:.*$", re.I | re.M)
_RULE = re.compile(r"^\s*(?:[-_=*]{3,}.*|.*original message.*)$", re.I | re.M)
_QUOTE = re.compile(r"^[ \t]*(?:>[ \t]*)+", re.M)
_HEX_ID = re.compile(r"\b[0-9a-f]{16,}\b", re.I)
_TOKEN = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class Doc:
    case_id: str
    tokens: tuple[str, ...]
    store: str | None = None
    item: str | None = None
    qty: int | None = None
    delay: int | None = None

    @property
    def fingerprint(self) -> str:
        return " ".join(self.tokens)


@dataclass
class Match:
    case_id: str
    kind: str                                  # duplicate | possible | none
    score: float
    text_sim: float
    conflicts: list[str] = field(default_factory=list)
    equal: list[str] = field(default_factory=list)
    why: str = ""

    def as_json(self) -> dict:
        return {"id": self.case_id, "kind": self.kind, "score": round(self.score, 3), "text_sim": round(self.text_sim, 3),
                "equal": self.equal, "conflicts": self.conflicts, "why": self.why}


def tokenize(text: str) -> tuple[str, ...]:
    """Lower-case word tokens with mail furniture removed (Fwd/Re headers, quote markers, rules, long hex ids)."""
    t = safety.normalize(text or "").lower()
    t = _HEADER.sub(" ", t)
    t = _RULE.sub(" ", t)
    t = _QUOTE.sub("", t)
    t = _HEX_ID.sub(" ", t)
    return tuple(_TOKEN.findall(t)[:MAX_TOKENS])


def text_similarity(a: tuple[str, ...], b: tuple[str, ...]) -> float:
    """0..1. Half word-set overlap (Jaccard), half ordered similarity (difflib ratio = 2*M/T)."""
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    jac = len(sa & sb) / len(sa | sb)
    sm = SequenceMatcher(None, a[:MAX_SEQ_TOKENS], b[:MAX_SEQ_TOKENS], autojunk=False)
    ub = sm.quick_ratio()                       # an upper bound on ratio(); a low bound means the full ratio is not worth computing
    seq = ub if ub < 0.35 else sm.ratio()
    return round(0.5 * jac + 0.5 * seq, 4)


def compare(a: Doc, b: Doc) -> Match:
    """Pure scoring of the new message `a` against an existing case `b`."""
    sim = 1.0 if a.fingerprint == b.fingerprint and a.tokens else text_similarity(a.tokens, b.tokens)
    equal, conflicts = [], []
    for k in ("store", "item", "qty", "delay"):
        x, y = getattr(a, k), getattr(b, k)
        if x is not None and y is not None:
            (equal if x == y else conflicts).append(k)
    score = sum(W[k] for k in equal) + W["text"] * sim

    def m(kind: str, why: str) -> Match:
        return Match(b.case_id, kind, score, sim, conflicts, equal, why)

    if a.tokens and a.fingerprint == b.fingerprint:
        return Match(b.case_id, "duplicate", 1.0, 1.0, conflicts, equal, "same message text")
    if a.store is None or b.store is None:      # one side is not extracted yet: text only
        if sim >= TEXT_ONLY_DUP:
            return m("duplicate", f"text {sim:.2f} similar (store not known on both sides)")
        return m("possible", f"text {sim:.2f} similar (store not known on both sides)") if sim >= TEXT_ONLY_POSSIBLE else m("none", "unrelated")
    if "store" in conflicts or "item" in conflicts:
        if sim >= CONFLICT_TEXT_POSSIBLE:
            return m("possible", f"text {sim:.2f} similar but {'/'.join(c for c in conflicts if c in ('store', 'item'))} differs")
        return m("none", "different " + "/".join(c for c in conflicts if c in ("store", "item")))
    if conflicts:                                # same store, item agrees or unknown, a number differs
        if sim >= QTY_DELAY_CONFLICT_TEXT:
            return m("possible", f"same store, {'/'.join(conflicts)} differs: an update or a repeat, a human decides")
        return m("none", "same store but different figures and text")
    if score >= DUP_SCORE and sim >= DUP_MIN_TEXT:
        return m("duplicate", f"score {score:.2f}: " + ", ".join(equal) + " equal")
    if score >= POSSIBLE_SCORE:
        return m("possible", f"score {score:.2f}: " + (", ".join(equal) or "text only") + " equal")
    return m("none", "unrelated")


# ───────────────────────── database side ─────────────────────────
@dataclass
class Screen:
    duplicate: Match | None = None             # best certain duplicate (the original), if any
    possible: list[Match] = field(default_factory=list)   # related cases a human should look at (best first, max 3)
    candidates: int = 0


def seq_of(case_id: str) -> int:
    return int(case_id.rsplit("-", 1)[1])


async def doc_for(case_id: str, text: str, fields: dict[str, Field]) -> Doc:
    def val(n: str) -> str | None:
        f = fields.get(n)
        return f.value if f is not None and f.usable else None

    store, item = await adapters.resolve_store(val("location")), await adapters.resolve_item(val("item"))
    q, d = to_decimal(val("quantity_affected")), to_decimal(val("delay_days"))
    return Doc(case_id, tokenize(text), store["id"] if store else None, item["id"] if item else None,
               int(q) if q is not None else None, int(d) if d is not None else None)


async def screen(case_id: str, text: str, fields: dict[str, Field], parent_id: str | None = None) -> Screen:
    """Compare a new case with recent active cases that were created before it."""
    me = await doc_for(case_id, text, fields)
    async with db.pool.connection() as c:
        rows = await (await c.execute(
            "SELECT c.id, c.parent_id, m.body FROM cases c "
            "JOIN case_sources s ON s.case_id=c.id AND s.role='origin' JOIN source_messages m ON m.id=s.message_id "
            "WHERE c.status <> ALL(%s) AND c.created_at > now() - make_interval(days => %s) AND c.id <> %s "
            "AND substring(c.id from '[0-9]+$')::int < %s ORDER BY c.created_at DESC LIMIT %s",
            (list(SKIP_STATUSES), WINDOW_DAYS, case_id, seq_of(case_id), MAX_CANDIDATES))).fetchall()
        rows = [r for r in rows if not (parent_id and r["parent_id"] == parent_id)]      # siblings of one message are never duplicates
        vals: dict[str, dict] = {}
        if rows:
            for r in await (await c.execute(
                    "SELECT e.case_id, e.name, e.value FROM extracted_fields e JOIN cases c ON c.id=e.case_id AND e.version=c.version "
                    "WHERE e.case_id = ANY(%s) AND e.name IN ('location','item','quantity_affected','delay_days') AND e.state <> 'dropped'",
                    ([r["id"] for r in rows],))).fetchall():
                vals.setdefault(r["case_id"], {})[r["name"]] = r["value"]
    out = Screen(candidates=len(rows))
    cache: dict[tuple[str, str | None], str | None] = {}

    async def ref(kind: str, v):
        key = (kind, v if isinstance(v, str) else None)
        if key not in cache:
            r = await (adapters.resolve_store(v) if kind == "store" else adapters.resolve_item(v)) if isinstance(v, str) else None
            cache[key] = r["id"] if r else None
        return cache[key]

    matches: list[Match] = []
    for r in rows:
        v = vals.get(r["id"], {})
        q = to_decimal(v["quantity_affected"]) if isinstance(v.get("quantity_affected"), str) else None
        d = to_decimal(v["delay_days"]) if isinstance(v.get("delay_days"), str) else None
        cand = Doc(r["id"], tokenize(r["body"]), await ref("store", v.get("location")), await ref("item", v.get("item")),
                   int(q) if q is not None else None, int(d) if d is not None else None)
        mt = compare(me, cand)
        if mt.kind != "none":
            matches.append(mt)
    dups = sorted((m for m in matches if m.kind == "duplicate"), key=lambda m: (-m.score, seq_of(m.case_id)))
    out.duplicate = dups[0] if dups else None
    out.possible = sorted((m for m in matches if m.kind == "possible"), key=lambda m: -m.score)[:3]
    return out
