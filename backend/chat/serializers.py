from rest_framework import serializers


class HrEnvelopeSerializer(serializers.Serializer):
    """Standard API response envelope (OpenAPI / contract)."""

    trace_id = serializers.CharField()
    intent = serializers.CharField(allow_blank=True, required=False)
    entities = serializers.JSONField()
    decision = serializers.JSONField()
    response = serializers.JSONField()
    status = serializers.CharField()


class ChatRequestSerializer(serializers.Serializer):
    message = serializers.CharField(max_length=8000)
    session_id = serializers.CharField(
        max_length=64, required=False, allow_blank=True, default=""
    )
    employee_id = serializers.CharField(
        max_length=64, required=False, allow_blank=True, default="demo-employee"
    )
    # Optional: text extracted from an uploaded receipt/document
    document_text = serializers.CharField(
        max_length=60000, required=False, allow_blank=True, default=""
    )


class DocumentExtractRequestSerializer(serializers.Serializer):
    file = serializers.FileField()


class IntentRequestSerializer(serializers.Serializer):
    message = serializers.CharField(max_length=8000)


class ExtractRequestSerializer(serializers.Serializer):
    message = serializers.CharField(max_length=8000)
    intent = serializers.CharField(max_length=64)
    session_id = serializers.CharField(
        max_length=64, required=False, allow_blank=True, default=""
    )


class DecisionRequestSerializer(serializers.Serializer):
    intent = serializers.CharField(max_length=64)
    entities = serializers.JSONField()
    employee_id = serializers.CharField(
        max_length=64, required=False, allow_blank=True, default="demo-employee"
    )


class MockCreateSerializer(serializers.Serializer):
    employee_id = serializers.CharField(max_length=64, default="demo-employee")
    intent = serializers.CharField(max_length=64)
    entities = serializers.JSONField(required=False, default=dict)
    decision = serializers.JSONField(required=False, default=dict)
