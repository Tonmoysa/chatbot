import uuid

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from chat.serializers import (
    ChatRequestSerializer,
    DecisionRequestSerializer,
    ExtractRequestSerializer,
    HrEnvelopeSerializer,
    IntentRequestSerializer,
    MockCreateSerializer,
)
from chat.services.crm.factory import get_crm_adapter
from chat.services.decision_engine import DecisionEngine
from chat.services.entity_extractor import EntityExtractor
from chat.services.intent_detector import IntentDetector
from chat.services.memory_store import ConversationMemoryStore
from chat.services.observability import log_step
from chat.services.orchestrator import ChatOrchestrator


def _trace(request) -> str:
    return getattr(request, "trace_id", None) or str(uuid.uuid4())


@extend_schema(
    summary="Service health",
    tags=["Health"],
    responses={200: HrEnvelopeSerializer},
    auth=[],
)
class HealthView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        tid = _trace(request)
        return Response(
            {
                "trace_id": tid,
                "intent": "",
                "entities": {},
                "decision": {},
                "response": {
                    "message": "HR chatbot microservice is running.",
                    "status": "success",
                    "request_id": "",
                },
                "status": "success",
            }
        )


@extend_schema(
    summary="Chat (full pipeline)",
    tags=["Chat"],
    request=ChatRequestSerializer,
    responses={200: HrEnvelopeSerializer},
)
class ChatView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        ser = ChatRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        tid = _trace(request)
        orch = ChatOrchestrator()
        out = orch.run_chat(
            message=ser.validated_data["message"],
            session_id=ser.validated_data.get("session_id"),
            employee_id=ser.validated_data.get("employee_id") or "demo-employee",
            trace_id=tid,
        )
        sid = out.pop("_session_id", None)
        resp = Response(out, status=status.HTTP_200_OK)
        if sid:
            resp["X-Session-Id"] = sid
        return resp


@extend_schema(
    summary="Intent detection only",
    tags=["Chat"],
    request=IntentRequestSerializer,
    responses={200: HrEnvelopeSerializer},
)
class IntentView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        ser = IntentRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        tid = _trace(request)
        det = IntentDetector()
        r = det.detect(ser.validated_data["message"], tid)
        log_step(tid, "intent_only", {"intent": r.get("intent")})
        return Response(
            {
                "trace_id": tid,
                "intent": r.get("intent", ""),
                "entities": {"confidence": r.get("confidence"), "source": r.get("source")},
                "decision": {},
                "response": {
                    "message": "Intent detection complete.",
                    "status": "success",
                    "request_id": "",
                },
                "status": "success",
            }
        )


@extend_schema(
    summary="Entity extraction only",
    tags=["Chat"],
    request=ExtractRequestSerializer,
    responses={200: HrEnvelopeSerializer},
)
class ExtractView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        ser = ExtractRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        tid = _trace(request)
        mem = ConversationMemoryStore()
        session = mem.get_or_create_session(ser.validated_data.get("session_id") or "", "")
        ctx = mem.recent_context_lines(session)
        ext = EntityExtractor()
        r = ext.extract(
            ser.validated_data["message"],
            ser.validated_data["intent"],
            ctx,
            tid,
        )
        log_step(tid, "extract_only", {})
        return Response(
            {
                "trace_id": tid,
                "intent": ser.validated_data["intent"],
                "entities": r.get("entities") or {},
                "decision": {"source": r.get("source")},
                "response": {
                    "message": "Entity extraction complete.",
                    "status": "success",
                    "request_id": "",
                },
                "status": "success",
            }
        )


@extend_schema(
    summary="Decision engine only",
    tags=["Chat"],
    request=DecisionRequestSerializer,
    responses={200: HrEnvelopeSerializer},
)
class DecisionView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        ser = DecisionRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        tid = _trace(request)
        crm = get_crm_adapter()
        crm_context: dict = {}
        intent = ser.validated_data["intent"]
        entities = ser.validated_data["entities"]
        emp = ser.validated_data.get("employee_id") or "demo-employee"
        from chat.constants import (
            INTENT_LEAVE_BALANCE,
            INTENT_LEAVE_REQUEST,
            INTENT_WFH_REQUEST,
        )

        if intent in (INTENT_LEAVE_REQUEST, INTENT_WFH_REQUEST, INTENT_LEAVE_BALANCE):
            crm_context.update(crm.get_leave_balance(emp))
        eng = DecisionEngine()
        decision = eng.evaluate(
            intent=intent, entities=entities, crm_context=crm_context
        )
        log_step(tid, "decision_only", {"outcome": decision.get("outcome")})
        return Response(
            {
                "trace_id": tid,
                "intent": intent,
                "entities": entities,
                "decision": decision,
                "response": {
                    "message": "Decision evaluation complete.",
                    "status": "success",
                    "request_id": "",
                },
                "status": "success",
            }
        )


@extend_schema(
    summary="Request status by id",
    tags=["CRM"],
    responses={200: HrEnvelopeSerializer},
)
class RequestStatusView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, request_id: str):
        tid = _trace(request)
        crm = get_crm_adapter()
        st = crm.get_request_status(request_id)
        return Response(
            {
                "trace_id": tid,
                "intent": "REQUEST_STATUS",
                "entities": {"request_id": request_id},
                "decision": {"outcome": "INFORMATIONAL", "reason": "Lookup"},
                "response": {
                    "message": f"Status: {st.get('status', 'unknown')}",
                    "status": "success",
                    "request_id": request_id,
                },
                "status": "success",
            }
        )


@extend_schema(
    summary="Mock: create HR request",
    tags=["Mock CRM"],
    request=MockCreateSerializer,
    responses={200: HrEnvelopeSerializer},
)
class MockRequestCreateView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        ser = MockCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        tid = _trace(request)
        crm = get_crm_adapter()
        r = crm.create_request(
            ser.validated_data["employee_id"],
            ser.validated_data["intent"],
            ser.validated_data.get("entities") or {},
            ser.validated_data.get("decision") or {},
        )
        return Response(
            {
                "trace_id": tid,
                "intent": ser.validated_data["intent"],
                "entities": ser.validated_data.get("entities") or {},
                "decision": ser.validated_data.get("decision") or {},
                "response": {
                    "message": "Mock request created.",
                    "status": "success",
                    "request_id": str(r.get("request_id", "")),
                },
                "status": "success",
            }
        )


@extend_schema(
    summary="Mock: request status",
    tags=["Mock CRM"],
    parameters=[
        OpenApiParameter(
            name="request_id",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            required=False,
        ),
    ],
    responses={200: HrEnvelopeSerializer},
)
class MockRequestStatusView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        tid = _trace(request)
        rid = request.query_params.get("request_id", "")
        crm = get_crm_adapter()
        st = crm.get_request_status(rid) if rid else {"status": "MISSING_ID"}
        return Response(
            {
                "trace_id": tid,
                "intent": "REQUEST_STATUS",
                "entities": {"request_id": rid},
                "decision": {},
                "response": {
                    "message": str(st),
                    "status": "success",
                    "request_id": rid,
                },
                "status": "success",
            }
        )


@extend_schema(
    summary="Mock: leave balance",
    tags=["Mock CRM"],
    parameters=[
        OpenApiParameter(
            name="employee_id",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            required=False,
        ),
    ],
    responses={200: HrEnvelopeSerializer},
)
class MockLeaveBalanceView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        tid = _trace(request)
        emp = request.query_params.get("employee_id", "demo-employee")
        crm = get_crm_adapter()
        bal = crm.get_leave_balance(emp)
        return Response(
            {
                "trace_id": tid,
                "intent": "LEAVE_BALANCE",
                "entities": {"employee_id": emp},
                "decision": {"outcome": "INFORMATIONAL"},
                "response": {
                    "message": f"Balance payload: {bal}",
                    "status": "success",
                    "request_id": "",
                },
                "status": "success",
            }
        )
