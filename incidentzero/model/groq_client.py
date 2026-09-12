from __future__ import annotations

import json
import os
from typing import Any

from groq import Groq

from incidentzero.domain.models import ModelReply, ToolCall
from .errors import PermanentModelError, TransientModelError


class GroqModelClient:
    def __init__(self, api_key: str | None = None, model: str = "openai/gpt-oss-20b") -> None:
        self.model = model
        self.client = Groq(api_key=api_key or os.getenv("GROQ_API_KEY"))

    def _translate_error(self, exc: Exception) -> Exception:
        status = getattr(exc, "status_code", None)
        if status in {408, 409, 429, 500, 502, 503, 504}:
            return TransientModelError(str(exc))
        return PermanentModelError(str(exc))

    def decide(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                parallel_tool_calls=False,
                temperature=0.1,
                reasoning_effort="low",
            )
        except Exception as exc:  # provider-specific classes intentionally kept out of student code
            # Handle Groq tool_use_failed where model generated slightly malformed JSON
            body = getattr(exc, "body", None)
            if not body and hasattr(exc, "response"):
                try:
                    body = exc.response.json()
                except Exception:
                    pass
            if isinstance(body, dict):
                err = body.get("error", {})
                if err.get("code") == "tool_use_failed" and "failed_generation" in err:
                    failed_gen = str(err.get("failed_generation", ""))
                    import re
                    m_name = re.search(r'"name":\s*"([^"]+)"', failed_gen)
                    if m_name:
                        t_name = m_name.group(1)
                        if t_name in {"verify_recovery", "get_incident"}:
                            return ModelReply(
                                content=None,
                                tool_calls=[ToolCall(id="call_salvaged_0", name=t_name, arguments={})],
                            )
                        return ModelReply(
                            content=None,
                            tool_calls=[ToolCall(id="call_salvaged_0", name=t_name, arguments={"__malformed_arguments__": failed_gen})],
                        )
            raise self._translate_error(exc) from exc

        msg = response.choices[0].message
        calls: list[ToolCall] = []
        for call in (msg.tool_calls or []):
            try:
                args = json.loads(call.function.arguments)
            except Exception:
                args = {"__malformed_arguments__": call.function.arguments}
            calls.append(ToolCall(id=call.id, name=call.function.name, arguments=args))
        usage = {}
        if getattr(response, "usage", None):
            usage = {
                "prompt_tokens": getattr(response.usage, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(response.usage, "completion_tokens", 0) or 0,
                "total_tokens": getattr(response.usage, "total_tokens", 0) or 0,
            }
        return ModelReply(
            content=msg.content,
            tool_calls=calls,
            usage=usage,
            finish_reason=response.choices[0].finish_reason,
        )

    def structured(self, messages: list[dict[str, Any]], schema_name: str, schema: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": schema_name, "strict": True, "schema": schema},
                },
                temperature=0.1,
                reasoning_effort="low",
            )
        except Exception as exc:
            raise self._translate_error(exc) from exc
        content = response.choices[0].message.content or "{}"
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise TransientModelError(f"Model returned invalid structured JSON: {content[:160]}") from exc
