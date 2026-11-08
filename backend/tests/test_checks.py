from __future__ import annotations

from xbot.intelligence import schemas as S
from xbot.intelligence.checks import (
    code_checks,
    coverage_issues,
    find_urls,
    near_duplicates,
    scan_secrets,
    split_sentences,
    weighted_length,
)


def _fact(fid: str, confidence: S.Confidence = "confirmed") -> S.FactWithId:
    return S.FactWithId(
        id=fid,
        kind="activity",
        text=f"fact {fid}",
        confidence=confidence,
        source_refs=["journal:1"],
    )


FACTS = {"F1": _fact("F1"), "F2": _fact("F2", "inferred"), "F3": _fact("F3", "unknown")}


def test_weighted_length_counts_links_as_23_and_wide_chars_as_2() -> None:
    assert weighted_length("hello") == 5
    assert weighted_length("see https://example.com/a/very/long/path/that/goes/on") == 4 + 23
    assert weighted_length("日本") == 4
    assert weighted_length("ok 🚀") == 3 + 2


def test_find_urls_and_secret_patterns() -> None:
    assert find_urls("go to www.example.com now") == ["www.example.com"]
    assert scan_secrets("key AKIAABCDEFGHIJKLMNOP leaked") == ["AWS access key"]
    assert "GitHub token" in scan_secrets("ghp_" + "a" * 36)
    assert "Anthropic key" in scan_secrets("sk-ant-" + "x" * 24)
    assert "JWT" in scan_secrets("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop")
    assert "password assignment" in scan_secrets("password=hunter22")
    assert scan_secrets("I rotated the API key today and nothing broke.") == []


def test_near_duplicates_uses_token_jaccard() -> None:
    text = "Built the GitHub sync job and learned how PKCE verifiers work"
    assert near_duplicates(
        text, ["built the github sync job and learned how pkce verifiers work today"]
    )
    assert near_duplicates(text, ["Completely different post about dynamic programming"]) == []


def test_split_sentences_ignores_fragments() -> None:
    assert split_sentences("First thing. Second thing!\nThird line here") == [
        "First thing.",
        "Second thing!",
        "Third line here",
    ]


def test_coverage_requires_every_sentence_and_sound_facts() -> None:
    text = "Fixed the sync retry. Learned PKCE today. This felt good."
    claims = [
        S.Claim(sentence="Fixed the sync retry.", kind="fact", fact_ids=["F1"], hedged=False),
        S.Claim(sentence="This felt good.", kind="opinion", fact_ids=[], hedged=False),
    ]
    issues = coverage_issues(text, claims, FACTS)
    assert len(issues) == 1 and "uncovered sentence" in issues[0] and "Learned PKCE" in issues[0]

    bad_claims = [
        S.Claim(sentence="Fixed the sync retry.", kind="fact", fact_ids=["F9"], hedged=False),
        S.Claim(sentence="Learned PKCE today.", kind="fact", fact_ids=["F2"], hedged=False),
        S.Claim(sentence="This felt good.", kind="fact", fact_ids=["F3"], hedged=True),
    ]
    issues = coverage_issues(text, bad_claims, FACTS)
    assert any("unknown fact ids" in i for i in issues)
    assert any("unhedged claim rests only on inferred" in i for i in issues)
    assert any("unknown-confidence" in i for i in issues)

    hedged = [
        S.Claim(sentence="Fixed the sync retry.", kind="fact", fact_ids=["F1"], hedged=False),
        S.Claim(sentence="Learned PKCE today.", kind="fact", fact_ids=["F2"], hedged=True),
        S.Claim(sentence="This felt good.", kind="opinion", fact_ids=[], hedged=False),
    ]
    assert coverage_issues(text, hedged, FACTS) == []


def test_code_checks_combine_all_rules() -> None:
    claims = [S.Claim(sentence="x", kind="opinion", fact_ids=[], hedged=False)]
    issues = code_checks(
        text="Shipped it https://example.com password=hunter22 " + "y" * 300,
        claims=claims,
        facts_by_id=FACTS,
        max_chars=280,
        allow_links=False,
        recent_texts=[],
    )
    kinds = " | ".join(issues)
    assert "too long" in kinds and "contains a link" in kinds and "possible secret" in kinds
    assert "uncovered sentence" in kinds


def test_multiline_sentence_covered_by_one_claim_passes() -> None:
    text = "Learned how PKCE verifiers work\nand why the code expires in 30 seconds."
    claims = [S.Claim(sentence=text.replace("\n", " "), kind="fact", fact_ids=["F1"], hedged=False)]
    assert coverage_issues(text, claims, FACTS) == []
