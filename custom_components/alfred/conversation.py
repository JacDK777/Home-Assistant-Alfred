"""Alfred conversation agent, used by Assist for both text and voice."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
import json
from typing import Any, Literal

import anthropic

from homeassistant.components import conversation
from homeassistant.const import CONF_LLM_HASS_API, MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, llm
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AlfredConfigEntry

try:
    import probatio
except ImportError:  # Home Assistant 2026.8 and older use voluptuous
    probatio = None

from .const import (
    CACHE_CONTROL,
    CONF_CHAT_MODEL,
    CONF_EFFORT,
    CONF_MAX_TOKENS,
    CONF_PROMPT,
    CONF_WEB_SEARCH,
    DEFAULT_CHAT_MODEL,
    DEFAULT_EFFORT,
    DEFAULT_MAX_TOKENS,
    DEFAULT_NAME,
    DEFAULT_PROMPT,
    DEFAULT_WEB_SEARCH,
    DOMAIN,
    DYNAMIC_WEB_SEARCH_MODELS,
    FALLBACK_BETA,
    FALLBACK_MODELS,
    LOGGER,
    MAX_TOOL_ITERATIONS,
)

REFUSAL_MESSAGE = "I'm afraid I can't help with that one."


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: AlfredConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Alfred conversation entity."""
    async_add_entities([AlfredConversationEntity(config_entry)])


def _format_tool(
    tool: llm.Tool, custom_serializer: Callable[[Any], Any] | None
) -> dict[str, Any]:
    """Convert a Home Assistant LLM tool into an Anthropic tool definition."""
    if probatio is not None:
        try:
            schema = probatio.to_openapi(
                tool.parameters,
                custom_serializer=custom_serializer,
                openapi_version="3.1.0",
            )
        except TypeError:  # probatio before 0.13 has no openapi_version
            schema = probatio.to_openapi(
                tool.parameters, custom_serializer=custom_serializer
            )
    else:
        from voluptuous_openapi import convert

        schema = convert(tool.parameters, custom_serializer=custom_serializer)
    return {
        "name": tool.name,
        "description": tool.description or "",
        # The API rejects these at the top level of an input schema.
        "input_schema": {
            key: value
            for key, value in schema.items()
            if key not in ("oneOf", "anyOf", "allOf")
        },
    }


def _block_to_param(block: Any) -> dict[str, Any]:
    """Serialize a response content block so it can be sent back unchanged."""
    data = block.to_dict()
    data.pop("parsed_output", None)
    return {key: value for key, value in data.items() if value is not None}


def _echoable_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop blocks the API must not receive back after a mid-output fallback.

    Before the last `fallback` marker, thinking and client tool_use blocks (and
    server_tool_use blocks without a matching result) belong to the declined attempt.
    """
    boundary = max(
        (i for i, block in enumerate(blocks) if block.get("type") == "fallback"),
        default=-1,
    )
    if boundary < 0:
        return blocks
    result_ids = {block.get("tool_use_id") for block in blocks}
    kept: list[dict[str, Any]] = []
    for i, block in enumerate(blocks):
        if i < boundary:
            block_type = block.get("type")
            if block_type in ("thinking", "redacted_thinking", "tool_use"):
                continue
            if block_type == "server_tool_use" and block.get("id") not in result_ids:
                continue
        kept.append(block)
    return kept


def _convert_chat_log(
    chat_log: conversation.ChatLog,
) -> list[dict[str, Any]]:
    """Convert the chat log into Anthropic messages.

    Assistant turns are replayed from the stored native content blocks so thinking
    blocks reach the API byte-for-byte unchanged.
    """
    messages: list[dict[str, Any]] = []

    def append(role: str, blocks: list[dict[str, Any]]) -> None:
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].extend(blocks)
        else:
            messages.append({"role": role, "content": list(blocks)})

    for content in chat_log.content[1:]:
        if isinstance(content, conversation.UserContent):
            append("user", [{"type": "text", "text": content.content}])
        elif isinstance(content, conversation.ToolResultContent):
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": content.tool_call_id,
            }
            if (result := getattr(content, "result", None)) is not None:
                # Home Assistant 2026.10 and newer wrap results in llm.ToolResult.
                block["content"] = json.dumps(result.data)
                if result.error:
                    block["is_error"] = True
            else:
                block["content"] = json.dumps(content.tool_result)
            append("user", [block])
        elif isinstance(content, conversation.AssistantContent):
            if isinstance(content.native, list):
                blocks = _echoable_blocks(content.native)
            else:
                # Content added by Home Assistant itself (e.g. a pipeline handoff).
                blocks = []
                if content.content:
                    blocks.append({"type": "text", "text": content.content})
                for tool_call in content.tool_calls or []:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tool_call.id,
                            "name": tool_call.tool_name,
                            "input": tool_call.tool_args,
                        }
                    )
            if blocks:
                append("assistant", blocks)
    return messages


class AlfredConversationEntity(
    conversation.ConversationEntity, conversation.AbstractConversationAgent
):
    """Alfred, the butler: answers questions and controls the home."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supports_streaming = True

    def __init__(self, entry: AlfredConfigEntry) -> None:
        """Initialize the agent."""
        self.entry = entry
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title or DEFAULT_NAME,
            manufacturer="Anthropic",
            model=entry.options.get(CONF_CHAT_MODEL, DEFAULT_CHAT_MODEL),
            entry_type=dr.DeviceEntryType.SERVICE,
        )
        if entry.options.get(CONF_LLM_HASS_API):
            self._attr_supported_features = (
                conversation.ConversationEntityFeature.CONTROL
            )

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Claude understands any language Assist sends."""
        return MATCH_ALL

    async def _async_handle_message(
        self,
        user_input: conversation.ConversationInput,
        chat_log: conversation.ChatLog,
    ) -> conversation.ConversationResult:
        """Answer one user message, calling Home Assistant tools as needed."""
        options = self.entry.options
        try:
            await chat_log.async_provide_llm_data(
                user_input.as_llm_context(DOMAIN),
                options.get(CONF_LLM_HASS_API),
                options.get(CONF_PROMPT) or DEFAULT_PROMPT,
                user_input.extra_system_prompt,
            )
        except conversation.ConverseError as err:
            return err.as_conversation_result()

        await self._async_run_chat_log(chat_log)
        return conversation.async_get_result_from_chat_log(user_input, chat_log)

    def _build_request(self, chat_log: conversation.ChatLog) -> dict[str, Any]:
        """Build the static part of the Messages API request."""
        options = self.entry.options
        model = options.get(CONF_CHAT_MODEL, DEFAULT_CHAT_MODEL)

        tools: list[dict[str, Any]] = []
        if chat_log.llm_api:
            tools.extend(
                _format_tool(tool, chat_log.llm_api.custom_serializer)
                for tool in chat_log.llm_api.tools
            )
        if tools:
            # Tool definitions rarely change, so cache them even if the prompt does.
            tools[-1]["cache_control"] = CACHE_CONTROL
        if options.get(CONF_WEB_SEARCH, DEFAULT_WEB_SEARCH):
            search_type = (
                "web_search_20260209"
                if model in DYNAMIC_WEB_SEARCH_MODELS
                else "web_search_20250305"
            )
            tools.append({"type": search_type, "name": "web_search", "max_uses": 3})

        extra_body: dict[str, Any] = {
            "output_config": {"effort": options.get(CONF_EFFORT, DEFAULT_EFFORT)},
            # Automatic caching of the conversation so far: the follow-up call after
            # a tool result re-reads everything before it at the cached price.
            "cache_control": CACHE_CONTROL,
        }
        extra_headers: dict[str, str] = {}
        if model in FALLBACK_MODELS:
            # On a safety decline, let the API retry on Anthropic's recommended model.
            extra_body["fallbacks"] = "default"
            extra_headers["anthropic-beta"] = FALLBACK_BETA

        request: dict[str, Any] = {
            "model": model,
            "max_tokens": int(options.get(CONF_MAX_TOKENS, DEFAULT_MAX_TOKENS)),
            # The system prompt (instructions + exposed devices) is stable across
            # turns, so tools + system are cached together for every request.
            "system": [
                {
                    "type": "text",
                    "text": chat_log.content[0].content,
                    "cache_control": CACHE_CONTROL,
                }
            ],
            "extra_body": extra_body,
            "extra_headers": extra_headers,
        }
        if tools:
            request["tools"] = tools
        return request

    async def _async_run_chat_log(self, chat_log: conversation.ChatLog) -> None:
        """Run the model/tool loop until Claude gives a final answer."""
        client = self.entry.runtime_data
        request = self._build_request(chat_log)

        for _ in range(MAX_TOOL_ITERATIONS):
            stop_reason: list[str | None] = [None]
            try:
                async for _content in chat_log.async_add_delta_content_stream(
                    self.entity_id,
                    self._async_stream_turn(
                        client, request, _convert_chat_log(chat_log), stop_reason
                    ),
                ):
                    pass
            except anthropic.AuthenticationError as err:
                self.entry.async_start_reauth(self.hass)
                raise HomeAssistantError("Your Anthropic API key was rejected") from err
            except anthropic.AnthropicError as err:
                LOGGER.error("Error talking to Anthropic: %s", err)
                raise HomeAssistantError(
                    f"Sorry, I had trouble reaching Claude: {err}"
                ) from err

            # Tool results need another round trip; pause_turn means a server tool
            # (web search) is still working and the turn should simply be resumed.
            if not chat_log.unresponded_tool_results and stop_reason[0] != "pause_turn":
                return

        LOGGER.warning("Stopped after %s tool iterations", MAX_TOOL_ITERATIONS)

    async def _async_stream_turn(
        self,
        client: anthropic.AsyncAnthropic,
        request: dict[str, Any],
        messages: list[dict[str, Any]],
        stop_reason: list[str | None],
    ) -> AsyncGenerator[conversation.AssistantContentDeltaDict]:
        """Stream one assistant message into the chat log.

        Text is forwarded as it arrives so text-to-speech can start speaking early.
        Tool calls and the native content blocks are added once the message completes.
        """
        yield {"role": "assistant"}
        async with client.messages.stream(messages=messages, **request) as stream:
            async for text in stream.text_stream:
                yield {"content": text}
            message = await stream.get_final_message()

        stop_reason[0] = message.stop_reason
        LOGGER.debug(
            "Usage: input=%s cache_read=%s cache_write=%s output=%s",
            message.usage.input_tokens,
            message.usage.cache_read_input_tokens,
            message.usage.cache_creation_input_tokens,
            message.usage.output_tokens,
        )
        if message.stop_reason == "refusal":
            LOGGER.info("Request declined: %s", getattr(message, "stop_details", None))
            # A refused turn is not replayed; answer politely instead.
            yield {"content": REFUSAL_MESSAGE}
            return
        if message.stop_reason == "max_tokens":
            LOGGER.warning("Response hit max_tokens; consider raising the limit")

        blocks = message.content
        if message.stop_reason == "max_tokens":
            # A tool_use cut off mid-input can't be run, and replaying it later
            # without a matching tool_result would be rejected by the API.
            blocks = [block for block in blocks if block.type != "tool_use"]

        if tool_calls := [
            llm.ToolInput(id=block.id, tool_name=block.name, tool_args=block.input)
            for block in blocks
            if block.type == "tool_use"
        ]:
            yield {"tool_calls": tool_calls}
        yield {"native": [_block_to_param(block) for block in blocks]}
