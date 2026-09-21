import os

from anthropic import Anthropic
from dotenv import load_dotenv

# Loads variables from a local .env file, if one exists. In GitHub Actions
# the environment variables are already set by repository secrets, so this
# is a harmless no-op there.
load_dotenv()

_client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def call_llm(model, messages, tools=None, tool_choice=None, system=None, max_tokens=1024):
    """The one wrapper every LLM call goes through. Logs token usage so
    cost stays visible. Returns the raw API response; callers pull out
    whatever shape they need (text or a tool call)."""
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

    response = _client.messages.create(**kwargs)

    usage = response.usage
    print(f"[llm] model={model} in={usage.input_tokens} out={usage.output_tokens}")

    return response
