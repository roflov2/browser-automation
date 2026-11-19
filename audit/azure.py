"""Azure OpenAI as Stagehand's model, signed in with Microsoft Entra ID instead of an API key.

Stagehand 4.4's model settings only accept OpenAI, Anthropic, Google, Groq and Cerebras model names and take no
endpoint, so an Azure deployment is reached through Stagehand's LLM callback instead: Stagehand hands every model
request to `AzureLLM`, which sends it to the deployment as a chat completion and returns the reply in Stagehand's shape.

Needs the `azure` extra (`pip install -e ".[azure]"`); the imports are deferred so a Gemini-only install still works.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections.abc import AsyncIterator
from typing import Any, Protocol

from .config import ConfigError, Settings

SCOPE = "https://cognitiveservices.azure.com/.default"
REFRESH_MARGIN_SECONDS = 300  # fetch a new token this long before the current one expires
FENCED_JSON = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


class UnsupportedRequest(ValueError):
    """Stagehand asked for something this adapter does not translate (tool calls: only agent mode uses them)."""


class TokenCredential(Protocol):
    """The part of an azure-identity credential used here."""

    def get_token(self, *scopes: str) -> Any: ...


class EntraToken:
    """An async bearer-token provider for the OpenAI client, cached until shortly before it expires.

    The credential is the synchronous azure-identity one (as in the usual `get_bearer_token_provider` recipe); its
    blocking `get_token` runs in a worker thread so it never stalls Stagehand's event loop.
    """

    def __init__(self, credential: TokenCredential, scope: str = SCOPE) -> None:
        self._credential = credential
        self._scope = scope
        self._token: str | None = None
        self._expires_on = 0.0
        self._lock = asyncio.Lock()

    async def __call__(self) -> str:
        async with self._lock:
            if self._token is None or time.time() > self._expires_on - REFRESH_MARGIN_SECONDS:
                access = await asyncio.to_thread(self._credential.get_token, self._scope)
                self._token, self._expires_on = access.token, float(access.expires_on)
            return self._token


def as_dict(params: Any) -> dict[str, Any]:
    """Stagehand passes its request as a pydantic model; tests may pass a plain dict."""
    if isinstance(params, dict):
        return params
    dumped: dict[str, Any] = params.model_dump(mode="json", by_alias=True, exclude_none=True)
    return dumped


def _blocks(content: Any) -> list[dict[str, Any]]:
    return content if isinstance(content, list) else [content]


def _user_part(block: dict[str, Any]) -> dict[str, Any]:
    if block["type"] == "text":
        return {"type": "text", "text": block["text"]}
    if block["type"] == "image":
        return {"type": "image_url", "image_url": {"url": f"data:{block['mime_type']};base64,{block['data']}"}}
    raise UnsupportedRequest(f"Azure adapter does not translate {block['type']!r} content (Stagehand agent mode)")


def _assistant_text(block: dict[str, Any]) -> str:
    if block["type"] == "text":
        return str(block["text"])
    raise UnsupportedRequest(f"Azure adapter does not translate assistant {block['type']!r} content")


def chat_messages(request: dict[str, Any]) -> list[dict[str, Any]]:
    """Stagehand's system prompt and messages as Chat Completions messages."""
    messages: list[dict[str, Any]] = []
    if request.get("system_prompt"):
        messages.append({"role": "system", "content": request["system_prompt"]})
    for message in request["messages"]:
        blocks = _blocks(message["content"])
        if message["role"] == "assistant":
            messages.append({"role": "assistant", "content": "".join(_assistant_text(b) for b in blocks)})
        else:
            messages.append({"role": "user", "content": [_user_part(b) for b in blocks]})
    return messages


def completion_options(request: dict[str, Any]) -> dict[str, Any]:
    """Everything but the messages: sampling settings and, for extract/observe/act, the JSON schema to answer in."""
    if request.get("tools"):
        raise UnsupportedRequest("Azure adapter does not translate tool calls (Stagehand agent mode)")
    options: dict[str, Any] = {}
    if request.get("temperature") is not None:
        options["temperature"] = request["temperature"]
    if request.get("stop_sequences"):
        options["stop"] = request["stop_sequences"]
    response_format = request.get("response_format") or {"type": "text"}
    if response_format["type"] == "json_schema":
        schema = response_format.get("schema", response_format.get("schema_"))
        json_schema: dict[str, Any] = {"name": response_format["name"], "schema": schema, "strict": False}
        if response_format.get("description"):
            json_schema["description"] = response_format["description"]
        # Not strict: OpenAI's strict mode rejects schemas that leave out additionalProperties or optional fields.
        options["response_format"] = {"type": "json_schema", "json_schema": json_schema}
    return options


def parse_json(text: str) -> Any:
    """The structured answer; tolerates a ```json fence, which models sometimes add despite the response format."""
    fenced = FENCED_JSON.match(text)
    try:
        return json.loads(fenced.group(1) if fenced else text)
    except json.JSONDecodeError as error:
        raise ValueError(f"Azure OpenAI did not return valid JSON: {text[:200]!r}") from error


def _resolve(schema: Any, root: dict[str, Any]) -> Any:
    """Follows a local `$ref` (`#/$defs/...` or `#/definitions/...`); other schemas come back as they are."""
    seen = 0
    while isinstance(schema, dict) and isinstance(schema.get("$ref"), str) and schema["$ref"].startswith("#/"):
        target: Any = root
        for part in schema["$ref"][2:].split("/"):
            target = target.get(part) if isinstance(target, dict) else None
        if target is None or (seen := seen + 1) > 32:
            return schema
        schema = target
    return schema


def _variants(schema: Any, root: dict[str, Any]) -> list[dict[str, Any]]:
    """The schema and, through anyOf/oneOf/allOf, every alternative it allows (refs resolved)."""
    schema = _resolve(schema, root)
    if not isinstance(schema, dict):
        return []
    found = [schema]
    for key in ("anyOf", "oneOf", "allOf"):
        for option in schema.get(key) or []:
            found.extend(_variants(option, root))
    return found


def _allows_null(schema: Any, root: dict[str, Any]) -> bool:
    for variant in _variants(schema, root):
        kind = variant.get("type")
        if kind == "null" or (isinstance(kind, list) and "null" in kind) or variant.get("nullable") is True:
            return True
        if "const" in variant and variant["const"] is None:
            return True
        if None in (variant.get("enum") or []):
            return True
    return False


def fill_missing_nulls(value: Any, schema: Any, root: dict[str, Any] | None = None) -> Any:
    """Sets every required-but-nullable key the model left out to null, at any depth; changes `value` in place.

    The schema is sent without strict mode, so the model may drop a key it has nothing for instead of answering null,
    and Stagehand's check of the answer then fails on `undefined`. Keys that cannot be null are left alone, so a real
    gap still fails that check.
    """
    root = root if root is not None else (schema if isinstance(schema, dict) else {})
    for variant in _variants(schema, root):
        if isinstance(value, dict):
            properties = variant.get("properties") or {}
            for key in variant.get("required") or []:
                if key not in value and key in properties and _allows_null(properties[key], root):
                    value[key] = None
            for key, child in properties.items():
                if key in value:
                    fill_missing_nulls(value[key], child, root)
        elif isinstance(value, list):
            items = variant.get("items")
            if isinstance(items, list):  # tuple form
                for element, child in zip(value, items, strict=False):
                    fill_missing_nulls(element, child, root)
            elif items is not None:
                for element in value:
                    fill_missing_nulls(element, items, root)
    return value


def _usage(usage: Any) -> dict[str, int] | None:
    if usage is None:
        return None
    result = {
        "input_tokens": usage.prompt_tokens,
        "output_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
    }
    cached = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", None)
    if cached:
        result["cached_input_tokens"] = cached
    reasoning = getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None)
    if reasoning:
        result["reasoning_tokens"] = reasoning
    return result


def stagehand_result(response: Any, structured: bool, schema: Any = None) -> dict[str, Any]:
    """A chat completion as Stagehand's LLM result; a structured answer gets the nulls it left out (see above)."""
    choice = response.choices[0]
    if getattr(choice.message, "refusal", None):
        raise ValueError(f"Azure OpenAI refused the request: {choice.message.refusal}")
    text = choice.message.content or ""
    result: dict[str, Any] = {"role": "assistant", "content": {"type": "text", "text": text}}
    if choice.finish_reason:
        result["stop_reason"] = choice.finish_reason
    usage = _usage(response.usage)
    if usage:
        result["usage"] = usage
    if structured:
        answer = parse_json(text)
        if schema is not None:
            fill_missing_nulls(answer, schema)
        return {**result, "output_format": "json_schema", "structured_content": answer}
    return {**result, "output_format": "text"}


class AzureLLM:
    """The Stagehand LLM callback: one chat completion on the Azure deployment per Stagehand model request."""

    def __init__(self, client: Any, deployment: str) -> None:
        self.client = client
        self.deployment = deployment

    async def __call__(self, params: Any) -> dict[str, Any]:
        request = as_dict(params)
        options = completion_options(request)
        response = await self.client.chat.completions.create(
            model=self.deployment, messages=chat_messages(request), **options
        )
        format_ = options.get("response_format")
        schema = format_["json_schema"]["schema"] if format_ else None
        return stagehand_result(response, structured=format_ is not None, schema=schema)


@contextlib.asynccontextmanager
async def open_azure_llm(settings: Settings) -> AsyncIterator[AzureLLM]:
    """The Azure callback for one session. Gets a token up front, so a sign-in problem stops the run straight away."""
    try:
        from azure.core.exceptions import ClientAuthenticationError  # noqa: PLC0415 - optional extra
        from azure.identity import DefaultAzureCredential  # noqa: PLC0415 - optional extra
        from openai import AsyncAzureOpenAI  # noqa: PLC0415 - optional extra
    except ImportError as error:
        raise ConfigError(f'Azure OpenAI needs the azure extra: pip install -e ".[azure]" ({error})') from error

    endpoint, deployment = settings.require_azure()
    credential = DefaultAzureCredential(exclude_managed_identity_credential=not settings.azure_openai_managed_identity)
    token = EntraToken(credential)
    client = AsyncAzureOpenAI(
        api_version=settings.azure_openai_api_version, azure_endpoint=endpoint, azure_ad_token_provider=token
    )
    try:
        try:
            await token()
        except ClientAuthenticationError as error:
            raise ConfigError(
                "Could not get a Microsoft Entra ID token for Azure OpenAI. Sign in with `az login`, or set "
                f"AZURE_CLIENT_ID, AZURE_TENANT_ID and AZURE_CLIENT_SECRET for a service principal.\n{error}"
            ) from error
        yield AzureLLM(client, deployment)
    finally:
        await client.close()
        credential.close()
