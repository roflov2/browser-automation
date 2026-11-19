"""Azure OpenAI as Stagehand's model: request/reply translation, token caching, settings, and a real Stagehand session.

No Azure call is made: a fake client stands in for AsyncAzureOpenAI and a fake credential for azure-identity.
"""

import asyncio
import inspect
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from stagehand._generated import models as wire

from audit.azure import AzureLLM, EntraToken, UnsupportedRequest, chat_messages, completion_options, open_azure_llm
from audit.config import ConfigError, Settings

from .browser_support import StubModel, find_chrome, launch_args, serve

ENDPOINT = "https://example-oai.openai.azure.com/"
SCHEMA = {"type": "object", "properties": {"total": {"type": "string"}}, "required": ["total"]}
STRUCTURED = {
    "system_prompt": "You read checkout pages.",
    "temperature": 0.1,
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "Read the total"},
                                     {"type": "image", "data": "aGVsbG8=", "mime_type": "image/png"}]},
        {"role": "assistant", "content": {"type": "text", "text": "Looking."}},
        {"role": "user", "content": {"type": "text", "text": "Go on"}},
    ],
    "response_format": {"type": "json_schema", "name": "Total", "schema": SCHEMA},
}  # fmt: skip


def completion(content: str, finish_reason: str = "stop", refusal: str | None = None) -> SimpleNamespace:
    usage = SimpleNamespace(
        prompt_tokens=120, completion_tokens=8, total_tokens=128,
        prompt_tokens_details=SimpleNamespace(cached_tokens=64), completion_tokens_details=None,
    )  # fmt: skip
    message = SimpleNamespace(content=content, refusal=refusal)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish_reason)], usage=usage)


class FakeAzure:
    """Stands in for AsyncAzureOpenAI: records each chat.completions.create call and returns `answer(kwargs)`."""

    def __init__(self, answer) -> None:
        self.calls: list[dict] = []
        self.closed = False
        self._answer = answer
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        answer = self._answer(kwargs)
        return await answer if inspect.isawaitable(answer) else answer

    async def close(self) -> None:
        self.closed = True


def test_structured_request_is_translated_to_chat_completions():
    request = wire.LLMStructuredGenerateParams.model_validate(STRUCTURED)  # what Stagehand actually passes
    client = FakeAzure(lambda _: completion('{"total": "$44.00"}'))

    result = asyncio.run(AzureLLM(client, "gpt-4o")(request))

    (call,) = client.calls
    assert call["model"] == "gpt-4o"
    assert call["temperature"] == 0.1
    assert call["messages"] == [
        {"role": "system", "content": "You read checkout pages."},
        {"role": "user", "content": [{"type": "text", "text": "Read the total"},
                                     {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}}]},
        {"role": "assistant", "content": "Looking."},
        {"role": "user", "content": [{"type": "text", "text": "Go on"}]},
    ]  # fmt: skip
    assert call["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "Total", "schema": SCHEMA, "strict": False},
    }
    assert result["structured_content"] == {"total": "$44.00"}
    assert result["usage"] == {"input_tokens": 120, "output_tokens": 8, "total_tokens": 128, "cached_input_tokens": 64}
    wire.LLMStructuredGenerateResult.model_validate(result)  # Stagehand accepts the reply


def test_text_request_gets_a_text_reply():
    request = {
        "messages": [{"role": "user", "content": {"type": "text", "text": "hi"}}],
        "response_format": {"type": "text"},
    }
    client = FakeAzure(lambda _: completion("hello"))

    result = asyncio.run(AzureLLM(client, "gpt-4o")(request))

    assert "response_format" not in client.calls[0]
    assert "temperature" not in client.calls[0]  # unset settings are left to the deployment's defaults
    assert result["output_format"] == "text"
    assert result["content"] == {"type": "text", "text": "hello"}
    wire.LLMMessageGenerateResult.model_validate(result)


def test_fenced_json_is_accepted_and_bad_json_or_refusal_fails():
    fenced = FakeAzure(lambda _: completion('```json\n{"total": "$1"}\n```'))
    assert asyncio.run(AzureLLM(fenced, "d")(STRUCTURED))["structured_content"] == {"total": "$1"}

    with pytest.raises(ValueError, match="valid JSON"):
        asyncio.run(AzureLLM(FakeAzure(lambda _: completion("not json")), "d")(STRUCTURED))
    with pytest.raises(ValueError, match="refused"):
        asyncio.run(AzureLLM(FakeAzure(lambda _: completion("", refusal="no")), "d")(STRUCTURED))


def test_tool_calls_are_refused_clearly():
    tool = {"name": "click", "input_schema": {"type": "object"}}
    with pytest.raises(UnsupportedRequest, match="tool calls"):
        completion_options({"messages": [], "tools": [tool]})
    tool_use = {"type": "tool_use", "id": "1", "name": "click", "input": {}}
    with pytest.raises(UnsupportedRequest):
        chat_messages({"messages": [{"role": "assistant", "content": tool_use}]})


class FakeCredential:
    def __init__(self, lifetime: float = 3600, fail: Exception | None = None) -> None:
        self.requests: list[tuple[str, ...]] = []
        self.lifetime = lifetime
        self.fail = fail
        self.closed = False

    def get_token(self, *scopes: str):
        if self.fail:
            raise self.fail
        self.requests.append(scopes)
        return SimpleNamespace(token=f"token-{len(self.requests)}", expires_on=time.time() + self.lifetime)

    def close(self) -> None:
        self.closed = True


def test_token_is_cached_until_it_nears_expiry():
    lasting, expiring = FakeCredential(), FakeCredential(lifetime=60)  # 60s is inside the refresh margin

    async def twice(credential):
        token = EntraToken(credential)
        return [await token(), await token()]

    assert asyncio.run(twice(lasting)) == ["token-1", "token-1"]
    assert lasting.requests == [("https://cognitiveservices.azure.com/.default",)]
    assert asyncio.run(twice(expiring)) == ["token-1", "token-2"]


def settings(**overrides) -> Settings:
    values = {"azure_openai_endpoint": ENDPOINT, "azure_openai_deployment": "gpt-4o", **overrides}
    return Settings(_env_file=None, **values)


def test_azure_settings(monkeypatch):
    monkeypatch.delenv("GOOGLE_GENERATIVE_AI_API_KEY", raising=False)
    chosen = settings()
    assert chosen.use_azure
    assert chosen.azure_openai_api_version == "2024-12-01-preview"
    chosen.require_model()  # no Gemini key needed
    assert not settings(azure_openai_endpoint="", azure_openai_deployment="").use_azure
    with pytest.raises(ValueError, match="AZURE_OPENAI_DEPLOYMENT"):
        settings(azure_openai_deployment=None)
    with pytest.raises(ValueError, match="https"):
        settings(azure_openai_endpoint="http://example-oai.openai.azure.com/")


def patch_azure_sdk(monkeypatch, credential: FakeCredential) -> dict:
    """Swap the real credential and client for fakes; returns what open_azure_llm built them with."""
    import azure.identity
    import openai

    built: dict = {}

    def make_credential(**kwargs):
        built["credential"] = kwargs
        return credential

    def make_client(**kwargs):
        built["client"] = kwargs
        built["fake"] = FakeAzure(lambda _: completion("{}"))
        return built["fake"]

    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", make_credential)
    monkeypatch.setattr(openai, "AsyncAzureOpenAI", make_client)
    return built


def test_open_azure_llm_signs_in_up_front_and_closes(monkeypatch):
    credential = FakeCredential()
    built = patch_azure_sdk(monkeypatch, credential)

    async def session():
        async with open_azure_llm(settings()) as llm:
            assert credential.requests  # the token was fetched before any model call
            return llm.deployment, await built["client"]["azure_ad_token_provider"]()

    assert asyncio.run(session()) == ("gpt-4o", "token-1")
    assert built["credential"] == {"exclude_managed_identity_credential": True}
    assert built["client"]["azure_endpoint"] == ENDPOINT
    assert built["client"]["api_version"] == "2024-12-01-preview"
    assert built["fake"].closed and credential.closed


def test_open_azure_llm_reports_a_failed_sign_in(monkeypatch):
    from azure.core.exceptions import ClientAuthenticationError

    credential = FakeCredential(fail=ClientAuthenticationError("no az login"))
    patch_azure_sdk(monkeypatch, credential)

    async def session():
        async with open_azure_llm(settings(azure_openai_managed_identity=True)):
            pass

    with pytest.raises(ConfigError, match="az login"):
        asyncio.run(session())
    assert credential.closed


class Total(BaseModel):
    total: str | None


@pytest.mark.browser
def test_stagehand_session_runs_on_the_azure_adapter():
    """Stagehand's real extract() and observe() go through AzureLLM and accept its replies."""
    chrome = find_chrome()
    if chrome is None:
        pytest.skip("no Chrome/Chromium found (set CHROME_PATH to run the browser tests)")
    stub = StubModel({"Total": {"total": "$44.00"}})

    async def answer(kwargs):
        # Hand the stub the request as Stagehand would have, and send back its answer as Azure would.
        schema = kwargs.get("response_format", {}).get("json_schema", {}).get("schema", {})
        request = {"messages": kwargs["messages"], "response_format": {"schema": schema}}
        return completion(json.dumps((await stub(request))["structured_content"]))

    async def session(port: int):
        from stagehand import Stagehand, local_browser

        client = FakeAzure(answer)
        browser = await local_browser.launch(headless=True, executable_path=chrome, args=launch_args())
        stagehand = await Stagehand.create(browser=browser, model=AzureLLM(client, "gpt-4o"))
        try:
            page = (await browser.context.pages())[0]
            await page.goto(f"http://127.0.0.1:{port}/index.html")
            extracted = await stagehand.extract("Read the total", Total)
            found = await stagehand.observe("Find the 'Open' button")
        finally:
            await stagehand.close()
        return extracted.data.total, found, client.calls

    with serve(Path(__file__).parent / "fixtures" / "shopify") as port:
        total, found, calls = asyncio.run(session(port))
    assert total == "$44.00"
    assert found  # observe() parsed the adapter's answer into actions
    assert all(call["model"] == "gpt-4o" for call in calls)
