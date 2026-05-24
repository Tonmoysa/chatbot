from knowledge_base.services.retriever import _rerank_by_policy_title
from knowledge_base.services.sanitization import (
    build_hr_policy_retrieval_query,
    extract_policy_title_phrases,
    hr_retrieval_hint_line,
    preprocess_query,
)


def test_leave_policy_query_keeps_user_wording_and_title():
    q = build_hr_policy_retrieval_query("amake leave policy ta bolo")
    assert "Policy title: Leave Policy" in q
    assert "amake leave policy ta bolo" in q
    assert "sick leave casual leave maternity" not in q


def test_leave_policy_somporke_jante_chai():
    q = build_hr_policy_retrieval_query("ami leave policy somporke jante chai")
    assert "Policy title: Leave Policy" in q
    assert "somporke jante chai" in q


def test_leave_poli_typo_rewritten():
    q = build_hr_policy_retrieval_query("amake leave poli and rules ta bolo")
    assert "Policy title: Leave Policy" in q
    assert "leave poli" in q


def test_information_security_policy_title_extracted():
    titles = extract_policy_title_phrases(
        "amake Information Security Policy t bolo"
    )
    assert titles == ["Information Security Policy"]
    q = build_hr_policy_retrieval_query("amake Information Security Policy t bolo")
    assert q.startswith("Policy title: Information Security Policy.")


def test_leave_rules_maps_to_leave_policy_not_casual():
    titles = extract_policy_title_phrases("leave rules ta bolo amake")
    assert titles == ["Leave Policy"]


def test_named_policy_skips_generic_embedding_hints():
    q = preprocess_query("amake leave policy ta bolo")
    assert hr_retrieval_hint_line(q) == ""


def test_daily_allowance_query_still_gets_hint():
    q = preprocess_query("amar daily allowance koto?")
    hint = hr_retrieval_hint_line(q)
    assert "allowance" in hint.lower() or "TA DA" in hint


def test_rerank_prefers_matching_document_title():
    class Hit:
        def __init__(self, score, title):
            self.score = score
            self.payload = {"document_title": title, "section_title": ""}

    hits = [
        Hit(0.82, "Casual Leave Policy"),
        Hit(0.80, "Information Security Policy"),
    ]
    ranked = _rerank_by_policy_title(
        hits,
        "Policy title: Information Security Policy. amake Information Security Policy t bolo",
    )
    assert ranked[0].payload["document_title"] == "Information Security Policy"
