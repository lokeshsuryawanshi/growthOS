"""Live protocol check of the OpenAI adapter against Groq's OpenAI-compatible endpoint (no OpenAI key needed).

  python scripts/live_adapters.py          # reads GROQ_API_KEY from .env; prints results, never the key

This exercises the OpenAI adapter's real request and response code (chat completions, JSON mode, tool calls, a
rejected key) over the network. It verifies the protocol, not OpenAI's own service or models.
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def main() -> int:
    from dotenv import dotenv_values
    from app.llm.providers import KeyRejected, OpenAIProvider
    from app.llm.types import LLMRequest, ToolSpec
    key = dotenv_values(ROOT / ".env").get("GROQ_API_KEY")
    if not key:
        sys.exit("GROQ_API_KEY is not set in .env")
    base, model = "https://api.groq.com/openai/v1", "openai/gpt-oss-20b"
    p = OpenAIProvider(key, base_url=base)
    results = {}
    r = await p.complete(model, LLMRequest(agent="t", tier="small", max_tokens=300, messages=[
        {"role": "system", "content": "Answer in one short sentence."}, {"role": "user", "content": "What colour is the sky?"}]))
    results["text"] = bool(r.content) and r.prompt_tokens > 0
    r = await p.complete(model, LLMRequest(agent="t", tier="small", max_tokens=300, json_mode=True, messages=[
        {"role": "user", "content": 'Reply with JSON {"city": <the capital of France>}'}]))
    results["json_mode"] = json.loads(r.content).get("city", "").lower() == "paris"
    tool = ToolSpec(name="get_weather", description="Weather for a city",
                    parameters={"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]})
    r = await p.complete(model, LLMRequest(agent="t", tier="small", max_tokens=400, tools=[tool], messages=[
        {"role": "user", "content": "Use the tool to get the weather in Pune."}]))
    results["tool_call"] = bool(r.tool_calls) and r.tool_calls[0].name == "get_weather" and \
        "pune" in json.loads(r.tool_calls[0].arguments).get("city", "").lower()
    try:
        await OpenAIProvider("gsk_" + "0" * 52, base_url=base).complete(model, LLMRequest(
            agent="t", tier="small", max_tokens=20, messages=[{"role": "user", "content": "hi"}]))
        results["bad_key_rejected"] = False
    except KeyRejected as e:
        results["bad_key_rejected"] = "0" * 20 not in str(e)          # rejected, and the key is not echoed back
    print(json.dumps({"endpoint": base, "model": model, **results}, indent=2))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
