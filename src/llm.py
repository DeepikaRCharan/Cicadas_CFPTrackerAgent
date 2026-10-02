import os

from anthropic import Anthropic
from dotenv import load_dotenv

# Loads variables from a local .env file, if one exists. In GitHub Actions
# the environment variables are already set by repository secrets, so this
# is a harmless no-op there.
load_dotenv()

_client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# Running per-model totals for this process, so a run can report what it
# spent: {model: {"calls", "input_tokens", "output_tokens", "searches", "fetches"}}.
USAGE: dict[str, dict[str, int]] = {}
_TALLY_KEYS = ("calls", "input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens", "searches", "fetches")


def usage_cost(model: str, input_tokens: int, output_tokens: int, cache_write_tokens: int = 0,
               cache_read_tokens: int = 0, searches: int = 0) -> float:
    """Estimated USD at list prices (config.PRICE_PER_MTOK); 0 for an unpriced model."""
    from src.config import CACHE_READ_MULTIPLIER, CACHE_WRITE_MULTIPLIER, PRICE_PER_MTOK, PRICE_PER_WEB_SEARCH

    price_in, price_out = PRICE_PER_MTOK.get(model, (0.0, 0.0))
    return (
        (input_tokens + cache_write_tokens * CACHE_WRITE_MULTIPLIER + cache_read_tokens * CACHE_READ_MULTIPLIER) / 1e6 * price_in
        + output_tokens / 1e6 * price_out
        + searches * PRICE_PER_WEB_SEARCH
    )


def call_llm(model, messages, tools=None, tool_choice=None, system=None, max_tokens=1024, **extra):
    """The one wrapper every LLM call goes through. Logs token usage (and
    server-side web searches/fetches, when any ran) so cost stays visible.
    Returns the raw API response; callers pull out whatever shape they need
    (text or a tool call). `extra` passes any other Messages API parameter
    straight through."""
    kwargs = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if tools is not None:
        kwargs["tools"] = tools
    if tool_choice is not None:
        kwargs["tool_choice"] = tool_choice
    if system is not None:
        kwargs["system"] = system

    kwargs.update(extra)
    response = _client.messages.create(**kwargs)

    usage = response.usage
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    line = f"[llm] model={model} in={usage.input_tokens} out={usage.output_tokens}"
    if cache_write or cache_read:
        line += f" cache_write={cache_write} cache_read={cache_read}"
    server = getattr(usage, "server_tool_use", None)
    if server is not None and (server.web_search_requests or server.web_fetch_requests):
        line += f" searches={server.web_search_requests} fetches={server.web_fetch_requests}"
    print(line)

    tally = USAGE.setdefault(model, dict.fromkeys(_TALLY_KEYS, 0))
    tally["calls"] += 1
    tally["input_tokens"] += usage.input_tokens
    tally["output_tokens"] += usage.output_tokens
    tally["cache_write_tokens"] += cache_write
    tally["cache_read_tokens"] += cache_read
    if server is not None:
        tally["searches"] += server.web_search_requests or 0
        tally["fetches"] += server.web_fetch_requests or 0

    return response
