"""Contract tests for the OpenAI and Anthropic adapters: the official SDKs run for real, with their HTTP layer replaced
by a mock transport that returns the documented response shapes. No request ever leaves the machine.

This proves the request we build and the way we read answers and errors. It does not prove anything about the live
services: OpenAI's protocol is also exercised live against Groq's OpenAI-compatible endpoint (scripts/live_adapters.py);
Anthropic's is not run against the real API (no key)."""
import json

import httpx
import httpx2
import pytest

from app.llm.providers import AnthropicProvider, FatalError, KeyRejected, OpenAIProvider, RateLimited, TransientError, to_anthropic
from app.llm.types import LLMRequest, ToolSpec

TOOLS = [ToolSpec(name="web_search", description="Search", parameters={"type": "object", "properties": {"query": {"type": "string"}}})]


def openai_body(content="hi", tool_calls=None, finish="stop"):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "gpt-6.1-sol",
            "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}


def openai(handler) -> tuple[OpenAIProvider, list]:
    seen = []

    def h(r):
        seen.append(r)
        return handler(r)
    return OpenAIProvider("sk-test-" + "x" * 30, http_client=httpx.AsyncClient(transport=httpx.MockTransport(h))), seen


def req(**kw):
    return LLMRequest(agent="t", tier="big", messages=[{"role": "system", "content": "Be brief."},
                                                       {"role": "user", "content": "Hello"}], max_tokens=300, **kw)


async def test_openai_text_json_mode_and_the_request_we_send():
    p, seen = openai(lambda r: httpx.Response(200, json=openai_body('{"a": 1}')))
    out = await p.complete("gpt-6.1-sol", req(json_mode=True))
    assert out.content == '{"a": 1}' and (out.prompt_tokens, out.completion_tokens) == (11, 7)
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/v1/chat/completions" and body["model"] == "gpt-6.1-sol"
    assert body["response_format"] == {"type": "json_object"} and body["max_completion_tokens"] == 300
    assert "temperature" not in body and "max_tokens" not in body
    assert seen[0].headers["authorization"].startswith("Bearer sk-test-")


async def test_openai_tool_calls_are_read():
    calls = [{"id": "call_1", "type": "function", "function": {"name": "web_search", "arguments": '{"query": "q"}'}}]
    p, seen = openai(lambda r: httpx.Response(200, json=openai_body(None, calls, "tool_calls")))
    out = await p.complete("gpt-6.1-sol", req(tools=TOOLS))
    assert [(c.id, c.name, json.loads(c.arguments)) for c in out.tool_calls] == [("call_1", "web_search", {"query": "q"})]
    body = json.loads(seen[0].content)
    assert body["tool_choice"] == "auto" and body["tools"][0]["function"]["name"] == "web_search"


async def test_openai_truncated_output_is_returned_for_the_repair_loop():
    p, _ = openai(lambda r: httpx.Response(200, json=openai_body('{"a": [1, 2', finish="length")))
    assert (await p.complete("m", req())).content == '{"a": [1, 2'


@pytest.mark.parametrize("status,body,headers,exc", [
    (401, {"error": {"message": "Incorrect API key provided", "type": "invalid_request_error", "code": "invalid_api_key"}}, {}, KeyRejected),
    (403, {"error": {"message": "forbidden"}}, {}, KeyRejected),
    (429, {"error": {"message": "Rate limit reached", "type": "requests", "code": "rate_limit_exceeded"}}, {"retry-after": "7"}, RateLimited),
    (429, {"error": {"message": "You exceeded your current quota", "type": "insufficient_quota", "code": "insufficient_quota"}}, {}, KeyRejected),
    (500, {"error": {"message": "server error"}}, {}, TransientError),
    (503, {"error": {"message": "overloaded"}}, {}, TransientError),
    (400, {"error": {"message": "bad request"}}, {}, FatalError),
])
async def test_openai_errors_map_to_what_the_gateway_understands(status, body, headers, exc):
    p, _ = openai(lambda r: httpx.Response(status, json=body, headers=headers))
    with pytest.raises(exc) as e:
        await p.complete("m", req())
    if status == 429 and exc is RateLimited:
        assert e.value.retry_after == 7.0
    if exc is KeyRejected and "quota" in json.dumps(body):
        assert e.value.reason == "no_credit"


# ---------------------------------------------------------------- Anthropic
def claude_body(content, stop="end_turn"):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5", "content": content,
            "stop_reason": stop, "stop_sequence": None, "usage": {"input_tokens": 13, "output_tokens": 9}}


def claude(handler) -> tuple[AnthropicProvider, list]:
    seen = []

    def h(r):
        seen.append(r)
        return handler(r)
    return AnthropicProvider("sk-ant-api03-" + "y" * 30, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(h))), seen


async def test_claude_text_with_system_prompt_and_no_sampling_parameters():
    p, seen = claude(lambda r: httpx2.Response(200, json=claude_body(
        [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "text", "text": '{"a": 1}'}])))
    out = await p.complete("claude-sonnet-5-5", req(json_mode=True))
    assert out.content == '{"a": 1}' and (out.prompt_tokens, out.completion_tokens) == (13, 9)   # thinking block ignored
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/v1/messages" and body["model"] == "claude-sonnet-5-5" and body["max_tokens"] == 300
    assert body["system"].startswith("Be brief.") and "JSON object only" in body["system"]
    assert body["messages"] == [{"role": "user", "content": "Hello"}]           # system is not a message
    assert "temperature" not in body and "top_p" not in body
    assert body["thinking"] == {"type": "between_tools"}                         # Sonnet 5.5's thinking-off mode
    assert body["output_config"] == {"effort": "low"}


async def test_claude_haiku_turns_thinking_off_the_supported_way():
    p, seen = claude(lambda r: httpx2.Response(200, json=claude_body([{"type": "text", "text": "0.02"}])))
    await p.complete("claude-haiku-5-5", req())
    assert json.loads(seen[0].content)["thinking"] == {"type": "disabled"}


async def test_claude_tool_use_round_trip():
    p, seen = claude(lambda r: httpx2.Response(200, json=claude_body(
        [{"type": "text", "text": "Searching."}, {"type": "tool_use", "id": "toolu_1", "name": "web_search", "input": {"query": "q"}}],
        stop="tool_use")))
    history = [{"role": "system", "content": "sys"}, {"role": "user", "content": "find"},
               {"role": "assistant", "content": "", "tool_calls": [
                   {"id": "toolu_0", "type": "function", "function": {"name": "web_search", "arguments": '{"query": "a"}'}},
                   {"id": "toolu_9", "type": "function", "function": {"name": "web_search", "arguments": '{"query": "b"}'}}]},
               {"role": "tool", "tool_call_id": "toolu_0", "content": "result a"},
               {"role": "tool", "tool_call_id": "toolu_9", "content": "result b"}]
    out = await p.complete("claude-sonnet-5-5", LLMRequest(agent="t", tier="big", messages=history, tools=TOOLS, max_tokens=400))
    assert [(c.id, c.name, json.loads(c.arguments)) for c in out.tool_calls] == [("toolu_1", "web_search", {"query": "q"})]
    body = json.loads(seen[0].content)
    assert body["tool_choice"] == {"type": "auto"}                               # forced tool choice is refused by the API
    assert body["tools"] == [{"name": "web_search", "description": "Search", "input_schema": TOOLS[0].parameters}]
    assert body["messages"][1] == {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_0", "name": "web_search", "input": {"query": "a"}},
        {"type": "tool_use", "id": "toolu_9", "name": "web_search", "input": {"query": "b"}}]}
    assert body["messages"][2] == {"role": "user", "content": [                  # parallel results in ONE user message
        {"type": "tool_result", "tool_use_id": "toolu_0", "content": "result a"},
        {"type": "tool_result", "tool_use_id": "toolu_9", "content": "result b"}]}


async def test_claude_truncated_output_and_refusal():
    p, _ = claude(lambda r: httpx2.Response(200, json=claude_body([{"type": "text", "text": '{"a": [1'}], stop="max_tokens")))
    assert (await p.complete("claude-haiku-5-5", req())).content == '{"a": [1'
    p, _ = claude(lambda r: httpx2.Response(200, json=claude_body([], stop="refusal")))
    with pytest.raises(FatalError, match="declined"):
        await p.complete("claude-haiku-5-5", req())


def _err(kind, msg="x"):
    return {"type": "error", "error": {"type": kind, "message": msg}}


@pytest.mark.parametrize("status,kind,headers,exc", [
    (401, "authentication_error", {}, KeyRejected), (403, "permission_error", {}, KeyRejected),
    (402, "billing_error", {}, KeyRejected),
    (429, "rate_limit_error", {"retry-after": "12"}, RateLimited),
    (529, "overloaded_error", {}, TransientError), (500, "api_error", {}, TransientError),
    (400, "invalid_request_error", {}, FatalError),
])
async def test_claude_errors_map_to_what_the_gateway_understands(status, kind, headers, exc):
    p, _ = claude(lambda r: httpx2.Response(status, json=_err(kind), headers=headers))
    with pytest.raises(exc) as e:
        await p.complete("claude-haiku-5-5", req())
    if exc is RateLimited:
        assert e.value.retry_after == 12.0
    if status == 402:
        assert e.value.reason == "no_credit"


def test_history_conversion_keeps_plain_turns_and_drops_nothing():
    system, msgs = to_anthropic([{"role": "system", "content": "a"}, {"role": "system", "content": "b"},
                                 {"role": "user", "content": "u"}, {"role": "assistant", "content": "x"},
                                 {"role": "user", "content": ""}])
    assert system == "a\n\nb" and msgs == [{"role": "user", "content": "u"}, {"role": "assistant", "content": "x"},
                                           {"role": "user", "content": "(empty)"}]
