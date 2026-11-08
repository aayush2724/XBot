"""Deterministic draft checks that run before any model-based evaluation. Free, fast, and the
reason a hallucinated or leaky draft never reaches the owner."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from xbot.intelligence.schemas import Claim, FactWithId

URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
URL_WEIGHT = 23  # X wraps links with t.co and counts them as 23 characters
COVERAGE_THRESHOLD = 0.85
DUPLICATE_THRESHOLD = 0.6

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b")),
    ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b")),
    ("OpenAI-style key", re.compile(r"\bsk-(?!ant-)[A-Za-z0-9_-]{20,}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    (
        "password assignment",
        re.compile(r"\b(?:password|passwd|pwd)\s*[:=]\s*\S{6,}", re.IGNORECASE),
    ),
    (
        "API key assignment",
        re.compile(r"\bapi[_-]?key\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{12,}", re.IGNORECASE),
    ),
]

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_WORD = re.compile(r"[a-z0-9']+")


def find_urls(text: str) -> list[str]:
    return URL_RE.findall(text)


def weighted_length(text: str) -> int:
    """Approximate X's counting: links count 23, most characters 1, wide characters
    (CJK, emoji, symbols above U+1100) count 2."""
    without_urls = URL_RE.sub("", text)
    n_urls = len(find_urls(text))
    total = n_urls * URL_WEIGHT
    for ch in without_urls:
        total += 2 if ord(ch) >= 0x1100 else 1
    return total


def scan_secrets(text: str) -> list[str]:
    return [name for name, pattern in SECRET_PATTERNS if pattern.search(text)]


def normalize_tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if len(w) > 2}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def near_duplicates(
    text: str, others: list[str], threshold: float = DUPLICATE_THRESHOLD
) -> list[tuple[str, float]]:
    mine = normalize_tokens(text)
    hits: list[tuple[str, float]] = []
    for other in others:
        score = jaccard(mine, normalize_tokens(other))
        if score >= threshold:
            hits.append((other[:80], round(score, 3)))
    return hits


def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE_SPLIT.split(text)]
    return [p for p in parts if len(_WORD.findall(p.lower())) >= 2]


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower()).strip(" \"'“”‘’.!?,;:")


def coverage_issues(
    text: str, claims: list[Claim], facts_by_id: dict[str, FactWithId]
) -> list[str]:
    """Every sentence must be covered by a claim; every fact claim must cite real, non-unknown
    facts; claims resting only on inferred facts must be hedged."""
    issues: list[str] = []
    normalized_claims = [_normalize(c.sentence) for c in claims]
    for sentence in split_sentences(text):
        target = _normalize(sentence)
        contained = len(target) >= 10 and any(target in c for c in normalized_claims)
        best = max(
            (SequenceMatcher(None, target, c).ratio() for c in normalized_claims), default=0.0
        )
        if not contained and best < COVERAGE_THRESHOLD:
            issues.append(f"uncovered sentence (no matching claim): {sentence[:80]}")
    for claim in claims:
        if claim.kind != "fact":
            continue
        if not claim.fact_ids:
            issues.append(f"fact claim cites no facts: {claim.sentence[:80]}")
            continue
        missing = [i for i in claim.fact_ids if i not in facts_by_id]
        if missing:
            issues.append(f"claim cites unknown fact ids {missing}: {claim.sentence[:80]}")
            continue
        cited = [facts_by_id[i] for i in claim.fact_ids]
        if any(f.confidence == "unknown" for f in cited):
            issues.append(f"claim rests on an unknown-confidence fact: {claim.sentence[:80]}")
        elif all(f.confidence == "inferred" for f in cited) and not claim.hedged:
            issues.append(f"unhedged claim rests only on inferred facts: {claim.sentence[:80]}")
    return issues


def code_checks(
    *,
    text: str,
    claims: list[Claim],
    facts_by_id: dict[str, FactWithId],
    max_chars: int,
    allow_links: bool,
    recent_texts: list[str],
) -> list[str]:
    issues: list[str] = []
    length = weighted_length(text)
    if length > max_chars:
        issues.append(f"too long: {length} weighted chars > {max_chars}")
    urls = find_urls(text)
    if urls and not allow_links:
        issues.append(f"contains a link but links are disabled: {urls[0][:60]}")
    for name in scan_secrets(text):
        issues.append(f"possible secret in text: {name}")
    for other, score in near_duplicates(text, recent_texts):
        issues.append(f"near-duplicate of a published post (jaccard {score}): {other}")
    issues.extend(coverage_issues(text, claims, facts_by_id))
    return issues
