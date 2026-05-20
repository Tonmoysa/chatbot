from unittest.mock import patch

import pytest

from knowledge_base.models import DocumentStatus, DocumentType, KnowledgeDocument


@pytest.mark.django_db
@patch("knowledge_base.signals.delete_by_document_id")
def test_document_delete_purges_qdrant(mock_delete):
    doc = KnowledgeDocument.objects.create(
        company_id="co-test",
        title="Leave Policy",
        document_type=DocumentType.POLICY,
        uploaded_by_employee_id="emp-1",
        status=DocumentStatus.INDEXED,
    )
    pk = doc.pk
    doc.delete()
    mock_delete.assert_called_once()
    args, kwargs = mock_delete.call_args
    assert args[0] == pk
    assert kwargs["company_id"] == "co-test"
