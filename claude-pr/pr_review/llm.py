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
            config=Config(connect_timeout=60, read_timeout=300,
                          retries={"max_attempts": 2}),
            **creds,
        )

    # ── simple completion ──────────────────────────────────────────────────────
    def complete(self, system: str, user: str) -> str:
        resp = self._client.converse(
            modelId=self.model_id,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            inferenceConfig={
                "maxTokens": self.max_tokens,
                "temperature": self.temperature,
                "topP": self.top_p,
            },
        )
        return resp["output"]["message"]["content"][0]["text"]

    def complete_json(self, system: str, user: str) -> Any:
        return _extract_json(self.complete(system, user))

    # ── agentic tool loop ──────────────────────────────────────────────────────
    def converse_with_tools(
        self,
        system: str,
        messages: List[Dict],
        tools: List[Dict],
    ) -> List[Dict]:
        """Single converse call with Bedrock tool_use support.

        Returns the content block list from the assistant turn.
        Each block is a dict: {"type": "text", "text": "..."} or
        {"type": "tool_use", "id": "...", "name": "...", "input": {...}}.
        """
        # Convert our tool schema to Bedrock format
        bedrock_tools = []
        for t in tools:
            bedrock_tools.append({
                "toolSpec": {
                    "name": t["name"],
                    "description": t["description"],
                    "inputSchema": {"json": t["input_schema"]},
                }
            })

        kwargs = dict(
            modelId=self.model_id,
            system=[{"text": system}],
            messages=messages,
            inferenceConfig={
                "maxTokens": self.max_tokens,
                "temperature": self.temperature,
                "topP": self.top_p,
            },
        )
        if bedrock_tools:
            kwargs["toolConfig"] = {"tools": bedrock_tools}

        resp = self._client.converse(**kwargs)
        content = resp["output"]["message"]["content"]

        # normalise to list of dicts with "type"
        result: List[Dict] = []
        for block in content:
            if "text" in block:
                result.append({"type": "text", "text": block["text"]})
            elif "toolUse" in block:
                tu = block["toolUse"]
                result.append({
                    "type": "tool_use",
                    "id": tu.get("toolUseId", ""),
                    "name": tu.get("name", ""),
                    "input": tu.get("input", {}),
                })
        return result


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
