"""Azure OpenAI as Stagehand's model, signed in with Microsoft Entra ID instead of an API key.

Stagehand 4.4's model settings only accept OpenAI, Anthropic, Google, Groq and Cerebras model names and take no
endpoint, so an Azure deployment is reached through Stagehand's LLM callback instead: Stagehand hands every model
request to `AzureLLM`, which sends it to the deployment as a chat completion and returns the reply in Stagehand's shape.

Carried over from the legacy `audit/azure.py` unchanged except for its configuration, which now comes from
`checkout_navigator.config.AzureConfig` (the same AZURE_OPENAI_* environment variables). Needs the Azure extras
(`pip install -r checkout_navigator/requirements-azure.txt`); the imports are deferred so a Gemini-only install
still works.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from typing import Any, Protocol

from checkout_navigator.config import AzureConfig, ConfigError

log = logging.getLogger(__name__)

SCOPE = "https://cognitiveservices.azure.com/.default"
REFRESH_MARGIN_SECONDS = 300  # fetch a new token this long before the current one expires
FENCED_JSON = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)
MAX_ATTEMPTS = 4  # one call plus three retries on a throttle (429), a server error or a dropped connection
MAX_WAIT_SECONDS = 60.0
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
RETRY_ERRORS = {"APIConnectionError", "APITimeoutError"}  # by name: openai is an optional import
LOW_QUOTA_SHARE = 0.1  # report when less than this share of the per-minute token or request limit is left
MAX_REPORTED_ERRORS = 5  # schema errors quoted back to the model when it is asked again
REASK_HINT = (
    "Reply again with JSON that matches the schema exactly: no keys the schema does not list, booleans must be "
    "true or false (never null unless the schema allows null), and every elementId must be copied exactly as shown "
    "in square brackets in the accessibility tree, frame number included, like 0-18372."
)


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


def drop_unknown_keys(value: Any, schema: Any, root: dict[str, Any] | None = None) -> Any:
    """Removes keys an object schema forbids (`additionalProperties: false`), at any depth; changes `value` in place.

    Without strict mode gpt-4o sometimes adds a key of its own (an "explanation" next to observe's elements), and
    Stagehand's check rejects the whole answer for it. Only keys the schema rules out are removed: where extra keys
    are allowed (Stagehand's extract schemas send `additionalProperties: {}`) they are kept. Nothing is invented.
    """
    root = root if root is not None else (schema if isinstance(schema, dict) else {})
    variants = _variants(schema, root)
    if isinstance(value, dict):
        closed = [v for v in variants if v.get("type") in ("object", None) and v.get("additionalProperties") is False]
        if closed:
            allowed = set().union(*((v.get("properties") or {}).keys() for v in closed))
            for key in [k for k in value if k not in allowed]:
                del value[key]
        for variant in variants:
            for key, child in (variant.get("properties") or {}).items():
                if key in value:
                    drop_unknown_keys(value[key], child, root)
    elif isinstance(value, list):
        for variant in variants:
            items = variant.get("items")
            if isinstance(items, dict):
                for element in value:
                    drop_unknown_keys(element, items, root)
    return value


def schema_errors(value: Any, schema: Any) -> list[str]:
    """What is wrong with `value` against `schema`, one line per error ('elements/2/elementId: ...'); [] if it fits.

    Uses jsonschema (part of the azure extra). Without it nothing is checked here and Stagehand's own check decides.
    """
    if not isinstance(schema, dict):
        return []
    try:
        import jsonschema  # noqa: PLC0415 - optional extra
    except ImportError:
        log.debug("jsonschema is not installed: the model's answers are not checked before Stagehand sees them")
        return []
    validator_class = jsonschema.validators.validator_for(schema, default=jsonschema.Draft202012Validator)
    try:
        validator = validator_class(schema)
        errors = sorted(validator.iter_errors(value), key=lambda e: list(e.absolute_path))
    except jsonschema.exceptions.SchemaError as error:  # a schema we cannot read is left to Stagehand
        log.debug("Could not check the answer against its schema: %s", error.message)
        return []
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}" for e in errors]


def structured_answer(text: str, schema: Any) -> Any:
    """The model's JSON answer with the safe repairs made: left-out nullable keys as null, forbidden keys removed."""
    answer = parse_json(text)
    if schema is not None:
        fill_missing_nulls(answer, schema)
        drop_unknown_keys(answer, schema)
    return answer


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


def reply_text(response: Any) -> str:
    """The text of a chat completion's first choice; raises if the model refused."""
    choice = response.choices[0]
    if getattr(choice.message, "refusal", None):
        raise ValueError(f"Azure OpenAI refused the request: {choice.message.refusal}")
    return str(choice.message.content or "")


def add_usage(result: dict[str, Any], earlier: Any) -> dict[str, Any]:
    """`result` with an earlier call's tokens added in, so a re-asked answer is billed for both calls."""
    first = _usage(earlier)
    if not first:
        return result
    usage = dict(result.get("usage") or {})
    for key, tokens in first.items():
        usage[key] = usage.get(key, 0) + tokens
    return {**result, "usage": usage}


def stagehand_result(response: Any, structured: bool, schema: Any = None) -> dict[str, Any]:
    """A chat completion as Stagehand's LLM result; a structured answer gets the safe repairs (see above)."""
    choice = response.choices[0]
    text = reply_text(response)
    result: dict[str, Any] = {"role": "assistant", "content": {"type": "text", "text": text}}
    if choice.finish_reason:
        result["stop_reason"] = choice.finish_reason
    usage = _usage(response.usage)
    if usage:
        result["usage"] = usage
    if structured:
        return {**result, "output_format": "json_schema", "structured_content": structured_answer(text, schema)}
    return {**result, "output_format": "text"}


def _header(headers: Any, name: str) -> str | None:
    value = headers.get(name) if headers is not None else None
    return str(value) if value is not None else None


def _int_header(headers: Any, name: str) -> int | None:
    value = _header(headers, name)
    try:
        return int(float(value)) if value is not None else None
    except ValueError:
        return None


def quota(headers: Any) -> str:
    """What the deployment's rate-limit headers say is left this minute, e.g. 'tokens 1,200/30,000, requests 4/50'."""
    parts = []
    for kind in ("tokens", "requests"):
        left = _int_header(headers, f"x-ratelimit-remaining-{kind}")
        limit = _int_header(headers, f"x-ratelimit-limit-{kind}")
        if left is not None:
            parts.append(f"{kind} {left:,}/{limit:,}" if limit else f"{kind} {left:,}")
    return f"left this minute: {', '.join(parts)}" if parts else "no rate-limit headers"


def quota_is_low(headers: Any) -> bool:
    for kind in ("tokens", "requests"):
        left = _int_header(headers, f"x-ratelimit-remaining-{kind}")
        limit = _int_header(headers, f"x-ratelimit-limit-{kind}")
        if left is not None and limit and left < limit * LOW_QUOTA_SHARE:
            return True
    return False


def retry_after(headers: Any) -> float | None:
    """How long the endpoint asked us to wait, in seconds (Azure sends retry-after-ms and/or retry-after)."""
    milliseconds = _header(headers, "retry-after-ms")
    seconds = _header(headers, "retry-after")
    try:
        if milliseconds is not None:
            return float(milliseconds) / 1000
        if seconds is not None:
            return float(seconds)
    except ValueError:
        pass
    return None


def endpoint_message(error: BaseException) -> str:
    """The endpoint's own error text (Azure: body.error.message or body.message), else the exception's."""
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        nested = body.get("error")
        inner: dict[str, Any] = nested if isinstance(nested, dict) else body
        code, message = inner.get("code"), inner.get("message")
        if message:
            return f"[{code}] {message}" if code else str(message)
    return str(error) or type(error).__name__


class AzureLLM:
    """The Stagehand LLM callback: one chat completion on the Azure deployment per Stagehand model request.

    Throttles (429), server errors and dropped connections are retried here rather than inside the OpenAI client
    (which retries silently), so each one is printed with the endpoint's message and the quota it reports.

    A structured answer is checked against the request's schema before Stagehand sees it (Stagehand's own check
    ends the step on any mismatch). After the safe repairs, one that still does not fit is sent back to the model
    once, with the errors; the second answer is returned whatever it is, and Stagehand's check has the last word.
    """

    def __init__(self, client: Any, deployment: str, sleep: Any = asyncio.sleep) -> None:
        self.client = client
        self.deployment = deployment
        self._sleep = sleep
        self.calls = 0

    async def _complete(self, messages: list[dict[str, Any]], options: dict[str, Any]) -> Any:
        self.calls += 1
        call = self.calls
        for attempt in range(1, MAX_ATTEMPTS + 1):
            started = time.monotonic()
            try:
                raw = await self.client.chat.completions.with_raw_response.create(
                    model=self.deployment, messages=messages, **options
                )
            except asyncio.CancelledError:
                log.warning(
                    f"  Azure OpenAI call {call} abandoned after {time.monotonic() - started:.1f}s "
                    f"(attempt {attempt}/{MAX_ATTEMPTS}): the caller timed out or was cancelled"
                )
                raise
            except Exception as error:
                status = getattr(error, "status_code", None)
                headers = getattr(getattr(error, "response", None), "headers", None)
                retryable = status in RETRY_STATUSES or type(error).__name__ in RETRY_ERRORS
                request_id = _header(headers, "apim-request-id") or _header(headers, "x-request-id")
                detail = (
                    f"Azure OpenAI call {call} failed after {time.monotonic() - started:.1f}s "
                    f"(attempt {attempt}/{MAX_ATTEMPTS}): {status or type(error).__name__} {endpoint_message(error)}"
                    f" | {quota(headers)}" + (f" | request id {request_id}" if request_id else "")
                )
                if not retryable or attempt == MAX_ATTEMPTS:
                    log.warning(f"  {detail} | giving up")
                    raise
                wait = min(retry_after(headers) or 2.0**attempt, MAX_WAIT_SECONDS)
                log.warning(f"  {detail} | retrying in {wait:.1f}s")
                await self._sleep(wait)
                continue
            response = raw.parse()
            headers = raw.headers
            usage = getattr(response, "usage", None)
            tokens = f"{usage.total_tokens:,} tokens" if usage is not None else "tokens unknown"
            summary = f"Azure OpenAI call {call}: {time.monotonic() - started:.1f}s, {tokens} | {quota(headers)}"
            if quota_is_low(headers):
                log.warning(f"  {summary} (nearly out)")
            else:
                log.debug(f"  {summary}")
            return response
        raise AssertionError("unreachable")

    async def __call__(self, params: Any) -> dict[str, Any]:
        request = as_dict(params)
        options = completion_options(request)
        messages = chat_messages(request)
        response = await self._complete(messages, options)
        format_ = options.get("response_format")
        if format_ is None:
            return stagehand_result(response, structured=False)
        schema = format_["json_schema"]["schema"]
        text = reply_text(response)
        errors = schema_errors(structured_answer(text, schema), schema)
        if not errors:
            return stagehand_result(response, structured=True, schema=schema)
        name = format_["json_schema"].get("name", "answer")
        log.warning(
            f"  Azure OpenAI call {self.calls}: the {name} answer does not fit its schema "
            f"({'; '.join(errors[:MAX_REPORTED_ERRORS])}); asking once more"
        )
        again = [
            *messages,
            {"role": "assistant", "content": text},
            {
                "role": "user",
                "content": "Your JSON did not match the schema: "
                f"{'; '.join(errors[:MAX_REPORTED_ERRORS])}. {REASK_HINT}",
            },
        ]
        retried = await self._complete(again, options)
        result = add_usage(stagehand_result(retried, structured=True, schema=schema), response.usage)
        if still := schema_errors(result["structured_content"], schema):
            log.warning(f"  Azure OpenAI call {self.calls}: the second {name} answer does not fit either ({still[0]})")
        return result


@contextlib.asynccontextmanager
async def open_azure_llm(azure: AzureConfig) -> AsyncIterator[AzureLLM]:
    """The Azure callback for one session. Gets a token up front, so a sign-in problem stops the run straight away."""
    try:
        from azure.core.exceptions import ClientAuthenticationError  # noqa: PLC0415 - optional extra
        from azure.identity import DefaultAzureCredential  # noqa: PLC0415 - optional extra
        from openai import AsyncAzureOpenAI  # noqa: PLC0415 - optional extra
    except ImportError as error:
        raise ConfigError(
            f"Azure OpenAI needs its extras: pip install -r checkout_navigator/requirements-azure.txt ({error})"
        ) from error

    endpoint, deployment = azure.endpoint, azure.deployment
    credential = DefaultAzureCredential(exclude_managed_identity_credential=not azure.managed_identity)
    token = EntraToken(credential)
    client = AsyncAzureOpenAI(
        api_version=azure.api_version,
        azure_endpoint=endpoint,
        azure_ad_token_provider=token,
        max_retries=0,  # AzureLLM retries itself, so that throttling is reported rather than silent
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
