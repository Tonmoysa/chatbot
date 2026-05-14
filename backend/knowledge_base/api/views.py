import uuid

from django.conf import settings
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from knowledge_base.serializers import (
    KbPolicyUploadResponseSerializer,
    KbPolicyUploadSerializer,
)
from knowledge_base.services.ingest import ingest_bytes


def _trace(request) -> str:
    return getattr(request, "trace_id", None) or str(uuid.uuid4())


@extend_schema(
    summary="Upload HR policy document for indexing",
    tags=["Knowledge base"],
    request=KbPolicyUploadSerializer,
    responses={200: KbPolicyUploadResponseSerializer},
)
class KbUploadPolicyView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        ser = KbPolicyUploadSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        tid = _trace(request)
        f = ser.validated_data["file"]
        data = f.read()
        max_b = int(getattr(settings, "KB_MAX_UPLOAD_BYTES", 26_214_400))
        if len(data) > max_b:
            return Response(
                {"detail": "File too large."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        title = (ser.validated_data.get("title") or "").strip() or getattr(
            f, "name", "policy"
        ) or "policy"
        meta = {}
        if ser.validated_data.get("policy_type"):
            meta["policy_type"] = ser.validated_data["policy_type"].strip()
        if ser.validated_data.get("department"):
            meta["department"] = ser.validated_data["department"].strip()
        uid = getattr(request.user, "pk", None) if getattr(
            request.user, "is_authenticated", False
        ) else None
        try:
            result = ingest_bytes(
                data=data,
                title=title,
                filename=getattr(f, "name", None),
                content_type=getattr(f, "content_type", None),
                uploaded_by_id=int(uid) if uid else None,
                trace_id=tid,
                metadata=meta,
                reindex=False,
            )
        except ValueError as exc:
            if str(exc) == "upload_too_large":
                return Response(
                    {"detail": "File too large."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            raise
        out = {
            "document_id": result["document_id"],
            "chunks_created": int(result.get("chunks_created") or 0),
            "status": result.get("status") or "unknown",
        }
        return Response(out, status=status.HTTP_200_OK)
