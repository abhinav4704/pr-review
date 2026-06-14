"""AWS Bedrock Nova Pro client — supports both simple completion and tool use."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional


class NovaClient:
    def __init__(
        self,
        model_id: str = "us.amazon.nova-pro-v1:0",
        region: str = "us-east-1",
        max_tokens: int = 4096,
        temperature: float = 0.2,
        top_p: float = 0.9,
        aws_access_key_id: str = "",
        aws_secret_access_key: str = "",
        aws_session_token: str = "",
    ) -> None:
        import boto3
        from botocore.config import Config

        self.model_id = model_id
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.top_p = top_p

        creds: Dict[str, str] = {}
        if aws_access_key_id and aws_secret_access_key:
            creds["aws_access_key_id"] = aws_access_key_id
            creds["aws_secret_access_key"] = aws_secret_access_key
            if aws_session_token:
                creds["aws_session_token"] = aws_session_token

        self._client = boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=Config(connect_timeout=30, read_timeout=120,
                          retries={"max_attempts": 6, "mode": "adaptive"}),
            **creds,
        )

    # ── guarded converse ───────────────────────────────────────────────────────
    def _converse(self, **kwargs):
        """Call Bedrock converse, turning boto errors into a concise message.

        botocore already retries throttling/5xx adaptively (max_attempts=6). When
        it still fails we re-raise a short, classified RuntimeError so the UI shows
        one clear cause instead of a raw stack trace repeated per chunk.
        """
        from botocore.exceptions import ClientError, BotoCoreError
        try:
            return self._client.converse(modelId=self.model_id, **kwargs)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "ClientError")
            hint = {
                "ThrottlingException": "Bedrock rate limit hit — reduce review depth/agents or request a quota increase.",
                "TooManyRequestsException": "Bedrock rate limit hit — reduce review depth/agents or request a quota increase.",
                "AccessDeniedException": "Access denied — check AWS credentials and that the model is enabled in this region.",
                "ValidationException": "Invalid request — check the model id and token limits.",
                "ResourceNotFoundException": "Model not found — check the model id and region.",
            }.get(code, "")
            raise RuntimeError(f"Bedrock {code}: {hint or e.response.get('Error', {}).get('Message', str(e))}") from e
        except BotoCoreError as e:
            raise RuntimeError(f"Bedrock connection error: {e}") from e

    # ── simple completion ──────────────────────────────────────────────────────
    def complete(self, system: str, user: str) -> str:
        resp = self._converse(
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            inferenceConfig={
                "maxTokens": self.max_tokens,
                "temperature": self.temperature,
                "topP": self.top_p,
            },
        )
        return _first_text(resp)

    def complete_json(self, system: str, user: str) -> Any:
        return _extract_json(self.complete(system, user))

    # ── agentic tool loop ──────────────────────────────────────────────────────
    def converse_with_tools(self, system, messages, tools):
        bedrock_tools = [{
            "toolSpec": {
                "name": t["name"],
                "description": t["description"],
                "inputSchema": {"json": t["input_schema"]},
            }
        } for t in tools]

        kwargs = dict(
            system=[{"text": system}],
            messages=messages,
            inferenceConfig={"maxTokens": self.max_tokens,
                            "temperature": self.temperature, "topP": self.top_p},
        )
        if bedrock_tools:
            kwargs["toolConfig"] = {"tools": bedrock_tools}

        resp = self._converse(**kwargs)
        try:
            return resp["output"]["message"]["content"]   # RAW bedrock blocks
        except (KeyError, TypeError):
            return []

    @staticmethod
    def normalize_blocks(content):
        out = []
        for block in content:
            if "text" in block:
                out.append({"type": "text", "text": block["text"]})
            elif "toolUse" in block:
                tu = block["toolUse"]
                out.append({"type": "tool_use", "id": tu.get("toolUseId", ""),
                            "name": tu.get("name", ""), "input": tu.get("input", {})})
        return out

# ── response helpers ─────────────────────────────────────────────────────────
def _first_text(resp: Dict[str, Any]) -> str:
    """Return the first text block of a converse response, or '' if none.

    Guards against empty content or tool-only responses (no 'text' key), which
    would otherwise raise IndexError/KeyError.
    """
    try:
        content = resp["output"]["message"]["content"]
    except (KeyError, TypeError):
        return ""
    for block in content or []:
        if isinstance(block, dict) and "text" in block:
            return block["text"]
    return ""


# ── JSON extraction ────────────────────────────────────────────────────────────
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _extract_json(text: str) -> Optional[Any]:
    candidates: List[str] = []
    m = _FENCE.search(text)
    if m:
        candidates.append(m.group(1).strip())
    candidates.append(text.strip())
    for opener, closer in (("[", "]"), ("{", "}")):
        s, e = text.find(opener), text.rfind(closer)
        if s != -1 and e != -1 and e > s:
            candidates.append(text[s: e + 1])
    for cand in candidates:
        try:
            return json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
    return None
