"""Thin LLM provider wrapper: pick Nova (AWS Bedrock) or OpenAI at call time."""

from __future__ import annotations

from typing import Callable


def make_completion_fn(provider: str, **creds) -> Callable[[str, str], str]:
    """Build the client ONCE and return fn(system, user) -> str.

    Use this when you will make many calls (e.g. multi-pass review) so the
    underlying boto3/OpenAI client is not rebuilt every call.
    """
    provider = (provider or "").lower()

    if provider == "nova":
        from .llm import NovaClient
        client = NovaClient(
            model_id=creds.get("model_id") or "us.amazon.nova-pro-v1:0",
            region=creds.get("region") or "us-east-1",
            max_tokens=int(creds.get("max_tokens") or 4096),
            aws_access_key_id=creds.get("aws_access_key_id", ""),
            aws_secret_access_key=creds.get("aws_secret_access_key", ""),
            aws_session_token=creds.get("aws_session_token", ""),
        )
        return lambda system, user: client.complete(system, user)

    if provider == "openai":
        from openai import OpenAI
        api_key = creds.get("api_key", "")
        if not api_key:
            raise RuntimeError("OpenAI API key is required.")
        client = OpenAI(api_key=api_key)
        model = creds.get("model") or "gpt-4o"
        max_tokens = int(creds.get("max_tokens") or 4096)

        def _fn(system: str, user: str) -> str:
            resp = client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                temperature=0.2,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            return resp.choices[0].message.content or ""

        return _fn

    raise ValueError(f"Unknown provider: {provider!r}")


def run_completion(provider: str, system: str, user: str, **creds) -> str:
    """Run a single system+user completion against the chosen provider.

    provider: "nova" or "openai".
    creds (nova):   model_id, region, aws_access_key_id, aws_secret_access_key,
                    aws_session_token, max_tokens
    creds (openai): api_key, model, max_tokens
    """
    provider = (provider or "").lower()

    if provider == "nova":
        from .llm import NovaClient
        client = NovaClient(
            model_id=creds.get("model_id") or "us.amazon.nova-pro-v1:0",
            region=creds.get("region") or "us-east-1",
            max_tokens=int(creds.get("max_tokens") or 4096),
            aws_access_key_id=creds.get("aws_access_key_id", ""),
            aws_secret_access_key=creds.get("aws_secret_access_key", ""),
            aws_session_token=creds.get("aws_session_token", ""),
        )
        return client.complete(system, user)

    if provider == "openai":
        from openai import OpenAI
        api_key = creds.get("api_key", "")
        if not api_key:
            raise RuntimeError("OpenAI API key is required.")
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model=creds.get("model") or "gpt-4o",
            max_tokens=int(creds.get("max_tokens") or 4096),
            temperature=0.2,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return resp.choices[0].message.content or ""

    raise ValueError(f"Unknown provider: {provider!r}")
