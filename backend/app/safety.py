"""Input hygiene + deterministic prompt-injection scanner.

The scanner is one layer of several (see plan): it runs on the ORIGINAL text (so hidden comments / zero-width tricks are
seen), is tuned NOT to flag benign requests such as "obtain approval for additional cost by 4 PM", and returns spans so
Intake can drop any field whose evidence lies inside an attacker-controlled span.
"""
from __future__ import annotations

import base64
import re
import unicodedata
from dataclasses import dataclass, field

MAX_BODY = 20000
MAX_BLOCK_LINES = 3
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍‎‏⁠﻿‪‫‬‭‮"), None)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_TAGS = re.compile(r"<[^>]{1,200}>")
# block-level HTML ends a "line": without this a whole HTML email is one line and one injected paragraph taints all of it
_BLOCK = re.compile(r"<br\s*/?>|</?(?:p|div|li|ul|ol|tr|table|h[1-6]|blockquote|section|article)\b[^>]{0,200}>", re.I)

# imperative language aimed at the system / assistant. Deliberately specific so ordinary ops requests do not match.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("override_instructions", re.compile(r"\b(ignore|disregard|forget|override|bypass)\b[^.\n]{0,40}\b(previous|prior|above|earlier|all|any|your|the|these|safety|policy|policies)\b[^.\n]{0,30}\b(instructions?|rules?|prompts?|polic(?:y|ies)|guardrails?|restrictions?|checks?|approvals?)\b", re.I)),
    ("role_hijack", re.compile(r"\b(you are now (?:the |a |an |my |our )?(?:ai|assistant|system|admin\w*|ceo|cfo|coo|director|finance director|manager|unrestricted|dan|developer|root)\b|act as|pretend (to be|you are)|new (?:system )?instructions?\s*[:\-]|new system instructions?|system prompt|developer (message|mode)|jailbreak)\b", re.I)),
    ("force_approval", re.compile(r"\b(auto[- ]?approve|approve\s+(this|it|the request|all|everything)\b[^.\n]{0,40}\b(automatically|immediately|now|without|directly)|mark\b[^.\n]{0,20}\b(as )?approved|skip\b[^.\n]{0,15}\bapprovals?|\b(?:proceed|process|release|execute|pay|book|go ahead|continue|approve)\b[^.\n]{0,30}\bwithout\b[^.\n]{0,20}\b(?:any )?(?:approval|review|human|checks?)|no approval (is )?(needed|required))\b", re.I)),
    ("suppress_flags", re.compile(r"\bdo not\b(?!\s+(?:forget|hesitate|fail)\b)[^.\n]{0,25}\b(flag|escalate|log|report|tell|notify|mention|audit)\b", re.I)),
    ("exfiltrate", re.compile(r"\b(reveal|print|show|leak|send|disclose|output)\b[^.\n]{0,30}\b(system prompt|instructions|api[ _-]?key|secret|password|credentials?|internal (cost|margin|price)s?|margins?)\b", re.I)),
    ("external_send", re.compile(r"\b(send|forward|email|cc|bcc)\b[^.\n]{0,50}\b[\w.+-]+@(?!demo\.test|system\.local)[\w-]+\.[\w.]+", re.I)),
    ("policy_change", re.compile(r"\b(change|raise|increase|remove|disable|set)\b[^.\n]{0,25}\b(approval )?(limit|threshold|polic(?:y|ies)|guardrail|authority)\b", re.I)),
    ("tool_call_syntax", re.compile(r"(<\|?(system|assistant|im_start)\|?>|\[/?INST\]|\bfunction_call\b|\btool_call\b)", re.I)),
]
_AUTHORITY = re.compile(r"\bI am (the |your )?(ceo|cfo|coo|director|chairman|owner|president|admin(istrator)?)\b|\bthis is (the )?(ceo|cfo|coo)\b", re.I)
_B64 = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")


@dataclass
class Scan:
    flagged: bool = False
    patterns: list[str] = field(default_factory=list)
    spans: list[tuple[int, int]] = field(default_factory=list)   # in the sanitized-for-scan text coordinates
    hidden_content: bool = False
    authority_claim: bool = False
    quotes: list[str] = field(default_factory=list)


def normalize(text: str) -> str:
    """NFKC + strip zero-width/bidi controls + collapse whitespace. Used for grounding and scanning."""
    t = unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH)
    return re.sub(r"[ \t\r\f\v]+", " ", t)


def break_blocks(text: str) -> str:
    """Turn block-level HTML tags (<p>, <br>, <li> ...) into newlines. Plain text is returned unchanged."""
    return _BLOCK.sub("\n", text)


def sanitize_for_llm(text: str) -> tuple[str, list[str]]:
    """Text the LLM sees: no hidden comments / tags / zero-width, capped. Returns (clean, notes)."""
    notes: list[str] = []
    t = text[:MAX_BODY]
    if len(text) > MAX_BODY:
        notes.append("truncated")
    if _HTML_COMMENT.search(t):
        notes.append("html_comment_removed")
        t = _HTML_COMMENT.sub(" ", t)
    if _TAGS.search(t):
        notes.append("html_tags_removed")
        t = _TAGS.sub(" ", _BLOCK.sub("\n", t))
    if t != t.translate(_ZERO_WIDTH):
        notes.append("zero_width_removed")
    return normalize(t), notes


def _expand_to_block(text: str, s: int, e: int) -> tuple[int, int] | None:
    """The paragraph (or the run of "> " quoted lines) that holds the match. Attackers split an instruction and its payload
    over several lines of one block, so the whole block is distrusted. None when the block is too long to be useful."""
    lines = text.split("\n")
    offs, pos = [], 0
    for ln in lines:
        offs.append((pos, pos + len(ln)))
        pos += len(ln) + 1
    idx = lambda p: next((i for i, (a, b) in enumerate(offs) if a <= p <= b), len(lines) - 1)  # noqa: E731
    i0, i1 = idx(s), idx(max(e - 1, s))
    q = lambda i: lines[i].lstrip().startswith(">")  # noqa: E731
    while i0 > 0 and lines[i0 - 1].strip() and q(i0 - 1) == q(i0):
        i0 -= 1
    while i1 < len(lines) - 1 and lines[i1 + 1].strip() and q(i1 + 1) == q(i1):
        i1 += 1
    a, b = offs[i0][0], offs[i1][1]
    # a long run of single-newline lines is just one paragraph of ordinary mail: do not let one bad line taint all of it
    # quoted text ("> ...") is one unit by nature, so it keeps the long cap
    return (a, b) if b - a <= 600 and (q(i0) or i1 - i0 + 1 <= MAX_BLOCK_LINES) else None


def _expand_to_sentence(text: str, s: int, e: int) -> tuple[int, int]:
    """Taint the whole paragraph; if it is too long, the line; if that is too long, the sentence."""
    block = _expand_to_block(text, s, e)
    if block is not None:
        return block
    ls = text.rfind("\n", 0, s) + 1
    le = text.find("\n", e)
    le = len(text) if le == -1 else le
    if le - ls <= 400:
        return ls, le
    left = max(text.rfind("\n", 0, s), text.rfind(". ", 0, s))
    right_candidates = [i for i in (text.find("\n", e), text.find(". ", e)) if i != -1]
    right = min(right_candidates) if right_candidates else len(text)
    return (left + 1 if left != -1 else 0), right


def scan(text: str) -> Scan:
    """Scan the original text (comments and all) in normalized form. Spans refer to normalize(text)."""
    raw = text[:MAX_BODY]
    norm = normalize(raw)
    res = Scan()
    if raw != raw.translate(_ZERO_WIDTH):
        res.hidden_content = True
        res.patterns.append("zero_width_chars")
    for m in _HTML_COMMENT.finditer(norm):
        inner = m.group(0)
        if any(p.search(inner) for _, p in _PATTERNS) or len(inner) > 120:
            res.hidden_content = True
            res.patterns.append("hidden_html_comment")
            res.spans.append((m.start(), m.end()))
            res.quotes.append(inner[:200])
    for name, pat in _PATTERNS:
        for m in pat.finditer(norm):
            s, e = _expand_to_sentence(norm, m.start(), m.end())
            res.patterns.append(name)
            res.spans.append((s, e))
            res.quotes.append(norm[s:e].strip()[:200])
    for m in _B64.finditer(norm):
        try:
            dec = base64.b64decode(m.group(0), validate=False).decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            continue
        if any(p.search(dec) for _, p in _PATTERNS):
            res.patterns.append("encoded_instruction")
            res.spans.append((m.start(), m.end()))
            res.quotes.append(dec[:200])
    if _AUTHORITY.search(norm):
        res.authority_claim = True
    res.flagged = bool(res.patterns)
    res.patterns = sorted(set(res.patterns))
    return res


def in_span(norm_text: str, quote: str, spans: list[tuple[int, int]]) -> bool:
    """True if `quote` occurs inside any flagged span (all occurrences inside => unsafe evidence)."""
    q = normalize(quote).strip()
    if not q or not spans:
        return False
    idxs = [m.start() for m in re.finditer(re.escape(q), norm_text)]
    if not idxs:
        return False
    return all(any(s <= i and i + len(q) <= e for s, e in spans) for i in idxs)
