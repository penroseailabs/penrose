"""Claude through the Anthropic SDK: the same two turn shapes as agents/llm.py (`ask` for a JSON turn, `act`
for native tool calls), for runs whose provider is "anthropic".

Tiers map to models: heavy (orchestrator, memory, compaction, reflection) to ANTHROPIC_HEAVY_MODEL, fast (gate,
titles) and phone (subagents, verifier) to the cheaper ANTHROPIC_FAST_MODEL / ANTHROPIC_PHONE_MODEL. A run's
model picks (agents/catalog.py) take the place of the heavy and phone models. Every
request streams (no HTTP timeouts on long turns) and is read from the final message.
"""

import base64
import json
import logging
import time

import anthropic
from pydantic import ValidationError
from pydantic_core import from_json

from ..config import settings
from ..errors import ProviderError
from . import capture
from .llm import partial_arguments

# Opus 5.5, Sonnet 5.5 and Fable 5.1 can decline a request; "default" re-runs it server-side on the model
# Anthropic picks for that kind of refusal. Haiku has no server-side fallback.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = ("claude-opus-5", "claude-sonnet-5-5", "claude-fable-5")
# The optional fields Claude takes in one request's structured-output schema (its documented limit).
OPTIONAL_LIMIT = 24

logger = logging.getLogger(__name__)


class RequestRefused(ProviderError):
    """Anthropic would not take the request as written (HTTP 400): something in it, not the account or the network."""


def model_for(tier, support=False):
    """(model, effort) for a tier; `support` agents (memory, reflection) take the cheaper support model."""
    if tier == "heavy":
        return (settings.anthropic_support_model if support else settings.anthropic_heavy_model), "high"
    if tier == "phone":
        return settings.anthropic_phone_model, settings.subagent_reasoning_effort
    return settings.anthropic_fast_model, "low"


def thinking_can_be_off(model):
    """Only Haiku accepts thinking off and a forced tool call; Opus 5.5 / Sonnet 5.5 / Fable reject both."""
    return model.startswith("claude-haiku")


def image_blocks(images):
    return [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                     "data": base64.standard_b64encode(jpeg).decode()}}
        for jpeg in images or []
    ]


def tool_def(function):
    """An OpenAI-style function definition (what agent.functions holds) as an Anthropic tool."""
    f = function["function"]
    return {"name": f["name"], "description": f.get("description", ""), "input_schema": f["parameters"]}


def json_text(text, partial=False):
    """The JSON object in a reply. Claude may put a sentence or a ```json fence around it (Sonnet 5.5 does, after
    long thinking), so this is the first `{` that opens a whole object, to its end. A reply still streaming in
    (`partial`) is read from its first `{`."""
    text = text.strip()
    starts = [i for i, ch in enumerate(text) if ch == "{"]
    if partial:
        return text[starts[0]:] if starts else text
    decoder = json.JSONDecoder()
    for start in starts:
        try:
            value, end = decoder.raw_decode(text, start)
        except ValueError:
            continue
        if isinstance(value, dict):
            return text[start:end]
    return text


def output_format(schema_model):
    """The turn's schema as a structured-output format, so the reply is JSON in that shape: without it Sonnet 5.5
    sometimes writes its actions as tool calls (`<invoke name="run_step">`) instead of JSON. None for a schema it
    cannot express (a map with free keys, like `facts`): that turn keeps the schema in its prompt only. Limits it
    drops (lengths, item counts, patterns) move into descriptions, and the reply is still validated in full.

    Claude compiles the schema to a grammar that about doubles with each optional field, and takes at most
    OPTIONAL_LIMIT of them (the orchestrator's 44 were refused as "compiled grammar is too large"). A schema over
    the limit is sent with every field required: the default stays in the field's description and the model
    writes it out, which costs output tokens, so a schema within the limit is sent as it is."""
    raw = schema_model.model_json_schema()

    def optional(node):
        if isinstance(node, list):
            return sum(optional(item) for item in node)
        if not isinstance(node, dict):
            return 0
        here = len(set(node["properties"]) - set(node.get("required", []))) if isinstance(node.get("properties"), dict) else 0
        return here + sum(optional(value) for value in node.values())

    require_all = optional(raw) > OPTIONAL_LIMIT

    def prepare(node):
        if isinstance(node, list):
            return all(prepare(item) for item in node)
        if not isinstance(node, dict):
            return True
        if node.get("type") == "object" and node.get("additionalProperties") not in (None, False):
            return False
        if require_all and node.get("type") == "object" and isinstance(node.get("properties"), dict):
            node["required"] = list(node["properties"])
        if "const" in node:  # the action unions tell their members apart by a constant `tool`
            node["enum"] = [node.pop("const")]
        node.pop("discriminator", None)
        return all(prepare(value) for value in node.values())

    if not prepare(raw):
        return None
    return {"type": "json_schema", "schema": anthropic.transform_schema(raw)}


def parse_problem(exc):
    """Why a reply did not parse, without its content (it can hold the user's data): the first schema error's
    place and kind, or the JSON error."""
    if isinstance(exc, ValidationError) and (errors := exc.errors()):
        first = errors[0]
        return f"{'.'.join(map(str, first.get('loc', ())))}: {first.get('type')} ({len(errors)} errors)"
    return type(exc).__name__ + (f" at char {exc.pos}" if isinstance(exc, json.JSONDecodeError) else "")


def check_stop(message):
    if message.stop_reason == "refusal":
        category = getattr(getattr(message, "stop_details", None), "category", None)
        raise ProviderError(f"Claude declined this request{f' ({category})' if category else ''}.")
    if message.stop_reason == "max_tokens":
        raise ProviderError("Model response exceeded its token limit. Reduce the task scope or increase the configured limit.")


def text_blocks(parts):
    """The context's pieces (llm.context_parts) as text blocks, marked for the cache where a piece says the prompt
    up to its end stays the same. Claude allows 4 marks and the system prompt has one: the last 3 here, the ones
    that keep the most."""
    marked = [i for i, part in enumerate(parts) if getattr(part, "cache", False)][-3:]
    return [{"type": "text", "text": str(part), **({"cache_control": {"type": "ephemeral"}} if i in marked else {})}
            for i, part in enumerate(parts)]


class Claude:
    def __init__(self, client=None):
        self._client = client
        self.formats = {}  # turn schema -> its structured-output format (or None), built once

    @property
    def client(self):
        if self._client is None:
            if not settings.anthropic_api_key:
                raise ProviderError("Anthropic API key is missing. Add it in Settings.")
            self._client = anthropic.AsyncAnthropic(
                api_key=settings.anthropic_api_key, timeout=settings.provider_timeout, max_retries=2,
                http_client=anthropic.DefaultAsyncHttpxClient(event_hooks=capture.hooks()),
            )
        return self._client

    async def close(self):
        if self._client is not None:
            await self._client.close()

    def messages_api(self, model):
        """(messages resource, extra request fields): the beta resource with server-side fallbacks where the
        model supports them."""
        if model.startswith(FALLBACK_MODELS):
            return self.client.beta.messages, {"betas": [FALLBACK_BETA], "fallbacks": "default"}
        return self.client.messages, {}

    async def run(self, model, on_text=None, on_thinking=None, on_usage=None, on_event=None, **request):
        """One streamed request; returns the final message. `on_usage("anthropic", model, usage, None)` hears its
        usage, a declined request's too (it is billed). With a fallback, the model is the one that answered.
        `on_event` hears every stream event as it comes (a tool-call turn follows its calls being written)."""
        resource, extra = self.messages_api(model)
        try:
            async with resource.stream(model=model, **request, **extra) as stream:
                if on_text or on_thinking or on_event:
                    async for event in stream:
                        if on_event:
                            await on_event(event)
                        if event.type != "content_block_delta":
                            continue
                        if event.delta.type == "text_delta" and on_text:
                            await on_text(event.delta.text)
                        elif event.delta.type == "thinking_delta" and on_thinking:
                            await on_thinking(event.delta.thinking)
                message = await stream.get_final_message()
        except anthropic.AuthenticationError:
            raise ProviderError("Anthropic rejected the API key. Check it in Settings.") from None
        except anthropic.PermissionDeniedError:
            raise ProviderError("This Anthropic API key cannot use the configured model.") from None
        except anthropic.NotFoundError:
            raise ProviderError(f"Anthropic does not know the model {model}. Check the configured model names.") from None
        except anthropic.RateLimitError:
            raise ProviderError("Anthropic rate limit reached. Wait a minute and retry.") from None
        except anthropic.APIStatusError as exc:
            # Anthropic's own error text says what is wrong (prompt too long, bad parameter, low credit); it does
            # not echo the request. Never include request bodies, credentials, or user data.
            error = exc.body.get("error") if isinstance(exc.body, dict) else None
            reason = (error.get("message") if isinstance(error, dict) else None) or ""
            logger.warning("Anthropic HTTP %s for %s: %s", exc.status_code, model, reason)
            raise (RequestRefused if exc.status_code == 400 else ProviderError)(
                f"Anthropic returned HTTP {exc.status_code}" + (f": {reason[:300]}" if reason else
                ". Check API access, model configuration, and account credit.")
            ) from None
        except anthropic.APIConnectionError:
            raise ProviderError("Could not reach Anthropic. Check the internet connection.") from None
        if on_usage and getattr(message, "usage", None) is not None:
            on_usage("anthropic", getattr(message, "model", None) or model,
                     message.usage.model_dump(mode="json", exclude_none=True), None)
        check_stop(message)
        return message

    async def ask(self, agent, system, context_text, images=None, on_update=None, on_thinking=None, parse=None,
                  on_usage=None, model=None, effort=None):
        """One JSON turn: `parse(text)` validates it and raises (ValueError) to get one corrected retry. `model` and
        `effort` are the user's picks; without them the agent's tier decides."""
        tier_model, tier_effort = model_for(agent.tier, agent.support)
        model, effort = model or tier_model, effort or tier_effort
        parts = [context_text] if isinstance(context_text, str) else list(context_text)
        content = [*image_blocks(images), *text_blocks(parts)]
        messages = [{"role": "user", "content": content}]
        if agent.schema not in self.formats:
            self.formats[agent.schema] = output_format(agent.schema)
        fmt = self.formats[agent.schema]
        for attempt in range(2):
            text, last_preview = "", 0.0

            async def on_text(delta):
                nonlocal text, last_preview
                text += delta
                # The reply so far is parsed again for each preview: ten a second is plenty to watch it grow.
                if on_update and time.monotonic() - last_preview >= 0.1:
                    last_preview = time.monotonic()
                    try:
                        # Half-written JSON, so the reply shows while it is still being written.
                        partial = from_json(json_text(text, partial=True), allow_partial="trailing-strings")
                    except ValueError:
                        return
                    if isinstance(partial, dict):
                        await on_update(partial)

            thinking_shown = ""

            async def thinking_delta(delta):
                nonlocal thinking_shown
                thinking_shown += delta
                if on_thinking:
                    await on_thinking(delta)

            if on_update:
                await on_update({})
            if on_thinking:
                await on_thinking("", reset=True)
            def send(fmt, messages=messages):
                return self.run(
                    model,
                    on_text=on_text if on_update else None,
                    on_thinking=thinking_delta if on_thinking else None,
                    on_usage=on_usage,
                    max_tokens=32000 if agent.tier == "heavy" else 16000,
                    system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                    thinking={"type": "adaptive", "display": "summarized" if on_thinking else "omitted"},
                    output_config={"effort": effort, **({"format": fmt} if fmt else {})},
                    messages=messages,
                )

            try:
                message = await send(fmt)
            except RequestRefused:
                if not fmt:
                    raise
                # The format is the one part of the request that can be left out: a schema Claude will not compile
                # (too large a grammar) is refused before anything is written. If the same request goes through
                # without it, the format was the reason, and this schema stays in the prompt only from here on.
                message = await send(None)
                logger.warning("%s: Anthropic refused the turn's output format; its schema stays prompted", agent.name)
                self.formats[agent.schema] = fmt = None
            reply = "".join(b.text for b in message.content if b.type == "text")
            try:
                return parse(json_text(reply))
            except ValueError as exc:
                logger.warning("%s reply %d did not parse: %s; %d chars, starts with %r", agent.name, attempt + 1,
                               parse_problem(exc), len(reply), reply.lstrip()[:1])
                if attempt:
                    raise ProviderError(f"{agent.name} returned invalid structured output.") from None
                messages = [*messages, {"role": "assistant", "content": reply or "(empty)"},
                            {"role": "user", "content": f"That did not match the JSON schema ({exc}). Return a complete valid JSON object only."}]
        raise ProviderError("No structured output.")

    async def call(self, agent, system, parts, images=None, on_partial=None, on_thinking=None, on_usage=None,
                   model=None, effort=None):
        """One turn answered with native tool calls: (its text, [{"name", "arguments"}]). The context comes in the
        same parts as a JSON turn's, cached the same way; the tools sit before the system prompt, under its cache
        mark. No output format and no forced call: a turn of text alone is an answer. `on_partial(text, calls)`
        hears the turn while it is written, the calls' arguments as far as they have come."""
        tier_model, tier_effort = model_for(agent.tier, agent.support)
        model, effort = model or tier_model, effort or tier_effort
        content = [*image_blocks(images), *text_blocks(parts)]
        text, calls, last_preview = "", {}, 0.0  # calls by content block: {"name", "json"}

        async def on_event(event):
            nonlocal text, last_preview
            if event.type == "content_block_start" and event.content_block.type == "tool_use":
                calls[event.index] = {"name": event.content_block.name, "json": ""}
            elif event.type != "content_block_delta":
                return
            elif event.delta.type == "text_delta":
                text += event.delta.text
            elif event.delta.type == "input_json_delta" and event.index in calls:
                calls[event.index]["json"] += event.delta.partial_json
            else:
                return
            if on_partial and time.monotonic() - last_preview >= 0.1:  # ten previews a second is plenty to watch it grow
                last_preview = time.monotonic()
                await on_partial(text, [{"name": call["name"], "arguments": partial_arguments(call["json"])}
                                        for _, call in sorted(calls.items())])

        try:
            message = await self.run(
                model,
                on_thinking=on_thinking,
                on_usage=on_usage,
                on_event=on_event,  # always: the calls written so far name the one that broke, if one does
                max_tokens=32000 if agent.tier == "heavy" else 16000,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                thinking={"type": "adaptive", "display": "summarized" if on_thinking else "omitted"},
                output_config={"effort": effort},
                # Arguments stream as they are written (a long question or answer shows while it grows); the turn's
                # own validation (the tool classes) stands in for the API's check of the finished input.
                tools=[{**tool_def(f), "eager_input_streaming": True} for f in agent.functions],
                tool_choice={"type": "auto"},
                messages=[{"role": "user", "content": content}],
            )
        except ValueError:
            # With eager input streaming the API passes a call's arguments on unchecked, and the SDK raises (mid-stream,
            # a plain ValueError) on arguments it cannot parse at all: Opus 5.5 once began `{"notes": s` and the run
            # failed. The turn comes back with that call marked, so the caller asks again with a correction, as for
            # any call whose arguments are not JSON. Its typed API errors are ProviderErrors by now, not caught here.
            if not calls:
                raise
            broken = calls[max(calls)]
            logger.warning("%s: Claude wrote %s arguments that are not JSON (%d chars); the turn is asked again",
                           agent.name, broken["name"], len(broken["json"]))
            return text.strip(), [{"name": broken["name"], "arguments": {}, "invalid_json": broken["json"]}]
        final = [{"name": b.name, **({"arguments": b.input} if isinstance(b.input, dict) else
                                     {"arguments": {}, "invalid_json": str(b.input)})}
                 for b in message.content if b.type == "tool_use"]
        return "".join(b.text for b in message.content if b.type == "text").strip(), final

    async def act(self, agent, system, text, images=None, on_usage=None, model=None, effort=None):
        """One phone-agent turn: (text besides the calls, [{"name", "arguments"}])."""
        tier_model, tier_effort = model_for(agent.tier)
        model, effort = model or tier_model, effort or tier_effort
        thinking_off = not settings.phone_thinking and thinking_can_be_off(model)
        request = {
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": [*image_blocks(images), {"type": "text", "text": text}]}],
            "tools": [tool_def(f) for f in agent.functions],
            "output_config": {"effort": effort},
        }
        if thinking_off:
            request |= {"thinking": {"type": "disabled"}, "tool_choice": {"type": "any"},
                        "max_tokens": settings.phone_max_tokens}
        else:
            # Thinking counts toward max_tokens; the prompt asks for tool calls only.
            request |= {"thinking": {"type": "adaptive"}, "tool_choice": {"type": "auto"},
                        "max_tokens": max(4 * settings.phone_max_tokens, 8000)}
        message = await self.run(model, on_usage=on_usage, **request)
        content = "".join(b.text for b in message.content if b.type == "text").strip()
        calls = [{"name": b.name, "arguments": b.input if isinstance(b.input, dict) else {}}
                 for b in message.content if b.type == "tool_use"]
        return content, calls
