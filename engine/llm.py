"""Phase 8: the Accord -- the DM's voice is pluggable.

Phase 3 wired the DM straight into one vendor's client. That was fine until the
key ran dry, at which point the whole table stopped: a credit error and nobody
could play. This module puts a seam there instead, so several models can take
the DM's chair and a dead one steps aside rather than ending the session.

Two kinds of model can sit in that chair, and they need genuinely different
handling:

  * Models that call tools natively (Claude, GPT, Gemini) run the Phase 3 loop
    unchanged -- narrate, call a tool, see the real result, narrate again.

  * Local models mostly cannot. Ollama refuses tools outright for gemma3 and
    deepseek-r1, and llama3.2:1b accepts them only to invent its own argument
    names. For those there is the JSON-plan path in OllamaProvider, which is
    the design the original spec called for: the model names the rolls it wants,
    the engine makes them, and only then does the model get to describe what
    happened.

What survives across both, and is the whole point of the seam, is the Phase 3
rule: the model never invents a die roll, a check result, or an HP total. In the
tool path the engine owns the dice because the model must call for them. In the
JSON-plan path it owns them because the model is asked for intentions before it
is allowed to write a single word of prose -- it cannot describe a landing it
has not been told the character stuck.
"""
import json
import os

import requests

# A combat round legitimately spends several calls (attack, damage, advance
# turn), so this has to be generous enough to carry a whole exchange.
MAX_TOOL_ITERATIONS = 12

# Tried in this order when NOVA_DM_LLM_PROVIDER is unset or "auto". Hosted
# models first for quality, the local one last because it is the floor that is
# always there -- no key, no bill, no network.
DEFAULT_CHAIN = ["anthropic", "openai", "gemini", "ollama"]

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")


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
            "system": system,
            "tools": [dict(t) for t in tools],
            "messages": [{"role": "user", "content": user_message}],
        }

    def _step(self, state):
        import anthropic

        try:
            response = self._client().messages.create(
                model=self.model,
                max_tokens=1024,
                system=state["system"],
                tools=state["tools"],
                messages=state["messages"],
            )
        except anthropic.APIError as e:
            raise ProviderUnavailable(_anthropic_error(e)) from e

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


class OllamaProvider:
    """Two passes, because one pass lets the model narrate before the dice exist.

    Asked in a single shot, gemma3 cheerfully wrote a character sticking a
    landing it had not rolled for. Withholding the narration field until the
    engine has actually rolled is what makes that impossible rather than merely
    discouraged.
    """

    name = "ollama"
    default_model = "gemma3:4b"
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

    def _chat(self, system, user, schema):
        try:
            response = requests.post(
                f"{self.host}/api/chat",
                json={
                    "model": self.model,
                    "stream": False,
                    "format": schema,
                    "keep_alive": self.keep_alive,
                    "options": {"temperature": 0.7, "num_predict": 600},
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
                timeout=300,
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
            system + "\n" + INTENT_INSTRUCTIONS,
            user_message + "\n\nWhich tools must the engine run?",
            intent_schema(tools),
        )

        results = []
        for intent in plan.get("intents") or []:
            call = intent_to_call(intent, by_name)
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
            system + "\n" + NARRATION_INSTRUCTIONS,
            f"{user_message}\n\nEngine results:\n{transcript}",
            {
                "type": "object",
                "properties": {"narration": {"type": "string"}},
                "required": ["narration"],
            },
        )
        narration = (told.get("narration") or "").strip()
        if narration:
            emit(narration)


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


def intent_to_call(intent, by_name):
    """Narrow a flat intent back to one tool's real arguments, or reject it."""
    if not isinstance(intent, dict):
        return None
    tool = by_name.get(intent.get("tool"))
    if tool is None:
        return None
    schema = tool["input_schema"]
    allowed = schema.get("properties", {})
    args = {k: v for k, v in intent.items() if k != "tool" and k in allowed}
    if any(field not in args for field in schema.get("required", [])):
        return None
    return ToolCall(tool["name"], tool["name"], args)


# --------------------------------------------------------------------------
# Registry and fallback
# --------------------------------------------------------------------------

PROVIDERS = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "gemini": GeminiProvider,
    "ollama": OllamaProvider,
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


def run_turn(system, user_message, tools, execute, emit, on_provider=None):
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
