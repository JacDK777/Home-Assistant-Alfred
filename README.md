# Alfred

A Claude-powered butler for Home Assistant. Alfred is a **conversation agent** that plugs into
Assist, so the same agent answers you whether you type in the Assist dialog, talk to a voice
satellite (Home Assistant Voice PE, ESPHome, Wyoming), or use the companion app.

- **Answers questions about your home:** "Is the back door locked?", "How warm is the nursery?",
  "Which lights are still on downstairs?"
- **Controls your devices:** "Turn off everything in the kitchen", "Set the heating to 20 degrees",
  "Start the robot vacuum".
- **Built for voice:** short, plain spoken replies with no markdown. Responses stream to
  text-to-speech as they are generated so Alfred starts talking sooner. Alfred only ends with a
  question when it needs an answer, because a question keeps the microphone open.
- **Optional web search** for general questions such as news or opening hours.
- **Any language** that your speech-to-text and text-to-speech engines support.

Alfred uses Home Assistant's built-in Assist API for device access, so it can only see and control
the entities you have **exposed to Assist**.

## Requirements

- Home Assistant 2025.8 or newer (tested against 2026.2 and 2026.10)
- An Anthropic API key from <https://platform.claude.com/settings/keys>

## Installation

### HACS (recommended)

1. In HACS, open the menu → **Custom repositories**.
2. Add `https://github.com/jacdk777/home-assistant-alfred` with the category **Integration**.
3. Install **Alfred**, then restart Home Assistant.

### Manual

Copy `custom_components/alfred` into your Home Assistant `config/custom_components/` folder and
restart.

## Setup

1. **Settings → Devices & services → Add integration → Alfred**, then paste your API key.
2. **Settings → Voice assistants → Add assistant** (or edit an existing one):
   - **Conversation agent:** Alfred
   - **Speech-to-text:** for example Home Assistant Cloud, Whisper, or Speech-to-Phrase
   - **Text-to-speech:** for example Home Assistant Cloud or Piper
   - Optional: turn on **Prefer handling commands locally** so simple commands like "turn on the
     lights" run instantly on Home Assistant's built-in agent and only everything else goes to
     Alfred.
3. **Settings → Voice assistants → Expose** and pick the entities Alfred may see and control.
4. Make it your preferred assistant, or assign it to a voice satellite.

## Options

Open **Settings → Devices & services → Alfred → Configure**:

| Option | Default | What it does |
| --- | --- | --- |
| Instructions | Butler persona | Alfred's personality and rules. Supports templates, e.g. `{{ states('sensor.outdoor_temperature') }}`. |
| Control Home Assistant | Assist | Which Home Assistant LLM APIs Alfred may use. Clear it for a chat-only assistant. |
| Model | `claude-haiku-5-5` | Haiku is the fastest and cheapest, and handles everyday home control well. Switch to `claude-sonnet-5-5` or `claude-opus-5-5` for more complex reasoning. |
| Effort | Low | How hard Claude thinks before replying. Low keeps voice responses snappy; raise it for complex requests. |
| Maximum response tokens | 16000 | Upper bound on a single reply. |
| Allow web search | Off | Lets Alfred search the web. Each search is billed by Anthropic. |

For Opus 5.5, Sonnet 5.5 and Fable 5.1 (not Haiku), Alfred turns on Anthropic's server-side
`fallbacks: "default"` setting. If a request is declined by a safety classifier, the API retries it
on Anthropic's recommended fallback model instead of failing. If the request is still declined,
Alfred replies with a short apology.

## Admin tools

Alfred can also help look after Home Assistant itself. In **Configure → Control Home
Assistant**, also select **Alfred admin**. Alfred can then:

- read recent warnings and errors from the log ("Are there any errors in the log?")
- list, read, create and edit automations and scripts that were made in the UI
  ("Make the porch light turn on at sunset")
- list, read and edit dashboards that are managed in the UI ("Put the kitchen lights on
  the Main Dash"). The version before each change is kept, so Alfred can undo it.
- look up entities with the integration and device they come from ("Which entities
  come from Tuya Local?")
- check which Tuya Local devices can't connect, compare the IP address saved for each
  one with the address it announces on your network, and update the IP address or
  protocol version ("Why are my Tuya Local bulbs unavailable?")
- report bugs in Alfred as GitHub issues, with Alfred's recent log entries attached,
  if you add a GitHub token in the options. Use a fine-grained token limited to this
  repository with **Issues: read and write**. Issues are public.

These tools only work for Home Assistant **administrators**. Requests from other users,
or from voice satellites and automations that are not tied to a signed-in user, get no
admin tools. Alfred is told to describe every change and wait for your yes before
saving it, and Home Assistant validates each automation and script before it is
written.

## Cost

Alfred uses prompt caching. Your exposed-device list, the tool definitions and the conversation so
far are cached for 5 minutes, and Anthropic bills cached input at about a tenth of the normal price.
Commands and state questions take two calls to Claude (choose the tool, then reply), and the second
call reads almost everything from cache.

With the default Haiku 5.5 model, a home with about 50 exposed entities costs roughly a tenth of a
cent per request, so 20 requests a day comes to well under $1 a month. Sonnet 5.5 costs about 20
times as much and Opus 5.5 about 40 times as much. Exposing fewer entities and turning on **Prefer
handling commands locally** lowers the cost further.

To see real token usage, enable debug logging for `custom_components.alfred`. Each call then logs
its input, cache-read, cache-write and output tokens.

## Using Alfred from automations

```yaml
action: conversation.process
data:
  agent_id: conversation.alfred
  text: "Give me a one sentence summary of the house before I leave."
response_variable: alfred
```

Then speak the reply with `{{ alfred.response.speech.plain.speech }}` on a media player via
`tts.speak`.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
ruff check . && ruff format --check .
pytest
```

The tests use `pytest-homeassistant-custom-component` and mock the Anthropic API, so they need no
API key.
