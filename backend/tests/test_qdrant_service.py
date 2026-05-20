"""Qdrant client compatibility (search API removal in qdrant-client 1.14+)."""

from unittest.mock import MagicMock, patch

from qdrant_client.models import FieldCondition, Filter, MatchValue


@patch("knowledge_base.services.qdrant_service.ensure_collection")
@patch("knowledge_base.services.qdrant_service.get_qdrant_client")
def test_search_vectors_uses_query_points(mock_get_client, _mock_ensure):
    pt = MagicMock()
    pt.score = 0.91
    pt.payload = {"chunk_text": "attendance policy snippet"}
    resp = MagicMock()
    resp.points = [pt]

    client = MagicMock()
    client.query_points.return_value = resp
    mock_get_client.return_value = client

    from knowledge_base.services.qdrant_service import search_vectors

    payload_filter = Filter(
        must=[FieldCondition(key="company_id", match=MatchValue(value="company-a"))]
    )
    hits = search_vectors(
        [0.1, 0.2, 0.3],
        limit=5,
        score_threshold=0.5,
        payload_filter=payload_filter,
        trace_id="t1",
    )
    client.query_points.assert_called_once()
    call_kw = client.query_points.call_args.kwargs
    assert call_kw["collection_name"]
    assert call_kw["query"] == [0.1, 0.2, 0.3]
    assert call_kw["limit"] == 5
    assert call_kw["score_threshold"] == 0.5
    assert call_kw["query_filter"] == payload_filter
    assert len(hits) == 1
    assert hits[0].score == 0.91
