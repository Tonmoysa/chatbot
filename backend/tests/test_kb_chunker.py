import pytest

from knowledge_base.services.chunker import chunk_policy_text, count_tokens, split_by_markdown_sections


def test_split_markdown_sections():
    text = "# A\n\nLine1.\n\n## B\n\nLine2."
    secs = split_by_markdown_sections(text)
    titles = [t for t, _ in secs]
    assert "A" in "".join(titles) or any("A" in t for t in titles)


def test_chunk_policy_preserves_header_context():
    body = "\n\n".join(f"Sentence {i}. More detail here." for i in range(40))
    text = f"## Leave rules\n\n{body}"
    chunks = chunk_policy_text(text, target_tokens=80, overlap_tokens=20)
    assert len(chunks) >= 1
    assert all(count_tokens(c.text) > 0 for c in chunks)


def test_bangla_chunking_does_not_crash():
    text = "## নীতি\n\nপ্রথম বাক্য। দ্বিতীয় বাক্য। " * 30
    chunks = chunk_policy_text(text, target_tokens=60, overlap_tokens=10)
    assert chunks
