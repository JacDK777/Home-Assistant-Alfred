"""Shared fixtures for Alfred tests."""

from collections.abc import AsyncIterator, Generator
import copy
from typing import Any
from unittest.mock import AsyncMock, patch

from anthropic._models import construct_type
from anthropic.types import Message
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.alfred.config_flow import DEFAULT_OPTIONS
from custom_components.alfred.const import DOMAIN


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/ in every test."""


def make_message(
    content: list[dict[str, Any]], stop_reason: str = "end_turn"
) -> Message:
    """Build an Anthropic Message the way the SDK would parse it."""
    return construct_type(
        type_=Message,
        value={
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-haiku-5-5",
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10},
        },
    )


class FakeStream:
    """Stand-in for the SDK's AsyncMessageStream context manager."""

    def __init__(self, message: Message) -> None:
        self._message = message

    async def __aenter__(self) -> "FakeStream":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    @property
    def text_stream(self) -> AsyncIterator[str]:
        """Yield the text of each text block, like the SDK's text_stream."""
        return self._iter_text()

    async def _iter_text(self) -> AsyncIterator[str]:
        for block in self._message.content:
            if block.type == "text":
                yield block.text

    async def get_final_message(self) -> Message:
        """Return the complete message."""
        return self._message


class StreamRecorder:
    """Replays queued responses and records every request."""

    def __init__(self) -> None:
        self.responses: list[Message] = []
        self.requests: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> FakeStream:
        self.requests.append(copy.deepcopy(kwargs))
        return FakeStream(self.responses.pop(0))


@pytest.fixture
def mock_models_list() -> Generator[AsyncMock]:
    """Make API key validation succeed."""
    with patch(
        "anthropic.resources.models.AsyncModels.list", new_callable=AsyncMock
    ) as mock:
        yield mock


@pytest.fixture
def stream_recorder() -> Generator[StreamRecorder]:
    """Patch Messages.stream with a recorder."""
    recorder = StreamRecorder()
    with patch(
        "anthropic.resources.messages.AsyncMessages.stream",
        side_effect=lambda *args, **kwargs: recorder(**kwargs),
    ):
        yield recorder


@pytest.fixture
def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """An Alfred config entry with default options."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Alfred",
        data={CONF_API_KEY: "sk-test"},
        options=dict(DEFAULT_OPTIONS),
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
async def setup_alfred(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_models_list: AsyncMock
) -> MockConfigEntry:
    """Set up Alfred and its dependencies."""
    assert await async_setup_component(hass, "homeassistant", {})
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry
