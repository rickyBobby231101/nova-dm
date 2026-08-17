"""Phase 8: the Accord -- the DM's voice is pluggable.

Phase 3 wired the DM straight into one vendor's client. That was fine until the
key ran dry, at which point the whole table stopped: a credit error and nobody
could play. This module puts a seam there instead, so several models can take
the DM's chair and a dead one steps aside rather than ending the session.

Two kinds of model can sit in that chair, and they need genuinely different
handling:

  * Models that call tools natively (Claude, GPT, Gemini) run the Phase 3 loop
    unchanged -- narrate, call a tool, see the real result, narrate again.

  * Older local models cannot. Ollama refuses tools outright for gemma3 and
    deepseek-r1, and llama3.2:1b accepts them only to invent its own argument
    names. For those there is the JSON-plan path in OllamaProvider, which is
    the design the original spec called for: the model names the rolls it wants,
    the engine makes them, and only then does the model get to describe what
    happened.

  * Newer local models can, and qwen3 does it properly -- real tool_calls with
    the right argument names. OllamaToolProvider runs it through the same loop
    as the hosted voices, which is why the default chain is now local-only:
    running on hardware you own no longer costs the stronger guarantee.

What survives across both, and is the whole point of the seam, is the Phase 3
rule: the model never invents a die roll, a check result, or an HP total. In the
tool path the engine owns the dice because the model must call for them. In the
JSON-plan path it owns them because the model is asked for intentions before it
is allowed to write a single word of prose -- it cannot describe a landing it
has not been told the character stuck.
"""
import json
import os
import re

import requests

from . import dice

# A combat round legitimately spends several calls (attack, damage, advance
# turn), so this has to be generous enough to carry a whole exchange.
MAX_TOOL_ITERATIONS = 12

# Tried in this order when NOVA_DM_LLM_PROVIDER is unset or "auto".
#
# Local only, and ordered by what this hardware can actually do rather than by
# which model is cleverest. Measured on the Inspiron against a real DM prompt:
#
#   llama3.2:1b  prompt 12.4 tok/s, gen 4.0 tok/s  ->  ~2.4 min/turn (2 calls)
#   qwen3:4b     prompt  2.8 tok/s, gen 1.5 tok/s  ->  ~47 min/turn (3 calls)
#
# qwen3 is the better DM -- it calls tools natively, so the engine holds the
# dice for the strong reason rather than because the model was asked to plan
# first. It is also unplayable here. llama3.2 leads because a turn that arrives
# is worth more than a turn that is argued for, and the JSON-plan path keeps the
# same guarantee by a different route: intentions before prose, always.
#
# qwen3 and gemma3 sit behind it as real backups, not decoration -- if llama3.2
# is not pulled, or Ollama refuses it, someone else takes the chair instead of
# the table losing its DM. They cost nothing while llama3.2 answers.
DEFAULT_CHAIN = ["ollama", "ollama-tools", "ollama-gemma"]

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")

# How long one Ollama call may take. Generous because this hardware is slow and
# a turn that arrives late still beats one that does not arrive: 300s was enough
# for llama3.2 and silently turned NOVA_DM_NARRATOR into a trap, since a slower
# narrator blew the budget, raised, and handed the turn to a provider with no
# hope of finishing it.
OLLAMA_TIMEOUT = int(os.environ.get("NOVA_DM_OLLAMA_TIMEOUT", "900"))


class ProviderUnavailable(Exception):
    """This voice cannot speak right now -- no key, no credit, no server.

    Distinct from a genuine bug: the chain is allowed to step over this one and
    ask the next voice, where any other exception should surface loudly.
    """


class TurnExhausted(Exception):
    """Ran out of tool iterations. The voice is fine; this turn just ran long,
    so it must not count against the provider or trigger a handover."""


class Outcome:
    """Who spoke, and what went wrong if anything.

    Errors come back rather than being emitted from in here: a failure notice is
    table chatter, not narration, and the caller is the one that knows it must
    not be logged to the campaign feed or read aloud through the speaker.
    """

    __slots__ = ("provider", "error")

    def __init__(self, provider=None, error=None):
        self.provider = provider
        self.error = error


class ToolCall:
    __slots__ = ("id", "name", "input")

    def __init__(self, id, name, input):
        self.id = id
        self.name = name
        self.input = input


class Reply:
    """One model response, normalized away from any vendor's block shapes."""

    __slots__ = ("narration", "tool_calls", "raw")

    def __init__(self, narration="", tool_calls=None, raw=None):
        self.narration = narration
        self.tool_calls = tool_calls or []
        self.raw = raw


# --------------------------------------------------------------------------
# Tool-loop providers
# --------------------------------------------------------------------------

class ToolLoopProvider:
    """Shared Phase 3 loop. Subclasses only translate dialect.

    `_begin` builds whatever conversation state the vendor wants, `_step` sends
    it and normalizes the answer, `_record` appends the assistant turn and the
    tool results back onto it. The loop itself -- and therefore the ordering
    guarantee that results are seen before the next narration -- lives here once.
    """

    name = None

    def run_turn(self, system, user_message, tools, execute, emit):
        state = self._begin(system, user_message, tools)
        for _ in range(MAX_TOOL_ITERATIONS):
            reply = self._step(state)
            if reply.narration:
                emit(reply.narration)
            if not reply.tool_calls:
                return
            results = [(call, execute(call.name, call.input)) for call in reply.tool_calls]
            self._record(state, reply, results)
        raise TurnExhausted("the DM pauses to gather their thoughts -- try again")


class AnthropicProvider(ToolLoopProvider):
    name = "anthropic"
    default_model = "claude-opus-5"

    def __init__(self, model=None):
        self.model = model or self.default_model

    def available(self):
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False
        return bool(os.environ.get("ANTHROPIC_API_KEY"))

    def _client(self):
        import anthropic

        if not hasattr(self, "_cached_client"):
            self._cached_client = anthropic.Anthropic()
        return self._cached_client

    def _begin(self, system, user_message, tools):
        return {
            # Block form so the prefix can carry a cache breakpoint. Tools render
            # before system, so one marker here caches both -- roughly 2.4k tokens
            # that would otherwise be re-sent on every iteration of the tool loop,
            # and a combat round can spend a dozen of those.
            "system": [{"type": "text", "text": system,
                        "cache_control": {"type": "ephemeral"}}],
            "tools": [dict(t) for t in tools],
            "messages": [{"role": "user", "content": user_message}],
        }

    def _step(self, state):
        import anthropic

        try:
            response = self._client().messages.create(
                model=self.model,
                # This model thinks by default and max_tokens caps thinking and
                # prose together, so 1024 truncated narration and sometimes cut a
                # tool_use block in half. Thinking stays ON deliberately: with it
                # off the model can write a tool call as ordinary text, which the
                # loop below would never see -- and a DM that skips the call is a
                # DM describing a roll the engine never made.
                max_tokens=8192,
                thinking={"type": "adaptive"},
                output_config={"effort": "medium"},
                system=state["system"],
                tools=state["tools"],
                messages=state["messages"],
            )
        except anthropic.BadRequestError:
            # Our own malformed request -- a bug, not a mute voice. Surfacing it
            # keeps the chain from demoting this provider over something that
            # would break every other one in exactly the same way.
            raise
        except anthropic.APIError as e:
            raise ProviderUnavailable(_anthropic_error(e)) from e

        # Both of these arrive as a perfectly successful response with empty or
        # half-finished content. Left unchecked the turn reads as one where the
        # DM simply said nothing, and the table is told it succeeded.
        if response.stop_reason == "refusal":
            raise ProviderUnavailable("Claude declined to narrate this turn")
        if response.stop_reason == "max_tokens":
            raise ProviderUnavailable("Claude's reply ran past its token budget")

        narration = "\n".join(b.text for b in response.content if b.type == "text").strip()
        calls = [
            ToolCall(b.id, b.name, b.input) for b in response.content if b.type == "tool_use"
        ]
        return Reply(narration, calls, raw=response)

    def _record(self, state, reply, results):
        state["messages"].append({"role": "assistant", "content": reply.raw.content})
        state["messages"].append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": call.id, "content": json.dumps(result)}
                for call, result in results
            ],
        })


def _anthropic_error(e):
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return error["message"]
    return getattr(e, "message", str(e))


class OpenAIProvider(ToolLoopProvider):
    """Spoken over plain REST rather than the SDK.

    chat/completions is stable and `requests` is already a dependency, so this
    costs the project no new package for a voice it may not even use.
    """

    name = "openai"
    default_model = "gpt-5.4-mini"
    endpoint = "https://api.openai.com/v1/chat/completions"

    def __init__(self, model=None):
        self.model = model or self.default_model

    def available(self):
        return bool(os.environ.get("OPENAI_API_KEY"))

    def _begin(self, system, user_message, tools):
        return {
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["input_schema"],
                    },
                }
                for t in tools
            ],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_message},
            ],
        }

    def _step(self, state):
        try:
            response = requests.post(
                self.endpoint,
                headers={"Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', '')}"},
                json={
                    "model": self.model,
                    "messages": state["messages"],
                    "tools": state["tools"],
                },
                timeout=120,
            )
        except requests.RequestException as e:
            raise ProviderUnavailable(str(e)) from e

        body = response.json()
        if "error" in body:
            raise ProviderUnavailable(body["error"].get("message", "openai error"))

        message = body["choices"][0]["message"]
        calls = []
        for call in message.get("tool_calls") or []:
            try:
                args = json.loads(call["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(call["id"], call["function"]["name"], args))
        return Reply((message.get("content") or "").strip(), calls, raw=message)

    def _record(self, state, reply, results):
        state["messages"].append(reply.raw)
        for call, result in results:
            state["messages"].append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(result),
            })


class GeminiProvider(ToolLoopProvider):
    name = "gemini"
    # Deliberately not pinned to a guessed id -- see available(). Override with
    # NOVA_DM_LLM_MODEL, or confirm against client.models.list() once a key exists.
    default_model = os.environ.get("NOVA_DM_GEMINI_MODEL", "gemini-2.0-flash")

    def __init__(self, model=None):
        self.model = model or self.default_model

    def available(self):
        try:
            from google import genai  # noqa: F401
        except ImportError:
            return False
        return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))

    def _client(self):
        from google import genai

        if not hasattr(self, "_cached_client"):
            key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            self._cached_client = genai.Client(api_key=key)
        return self._cached_client

    def _begin(self, system, user_message, tools):
        return {
            "system": system,
            "declarations": [
                {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": _strip_unsupported(t["input_schema"]),
                }
                for t in tools
            ],
            "contents": [{"role": "user", "parts": [{"text": user_message}]}],
        }

    def _step(self, state):
        from google.genai import types

        try:
            response = self._client().models.generate_content(
                model=self.model,
                contents=state["contents"],
                config=types.GenerateContentConfig(
                    system_instruction=state["system"],
                    tools=[types.Tool(function_declarations=state["declarations"])],
                ),
            )
        except Exception as e:
            raise ProviderUnavailable(str(e)) from e

        narration, calls = "", []
        candidates = getattr(response, "candidates", None) or []
        for part in (candidates[0].content.parts if candidates else []):
            if getattr(part, "text", None):
                narration += part.text
            fn = getattr(part, "function_call", None)
            if fn:
                calls.append(ToolCall(fn.name, fn.name, dict(fn.args or {})))
        return Reply(narration.strip(), calls, raw=response)

    def _record(self, state, reply, results):
        state["contents"].append({
            "role": "model",
            "parts": [
                {"function_call": {"name": call.name, "args": call.input}}
                for call, _ in results
            ],
        })
        state["contents"].append({
            "role": "user",
            "parts": [
                {"function_response": {"name": call.name, "response": _wrap(result)}}
                for call, result in results
            ],
        })


def _wrap(result):
    """Gemini wants a function response to be an object, not a bare scalar."""
    return result if isinstance(result, dict) else {"result": result}


def _strip_unsupported(schema):
    """Gemini's schema dialect rejects some JSON Schema keywords outright."""
    if not isinstance(schema, dict):
        return schema
    out = {}
    for key, value in schema.items():
        if key in ("additionalProperties", "$schema", "title"):
            continue
        if key == "properties":
            out[key] = {k: _strip_unsupported(v) for k, v in value.items()}
        elif key == "items":
            out[key] = _strip_unsupported(value)
        else:
            out[key] = value
    return out


# --------------------------------------------------------------------------
# JSON-plan provider (local models that cannot call tools)
# --------------------------------------------------------------------------

INTENT_INSTRUCTIONS = """
You are deciding ONLY what the engine must roll or apply. Do not write any prose
and do not state or imply any outcome -- you have not been told the results yet.
Reply with a list of intents, each naming one tool and its arguments. If the
action needs no dice and changes no hit points, reply with an empty list.
"""

NARRATION_INSTRUCTIONS = """
Below are the real results the engine produced. Narrate what happens, in second
person, a paragraph or two, evocative but not padded. The numbers are already
decided -- describe them faithfully, never contradict them, and never introduce a
roll or an HP change that is not listed.
"""


class OllamaToolProvider(ToolLoopProvider):
    """A local model that can actually call tools.

    OllamaProvider below exists because the local models available when Phase 8
    was written could not: Ollama refuses tools outright for gemma3 and
    deepseek-r1, and llama3.2:1b invents its own argument names. qwen3 does not
    need that crutch -- it emits real tool_calls with the right argument names --
    so it runs the same loop the hosted voices do, and the engine keeps the dice
    for the same reason rather than a weaker one.

    Two dialect quirks against the OpenAI shape: Ollama hands back `arguments`
    already parsed as an object rather than a JSON string, and it identifies a
    tool result by name rather than by a call id.
    """

    name = "ollama-tools"
    default_model = "qwen3:4b"
    keep_alive = os.environ.get("NOVA_DM_OLLAMA_KEEP_ALIVE", "1h")

    def __init__(self, model=None, host=None):
        self.model = model or self.default_model
        self.host = host or OLLAMA_HOST

    def available(self):
        try:
            response = requests.get(f"{self.host}/api/tags", timeout=5)
            names = [m["name"] for m in response.json().get("models", [])]
        except (requests.RequestException, ValueError, KeyError):
            return False
        return self.model in names

    def _begin(self, system, user_message, tools):
        return {
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["input_schema"],
                    },
                }
                for t in tools
            ],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_message},
            ],
        }

    def _step(self, state):
        try:
            response = requests.post(
                f"{self.host}/api/chat",
                json={
                    "model": self.model,
                    "messages": state["messages"],
                    "tools": state["tools"],
                    "stream": False,
                    "keep_alive": self.keep_alive,
                    # qwen3 reasons by default and this box is slow enough that
                    # thinking tokens cost real seconds at the table. Any leak
                    # past this is stripped below rather than read aloud.
                    "think": False,
                },
                timeout=600,
            )
        except requests.RequestException as e:
            raise ProviderUnavailable(str(e)) from e

        body = response.json()
        if "error" in body:
            raise ProviderUnavailable(body["error"])

        message = body.get("message") or {}
        calls = []
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):  # some builds still send a JSON string
                try:
                    args = json.loads(args or "{}")
                except json.JSONDecodeError:
                    args = {}
            calls.append(ToolCall(call.get("id") or fn.get("name"), fn.get("name"), args or {}))

        return Reply(_strip_thinking(message.get("content")), calls, raw=message)

    def _record(self, state, reply, results):
        state["messages"].append(reply.raw)
        for call, result in results:
            state["messages"].append({
                "role": "tool",
                "tool_name": call.name,
                "content": json.dumps(result),
            })


def _strip_thinking(content):
    """Drop a reasoning block a local model left in its prose.

    Narration goes to a text-to-speech engine and is read to the table, so
    "Okay, the user wants me to describe..." is not a cosmetic problem -- it is
    the DM saying it out loud.
    """
    text = content or ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"^\s*<think>.*", "", text, flags=re.DOTALL | re.IGNORECASE)
    return text.strip()


class OllamaProvider:
    """Two passes, because one pass lets the model narrate before the dice exist.

    Asked in a single shot, gemma3 cheerfully wrote a character sticking a
    landing it had not rolled for. Withholding the narration field until the
    engine has actually rolled is what makes that impossible rather than merely
    discouraged.
    """

    name = "ollama"
    # What the engine already knows and need not ask the model for; run_turn
    # sets it per turn. An attribute rather than an argument because only this
    # path validates intents -- the tool-calling providers hand arguments
    # straight to execute -- and widening five run_turn signatures for one of
    # them would be worse than this.
    defaults: dict = {}
    # Per-pass system prompts, set by run_turn. Falls back to the single
    # `system` string so this provider still works when called directly.
    prompts: dict = {}
    # Optionally, a second model writes the prose.
    #
    # The two passes want different things. Planning is clerical -- name the
    # rolls, get the argument names right -- and a 1B model does it in about a
    # minute. Narration is the part anyone at the table actually experiences,
    # and a 1B model writes it thinly.
    #
    # What makes the split affordable is that the narration prompt carries no
    # tool schemas and no mechanics: 328 tokens against planning's 1170. A model
    # that costs 14 minutes on the planning prompt costs about two on this one,
    # which turns "unusably slow" into a trade worth offering.
    #
    # Unset by default: it roughly doubles a turn, and that is the table's call.
    narrator_model = os.environ.get("NOVA_DM_NARRATOR") or None
    # The fastest thing on this box that can hold a scene together. gemma3 is
    # better prose and 4x the wait; see DEFAULT_CHAIN for the measurements.
    default_model = "llama3.2:1b"
    # Ollama drops a model from memory after 5 idle minutes by default, and
    # reloading gemma3 off this box's disk measured at 69 seconds -- longer than
    # the turn itself. Asking per request keeps it resident without anyone
    # having to edit the system service.
    keep_alive = os.environ.get("NOVA_DM_OLLAMA_KEEP_ALIVE", "1h")

    def __init__(self, model=None, host=None):
        self.model = model or self.default_model
        self.host = host or OLLAMA_HOST

    def available(self):
        try:
            response = requests.get(f"{self.host}/api/tags", timeout=5)
            names = [m["name"] for m in response.json().get("models", [])]
        except (requests.RequestException, ValueError, KeyError):
            return False
        return self.model in names

    def _chat(self, system, user, schema, model=None):
        try:
            response = requests.post(
                f"{self.host}/api/chat",
                json={
                    "model": model or self.model,
                    "stream": False,
                    "format": schema,
                    "keep_alive": self.keep_alive,
                    "options": {"temperature": 0.7, "num_predict": 600},
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
                timeout=OLLAMA_TIMEOUT,
            )
        except requests.RequestException as e:
            raise ProviderUnavailable(str(e)) from e

        body = response.json()
        if "error" in body:
            raise ProviderUnavailable(body["error"])
        try:
            return json.loads(body["message"]["content"])
        except (KeyError, json.JSONDecodeError) as e:
            raise ProviderUnavailable(f"unparseable reply: {e}") from e

    def run_turn(self, system, user_message, tools, execute, emit):
        by_name = {t["name"]: t for t in tools}

        plan = self._chat(
            self.prompts.get("planning", system) + "\n" + INTENT_INSTRUCTIONS
            + tool_signatures(tools),
            user_message + "\n\nWhich tools must the engine run?",
            intent_schema(tools),
        )

        results = []
        for intent in plan.get("intents") or []:
            call = intent_to_call(intent, by_name, self.defaults)
            if call is None:
                # The engine is the referee: a malformed or unknown intent is
                # dropped, not guessed at. gemma3 reaches for the wrong tool
                # often enough that this is load-bearing, not defensive padding.
                continue
            results.append((call.name, execute(call.name, call.input)))

        if results:
            transcript = "\n".join(f"- {name}: {json.dumps(r)}" for name, r in results)
        else:
            transcript = "(no rolls were needed)"

        told = self._chat(
            self.prompts.get("narration", system) + "\n" + NARRATION_INSTRUCTIONS,
            f"{user_message}\n\nEngine results:\n{transcript}",
            {
                "type": "object",
                "properties": {"narration": {"type": "string"}},
                "required": ["narration"],
            },
            model=self.narrator_model,
        )
        narration = (told.get("narration") or "").strip()
        if narration:
            emit(narration)


def tool_signatures(tools):
    """One line per tool naming its arguments, e.g. `roll_dice(expr)`.

    The flat schema below deliberately offers every tool's properties to every
    intent, and marks only `tool` as required -- which leaves a small model free
    to name the right tool and then fill in a plausible-looking argument
    belonging to a different one. Observed exactly that: llama3.2 asked for
    roll_dice and supplied `name`, which start_encounter owns, so the intent was
    dropped as malformed and the lock was never rolled for.

    The tool descriptions say what each tool is for but never what it takes, so
    this is the only place the model is told. Cheap at roughly sixty tokens, and
    generated from the schema so it cannot drift away from what is enforced.
    """
    lines = []
    for tool in tools:
        schema = tool["input_schema"]
        required = schema.get("required", [])
        optional = [k for k in schema.get("properties", {}) if k not in required]
        args = ", ".join(required + [f"[{k}]" for k in optional])
        lines.append(f"  {tool['name']}({args})")
    return ("\nEach tool takes exactly these arguments, and square brackets mark the "
            "optional ones. Use no others:\n" + "\n".join(lines) + "\n")


def intent_schema(tools):
    """One flat schema covering every tool's arguments.

    Ollama constrains generation with a grammar built from this, and small
    models follow a flat object far more reliably than a per-tool union. The
    price is that a given intent may carry keys belonging to another tool --
    intent_to_call throws those away.
    """
    properties = {"tool": {"type": "string", "enum": [t["name"] for t in tools]}}
    for tool in tools:
        for key, spec in tool["input_schema"].get("properties", {}).items():
            properties.setdefault(key, spec)
    return {
        "type": "object",
        "properties": {
            "intents": {
                "type": "array",
                "items": {"type": "object", "properties": properties, "required": ["tool"]},
            }
        },
        "required": ["intents"],
    }


def intent_to_call(intent, by_name, defaults=None):
    """Narrow a flat intent back to one tool's real arguments, or reject it.

    `defaults` fills in what the engine already knows and the model keeps
    forgetting. Chiefly character_id: the acting character is not in doubt --
    the engine was handed it before the model was asked anything -- so making a
    1B model echo it back correctly is clerical work it is measurably bad at,
    and dropping an otherwise perfect roll_check over it costs the table a roll.
    A value the model did supply always wins; this only fills a gap.
    """
    if not isinstance(intent, dict):
        return None
    tool = by_name.get(intent.get("tool"))
    if tool is None:
        return None
    schema = tool["input_schema"]
    allowed = schema.get("properties", {})
    args = {k: v for k, v in intent.items() if k != "tool" and k in allowed}

    for field, value in (defaults or {}).items():
        if field in allowed and field not in args:
            args[field] = value

    if any(field not in args for field in schema.get("required", [])):
        return None

    # A dice expression the engine cannot parse is not a roll, whatever the
    # model called it -- rejecting it here keeps a nonsense roll out of the
    # narration rather than raising from inside the tool.
    if "expr" in args and not dice.is_valid(args["expr"]):
        return None

    return ToolCall(tool["name"], tool["name"], args)


# --------------------------------------------------------------------------
# Registry and fallback
# --------------------------------------------------------------------------

class OllamaGemmaProvider(OllamaProvider):
    """The same two-pass path, on gemma3.

    A separate registry name purely so the chain can name a second local model:
    NOVA_DM_LLM_MODEL is global, so without this there is no way to say "try
    llama3.2, then gemma3" in one chain.
    """

    name = "ollama-gemma"
    default_model = "gemma3:4b"


PROVIDERS = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "gemini": GeminiProvider,
    "ollama-tools": OllamaToolProvider,
    "ollama": OllamaProvider,
    "ollama-gemma": OllamaGemmaProvider,
}

_demoted = set()


def reset_availability():
    """Forget which voices failed. Called between turns by the tests."""
    _demoted.clear()


def build(name, model=None):
    return PROVIDERS[name](model=model or os.environ.get("NOVA_DM_LLM_MODEL"))


def chain():
    """The voices to try, in order, for this turn."""
    configured = os.environ.get("NOVA_DM_LLM_PROVIDER", "auto").strip().lower()
    if configured and configured != "auto":
        # An explicit choice is honoured exactly: if it fails the caller should
        # see why, not be quietly handed a different DM.
        return [configured]
    override = os.environ.get("NOVA_DM_LLM_CHAIN")
    return [n.strip() for n in override.split(",")] if override else list(DEFAULT_CHAIN)


def run_turn(system, user_message, tools, execute, emit, on_provider=None, defaults=None,
             prompts=None):
    """Ask each candidate voice in turn until one narrates the turn.

    A voice that has already emitted narration is never abandoned mid-turn --
    handing over at that point would narrate the same action twice, in two
    different styles, which is worse for the table than an honest error.
    """
    last_error = None

    for name in chain():
        if name in _demoted or name not in PROVIDERS:
            continue
        provider = build(name)
        provider.defaults = defaults or {}
        # Optional per-pass system prompts. Only the two-pass path can use them;
        # everyone else narrates and decides in one conversation and keeps
        # `system` whole.
        provider.prompts = prompts or {}
        if not provider.available():
            _demoted.add(name)
            continue

        spoken = []

        def record(text, _spoken=spoken):
            _spoken.append(text)
            emit(text)

        if on_provider:
            on_provider(name)
        try:
            provider.run_turn(system, user_message, tools, execute, record)
            return Outcome(name)
        except TurnExhausted as e:
            return Outcome(name, str(e))
        except ProviderUnavailable as e:
            last_error = f"{name}: {e}"
            if spoken:
                return Outcome(name, f"the DM's voice falters: {e}")
            _demoted.add(name)

    return Outcome(None, f"no DM voice is available right now: {last_error or 'none configured'}")
