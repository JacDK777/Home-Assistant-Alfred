"""Constants for the Alfred integration."""

import logging

DOMAIN = "alfred"
LOGGER = logging.getLogger(__package__)

DEFAULT_NAME = "Alfred"

CONF_CHAT_MODEL = "chat_model"
CONF_PROMPT = "prompt"
CONF_EFFORT = "effort"
CONF_MAX_TOKENS = "max_tokens"
CONF_WEB_SEARCH = "web_search"

DEFAULT_CHAT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "low"
DEFAULT_MAX_TOKENS = 16000
DEFAULT_WEB_SEARCH = False

MODEL_OPTIONS = [
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-haiku-5-5",
    "claude-fable-5-1",
]
EFFORT_OPTIONS = ["low", "medium", "high"]

# Models that accept the server-side `fallbacks: "default"` parameter.
FALLBACK_MODELS = {
    "claude-fable-5-1",
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-sonnet-5-5",
}
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Max number of model round trips per user message (tool calls in between).
MAX_TOOL_ITERATIONS = 10

DEFAULT_PROMPT = """You are Alfred, a calm, capable and discreet butler who runs this
smart home.

Answer questions about the home and control devices using the tools you have.
Many replies are spoken aloud by a voice assistant, so:
- Keep answers short: one or two sentences unless asked for more detail.
- Use plain conversational sentences. No markdown, lists, tables, emoji or URLs.
- Say numbers, times and units the way a person would say them.
- When you have done something, confirm it briefly ("The kitchen lights are off.").
- Only end with a question when you genuinely need an answer, because a question
  keeps the microphone listening for the reply.
If a request is ambiguous, pick the most sensible interpretation; ask only when a
wrong guess would matter (locks, alarms, garage doors, heating)."""
