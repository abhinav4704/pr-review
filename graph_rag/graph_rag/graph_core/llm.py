"""Provider-agnostic LLM wrapper for the semantic layer.

One job: given a system prompt, a user prompt, and a JSON schema, return a dict
that validates against that schema. Every supported provider is driven through
its native *structured-output* mode so the result is always schema-shaped — the
"contract first, not prompt first" rule from SEMANTIC_LAYER.md.

Providers (pick with --provider / GRAPH_RAG_LLM_PROVIDER):
  anthropic — Claude via the Anthropic API             (ANTHROPIC_API_KEY)
  bedrock   — Any Bedrock model via boto3 Converse API  (AWS creds + AWS_REGION)
              Model prefix determines the call path:
                anthropic.*  → Anthropic Messages API via AnthropicBedrockMantle
                amazon.*     → boto3 converse + toolUse (Nova Pro, Nova Lite, etc.)
  openai    — GPT via the OpenAI API                   (OPENAI_API_KEY)

SDKs are imported lazily so the rest of the pipeline keeps working without any
of them installed; the import error only surfaces when the semantic pass runs.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

PROVIDERS = ("anthropic", "bedrock", "openai")

DEFAULT_PROVIDER = os.environ.get("GRAPH_RAG_LLM_PROVIDER", "anthropic")

# Per-provider default model. Override with --model / GRAPH_RAG_LLM_MODEL for
# tiering (cheap model for routine functions, strong one for class/repo synthesis).
_DEFAULT_MODELS = {
    "anthropic": "claude-opus-4-8",
    "bedrock": "amazon.nova-pro-v1:0",
    "openai": "gpt-4o",
}


def default_model(provider: str) -> str:
    return os.environ.get("GRAPH_RAG_LLM_MODEL") or _DEFAULT_MODELS.get(provider, "")


# How many times to retry a Bedrock Converse call that fails with
# ModelErrorException ("invalid sequence as part of ToolUse") before giving up.
_TOOLUSE_RETRIES = 2


@dataclass
class SemanticLLM:
    provider: str = DEFAULT_PROVIDER
    model: str = ""
    max_tokens: int = 1024
    temperature: float | None = None  # only sent to providers that accept it (openai)
    _client: object = None

    def __post_init__(self):
        if self.provider not in PROVIDERS:
            raise ValueError(f"unknown provider {self.provider!r}; valid: {list(PROVIDERS)}")
        if not self.model:
            self.model = default_model(self.provider)

    # --- public API -----------------------------------------------------------
    def extract(self, system: str, user: str, schema: dict) -> dict:
        """Return JSON matching `schema`. Raises on transport/parse failure."""
        if self.provider == "openai":
            return self._openai_extract(system, user, schema)
        if self.provider == "bedrock" and self.model.startswith("amazon."):
            return self._boto3_extract(system, user, schema)
        return self._anthropic_extract(system, user, schema)  # anthropic + bedrock claude

    # --- boto3 native Bedrock Converse (amazon.* models: Nova Pro/Lite/Micro) ---
    def _boto3_extract(self, system: str, user: str, schema: dict) -> dict:
        """Use the Bedrock Converse API with toolUse to get structured output.
        Works for amazon.nova-pro-v1:0, amazon.nova-lite-v1:0, etc.

        Retries on `ModelErrorException` ("invalid sequence as part of
        ToolUse") — a known transient Nova quirk where the model occasionally
        emits a malformed tool-call; a retry with the same input usually
        succeeds. Not a bug in our schema/prompt, so we don't change either,
        just re-ask up to `_TOOLUSE_RETRIES` times before giving up."""
        try:
            import boto3  # noqa: PLC0415
            from botocore.exceptions import ClientError  # noqa: PLC0415
        except ImportError as e:
            raise RuntimeError("bedrock (amazon.*) provider needs: pip install boto3") from e
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        if not region:
            raise RuntimeError("set AWS_REGION for the bedrock provider")
        client = boto3.client("bedrock-runtime", region_name=region)
        kwargs = dict(
            modelId=self.model,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            toolConfig={
                "tools": [{
                    "toolSpec": {
                        "name": "extract",
                        "description": "Extract structured semantic data from code.",
                        "inputSchema": {"json": schema},
                    }
                }],
                "toolChoice": {"tool": {"name": "extract"}},
            },
            inferenceConfig={"maxTokens": self.max_tokens},
        )
        last_exc: Exception | None = None
        for attempt in range(_TOOLUSE_RETRIES + 1):
            try:
                response = client.converse(**kwargs)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code == "ModelErrorException" and attempt < _TOOLUSE_RETRIES:
                    last_exc = exc
                    continue  # transient malformed-tool-use, same input, just retry
                raise
            for block in response.get("output", {}).get("message", {}).get("content", []):
                if block.get("toolUse"):
                    return block["toolUse"]["input"]
            last_exc = RuntimeError(f"boto3 converse returned no toolUse block: {response}")
        raise last_exc

    # --- Anthropic / Bedrock Claude (Messages + structured-output surface) -----
    def _anthropic_client(self):
        if self._client is not None:
            return self._client
        if self.provider == "bedrock":
            try:
                from anthropic import AnthropicBedrockMantle  # noqa: PLC0415
            except ImportError as e:  # pragma: no cover
                raise RuntimeError(
                    "bedrock provider needs: pip install 'anthropic[bedrock]'"
                ) from e
            region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
            if not region:
                raise RuntimeError("set AWS_REGION for the bedrock provider")
            self._client = AnthropicBedrockMantle(aws_region=region)
        else:
            try:
                import anthropic  # noqa: PLC0415
            except ImportError as e:  # pragma: no cover
                raise RuntimeError("anthropic provider needs: pip install anthropic") from e
            if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
                raise RuntimeError("set ANTHROPIC_API_KEY for the anthropic provider")
            self._client = anthropic.Anthropic()
        return self._client

    def _anthropic_extract(self, system: str, user: str, schema: dict) -> dict:
        resp = self._anthropic_client().messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        return json.loads(text)

    # --- OpenAI ----------------------------------------------------------------
    def _openai_client(self):
        if self._client is not None:
            return self._client
        try:
            from openai import OpenAI  # noqa: PLC0415
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("openai provider needs: pip install openai") from e
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("set OPENAI_API_KEY for the openai provider")
        self._client = OpenAI()
        return self._client

    def _openai_extract(self, system: str, user: str, schema: dict) -> dict:
        kwargs = {}
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        resp = self._openai_client().chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "semantic", "schema": schema, "strict": True},
            },
            **kwargs,
        )
        return json.loads(resp.choices[0].message.content)
