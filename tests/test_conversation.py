"""Tests for the Alfred conversation agent."""

from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest
import voluptuous as vol

from homeassistant.components import conversation
from homeassistant.const import CONF_LLM_HASS_API
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import intent, llm
from homeassistant.util.json import JsonObjectType

from .conftest import StreamRecorder, make_message
from custom_components.alfred.const import CONF_CHAT_MODEL, CONF_WEB_SEARCH

AGENT_ID = "conversation.alfred"


class GetTemperatureTool(llm.Tool):
    """A fake tool that reports a room temperature."""

    name = "get_temperature"
    description = "Get the temperature of a room"
    parameters = vol.Schema({vol.Required("room"): str})

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        self.calls.append(tool_input.tool_args)
        return {"temperature": 21, "unit": "°C"}


class FakeAPI(llm.API):
    """An LLM API exposing GetTemperatureTool."""

    def __init__(self, hass: HomeAssistant, tool: GetTemperatureTool) -> None:
        super().__init__(hass=hass, id="fake_api", name="Fake API")
        self.tool = tool

    async def async_get_api_instance(
        self, llm_context: llm.LLMContext
    ) -> llm.APIInstance:
        return llm.APIInstance(
            api=self,
            api_prompt="Use the tools.",
            llm_context=llm_context,
            tools=[self.tool],
        )


@pytest.fixture
def temperature_tool(hass: HomeAssistant) -> GetTemperatureTool:
    """Register the fake API and return its tool."""
    tool = GetTemperatureTool()
    llm.async_register_api(hass, FakeAPI(hass, tool))
    return tool


async def _converse(hass: HomeAssistant, text: str, conversation_id: str | None = None):
    return await conversation.async_converse(
        hass, text, conversation_id, Context(), agent_id=AGENT_ID
    )


async def test_simple_answer(
    hass: HomeAssistant, setup_alfred, stream_recorder: StreamRecorder
) -> None:
    """A plain question gets a spoken answer and sends the expected request."""
    stream_recorder.responses.append(
        make_message([{"type": "text", "text": "Good evening. All is well."}])
    )

    result = await _converse(hass, "How is the house?")

    assert result.response.response_type is intent.IntentResponseType.ACTION_DONE
    assert result.response.speech["plain"]["speech"] == "Good evening. All is well."
    request = stream_recorder.requests[0]
    assert request["model"] == "claude-haiku-5-5"
    assert request["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "How is the house?"}]}
    ]
    assert request["extra_body"]["output_config"] == {"effort": "low"}
    # Haiku 5.5 has no server-side fallback.
    assert "fallbacks" not in request["extra_body"]
    assert request["extra_headers"] == {}


async def test_prompt_caching(
    hass: HomeAssistant,
    setup_alfred,
    stream_recorder: StreamRecorder,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Tools, the system prompt and the conversation tail are marked for caching."""
    stream_recorder.responses += [
        make_message([{"type": "text", "text": "Hi."}]),
        make_message([{"type": "text", "text": "Still here."}]),
    ]

    result = await _converse(hass, "Hello")
    freezer.tick(timedelta(minutes=1))
    await _converse(hass, "Are you there?", result.conversation_id)

    request, follow_up = stream_recorder.requests
    # No clock or other per-turn data in the cached prefix, so later turns hit it.
    assert follow_up["system"] == request["system"]
    assert follow_up["tools"] == request["tools"]
    (system,) = request["system"]
    assert "Alfred" in system["text"]
    assert system["cache_control"] == {"type": "ephemeral"}
    assert request["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert all("cache_control" not in tool for tool in request["tools"][:-1])
    assert request["extra_body"]["cache_control"] == {"type": "ephemeral"}


async def test_fallbacks_on_supported_models(
    hass: HomeAssistant, setup_alfred, stream_recorder: StreamRecorder
) -> None:
    """Opus 5.5 gets server-side refusal fallbacks."""
    hass.config_entries.async_update_entry(
        setup_alfred,
        options={**setup_alfred.options, CONF_CHAT_MODEL: "claude-opus-5-5"},
    )
    await hass.async_block_till_done()
    stream_recorder.responses.append(make_message([{"type": "text", "text": "Hi."}]))

    await _converse(hass, "Hello")

    request = stream_recorder.requests[0]
    assert request["model"] == "claude-opus-5-5"
    assert request["extra_body"]["fallbacks"] == "default"
    assert request["extra_headers"] == {
        "anthropic-beta": "server-side-fallback-2026-07-01"
    }


async def test_tool_call_round_trip(
    hass: HomeAssistant,
    setup_alfred,
    stream_recorder: StreamRecorder,
    temperature_tool: GetTemperatureTool,
) -> None:
    """Claude calls a Home Assistant tool, gets the result and answers."""
    hass.config_entries.async_update_entry(
        setup_alfred, options={**setup_alfred.options, CONF_LLM_HASS_API: ["fake_api"]}
    )
    await hass.async_block_till_done()

    thinking = {"type": "thinking", "thinking": "", "signature": "sig-1"}
    tool_use = {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "get_temperature",
        "input": {"room": "lounge"},
    }
    stream_recorder.responses += [
        make_message([thinking, tool_use], stop_reason="tool_use"),
        make_message([{"type": "text", "text": "The lounge is 21 degrees."}]),
        make_message([{"type": "text", "text": "You're welcome."}]),
    ]

    result = await _converse(hass, "How warm is the lounge?")

    assert temperature_tool.calls == [{"room": "lounge"}]
    assert result.response.speech["plain"]["speech"] == "The lounge is 21 degrees."
    first, second = stream_recorder.requests
    assert [tool["name"] for tool in first["tools"]] == ["get_temperature"]
    assert second["messages"][1:] == [
        # The thinking block is replayed unchanged alongside the tool call.
        {"role": "assistant", "content": [thinking, tool_use]},
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": '{"temperature": 21, "unit": "\\u00b0C"}',
                }
            ],
        },
    ]

    # Follow-up in the same conversation keeps the full history.
    await _converse(hass, "Thanks", result.conversation_id)
    third = stream_recorder.requests[2]
    assert [m["role"] for m in third["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert third["messages"][3]["content"] == [
        {"type": "text", "text": "The lounge is 21 degrees."}
    ]


async def test_refusal(
    hass: HomeAssistant, setup_alfred, stream_recorder: StreamRecorder
) -> None:
    """A declined request gets a polite spoken reply."""
    stream_recorder.responses.append(make_message([], stop_reason="refusal"))

    result = await _converse(hass, "Something not allowed")

    assert "can't help" in result.response.speech["plain"]["speech"]


@pytest.mark.parametrize(
    ("model", "search_type"),
    [
        ("claude-haiku-5-5", "web_search_20250305"),
        ("claude-opus-5-5", "web_search_20260209"),
    ],
)
async def test_web_search(
    hass: HomeAssistant,
    setup_alfred,
    stream_recorder: StreamRecorder,
    model: str,
    search_type: str,
) -> None:
    """Web search uses dynamic filtering only on models that support it."""
    hass.config_entries.async_update_entry(
        setup_alfred,
        options={**setup_alfred.options, CONF_WEB_SEARCH: True, CONF_CHAT_MODEL: model},
    )
    await hass.async_block_till_done()
    stream_recorder.responses.append(make_message([{"type": "text", "text": "Sunny."}]))

    await _converse(hass, "What's the weather?")

    request = stream_recorder.requests[0]
    assert request["tools"][-1] == {
        "type": search_type,
        "name": "web_search",
        "max_uses": 3,
    }


async def test_fallback_blocks_not_echoed(
    hass: HomeAssistant, setup_alfred, stream_recorder: StreamRecorder
) -> None:
    """After a mid-output fallback, the declined attempt's thinking isn't replayed."""
    stream_recorder.responses += [
        make_message(
            [
                {"type": "thinking", "thinking": "", "signature": "declined"},
                {"type": "fallback", "from": {"model": "a"}, "to": {"model": "b"}},
                {"type": "text", "text": "Certainly."},
            ]
        ),
        make_message([{"type": "text", "text": "Done."}]),
    ]

    result = await _converse(hass, "First")
    await _converse(hass, "Second", result.conversation_id)

    assert stream_recorder.requests[1]["messages"][1]["content"] == [
        {"type": "fallback", "from": {"model": "a"}, "to": {"model": "b"}},
        {"type": "text", "text": "Certainly."},
    ]
