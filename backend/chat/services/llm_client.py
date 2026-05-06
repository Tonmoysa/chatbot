import json
import logging
import re
from typing import Any

import httpx
from django.conf import settings

logger = logging.getLogger("hr_chatbot")


def _strip_json_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.I)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


class LLMClient:
    """OpenAI-compatible chat completions; strict JSON-only responses."""

    def __init__(self) -> None:
        self.base = settings.LLM_API_BASE_URL
        self.api_key = settings.LLM_API_KEY
        self.model = settings.LLM_MODEL
        self.timeout = settings.LLM_TIMEOUT_SECONDS

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def chat_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        trace_id: str,
    ) -> dict[str, Any] | None:
        if not self.is_configured():
            return None
        for attempt in range(2):
            raw = self._complete(system_prompt, user_prompt, trace_id, attempt)
            parsed = self._parse_json_object(raw)
            if parsed is not None:
                return parsed
            logger.warning(
                "llm_invalid_json trace_id=%s attempt=%s", trace_id, attempt + 1
            )
        return None

    def _complete(
        self, system_prompt: str, user_prompt: str, trace_id: str, attempt: int
    ) -> str | None:
        url = f"{self.base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        extra = ""
        if attempt == 1:
            extra = (
                "\nReturn a single JSON object only. No markdown. No explanation."
            )
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt + extra},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                r = client.post(url, headers=headers, json=body)
                r.raise_for_status()
                data = r.json()
            return (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )
        except Exception as exc:
            logger.warning("llm_http_error trace_id=%s err=%s", trace_id, type(exc).__name__)
            return None

    def _parse_json_object(self, content: str | None) -> dict[str, Any] | None:
        if not content:
            return None
        try:
            parsed = json.loads(_strip_json_fence(content))
            if isinstance(parsed, dict):
                return parsed
            # Some providers occasionally return a single-item JSON array even when
            # asked for an object. Accept it to avoid breaking the pipeline.
            if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
                return parsed[0]
            return None
        except json.JSONDecodeError:
            return None
