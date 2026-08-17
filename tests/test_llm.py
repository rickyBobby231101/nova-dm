"""Phase 8: the provider seam.

The point of engine.llm is that a dead voice steps aside instead of ending the
session, and that no voice -- local or hosted -- gets to invent a number. These
tests pin both, plus the dialect translation each provider does on the way in
and out.

No network here: hosted providers are exercised through fakes registered in
llm.PROVIDERS, and the Ollama path patches requests.
"""
import json
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import llm

# Bound before any local shadows it -- the fake requests.post below has to take
# a parameter literally named `json` to match the real call signature.
_dumps = json.dumps

TOOLS = [
    {
        "name": "roll_check",
        "description": "roll an ability check",
        "input_schema": {
            "type": "object",
            "properties": {
                "character_id": {"type": "integer"},
                "ability": {"type": "string"},
            },
            "required": ["character_id", "ability"],
        },
    },
    {
        "name": "roll_dice",
        "description": "roll an expression",
        "input_schema": {
            "type": "object",
            "properties": {"expr": {"type": "string"}},
            "required": ["expr"],
        },
    },
]


@pytest.fixture(autouse=True)
def clean_llm_state(monkeypatch):
    """_demoted is module-global and outlives a turn by design -- a voice that
    failed stays out for the rest of the process. Tests must not inherit each
    other's demotions, and must not read the developer's real env."""
    llm.reset_availability()
    for var in ("NOVA_DM_LLM_PROVIDER", "NOVA_DM_LLM_CHAIN", "NOVA_DM_LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    yield
    llm.reset_availability()


class FakeProvider:
    """A voice under test control. `script` is a list of callables run one per
    step, each handed (execute, emit), so a test decides exactly when the engine
    is asked for a number and when prose comes out."""

    name = "fake"
    available_result = True
    script = []
    ran = False

    def __init__(self, model=None):
        self.model = model

    def available(self):
        return self.available_result

    def run_turn(self, system, user_message, tools, execute, emit):
        type(self).ran = True
        for step in self.script:
            step(execute, emit)


def _provider(monkeypatch, name, **attrs):
    """Register a one-off provider class under `name` and return the class."""
    body = {"name": name, "script": [], "ran": False, **attrs}
    cls = type(f"Fake_{name}", (FakeProvider,), body)
    monkeypatch.setitem(llm.PROVIDERS, name, cls)
    return cls


def _chain(monkeypatch, *names):
    monkeypatch.setenv("NOVA_DM_LLM_CHAIN", ",".join(names))


# ---------------------------------------------------------------------------
# chain selection
# ---------------------------------------------------------------------------

def test_default_chain_is_local_only():
    """Runs on hardware Daniel owns: no key, no bill, no network. qwen3's tool
    loop first, the JSON-plan path behind it for models that can't call tools."""
    assert llm.chain() == ["ollama-tools", "ollama"]
    assert "anthropic" not in llm.chain()


def test_hosted_voices_are_still_reachable_when_asked_for(monkeypatch):
    """Local-only is the default, not a removal."""
    monkeypatch.setenv("NOVA_DM_LLM_CHAIN", "anthropic,ollama")
    assert llm.chain() == ["anthropic", "ollama"]
    assert "anthropic" in llm.PROVIDERS


def test_explicit_provider_is_honoured_exactly(monkeypatch):
    # An explicit choice must not quietly hand the table a different DM.
    monkeypatch.setenv("NOVA_DM_LLM_PROVIDER", "ollama")
    assert llm.chain() == ["ollama"]


def test_explicit_provider_tolerates_case_and_padding(monkeypatch):
    monkeypatch.setenv("NOVA_DM_LLM_PROVIDER", "  Ollama  ")
    assert llm.chain() == ["ollama"]


def test_chain_override_applies_when_provider_is_auto(monkeypatch):
    monkeypatch.setenv("NOVA_DM_LLM_PROVIDER", "auto")
    monkeypatch.setenv("NOVA_DM_LLM_CHAIN", "ollama, anthropic")
    assert llm.chain() == ["ollama", "anthropic"]


# ---------------------------------------------------------------------------
# fallback and demotion
# ---------------------------------------------------------------------------

def test_first_available_voice_answers_and_the_rest_stay_silent(monkeypatch):
    _provider(monkeypatch, "first", script=[lambda ex, em: em("the door creaks")])
    second = _provider(monkeypatch, "second", script=[lambda ex, em: em("never spoken")])
    _chain(monkeypatch, "first", "second")

    spoken = []
    outcome = llm.run_turn("sys", "msg", TOOLS, MagicMock(), spoken.append)

    assert outcome.provider == "first"
    assert outcome.error is None
    assert spoken == ["the door creaks"]
    assert second.ran is False


def test_unavailable_voice_is_skipped_and_stays_demoted(monkeypatch):
    _provider(monkeypatch, "dead", available_result=False)
    _provider(monkeypatch, "alive", script=[lambda ex, em: em("torchlight")])
    _chain(monkeypatch, "dead", "alive")

    assert llm.run_turn("sys", "msg", TOOLS, MagicMock(), lambda t: None).provider == "alive"
    assert "dead" in llm._demoted

    # A second turn must not pay to re-check a voice already known to be out.
    assert llm.run_turn("sys", "msg", TOOLS, MagicMock(), lambda t: None).provider == "alive"


def test_provider_unavailable_before_narration_hands_over(monkeypatch):
    def die(execute, emit):
        raise llm.ProviderUnavailable("credit balance is too low")

    _provider(monkeypatch, "broke", script=[die])
    _provider(monkeypatch, "backup", script=[lambda ex, em: em("the local voice picks up")])
    _chain(monkeypatch, "broke", "backup")

    spoken = []
    outcome = llm.run_turn("sys", "msg", TOOLS, MagicMock(), spoken.append)

    assert outcome.provider == "backup"
    assert outcome.error is None
    assert spoken == ["the local voice picks up"]
    assert "broke" in llm._demoted


def test_voice_that_already_narrated_is_never_handed_over(monkeypatch):
    """The load-bearing rule: handing over after prose has reached the table
    narrates the same action twice, in two different styles. An honest error
    beats a double narration."""
    def speak_then_die(execute, emit):
        emit("You push the door open and--")
        raise llm.ProviderUnavailable("connection reset")

    _provider(monkeypatch, "flaky", script=[speak_then_die])
    backup = _provider(monkeypatch, "backup", script=[lambda ex, em: em("SHOULD NOT SPEAK")])
    _chain(monkeypatch, "flaky", "backup")

    spoken = []
    outcome = llm.run_turn("sys", "msg", TOOLS, MagicMock(), spoken.append)

    assert outcome.provider == "flaky"
    assert "falters" in outcome.error
    assert spoken == ["You push the door open and--"]
    assert backup.ran is False


def test_turn_exhausted_does_not_demote_the_voice(monkeypatch):
    """Running long is the turn's fault, not the voice's -- demoting here would
    cost the table a perfectly good DM for the rest of the session."""
    def overrun(execute, emit):
        raise llm.TurnExhausted("the DM pauses to gather their thoughts -- try again")

    _provider(monkeypatch, "long", script=[overrun])
    backup = _provider(monkeypatch, "backup", script=[lambda ex, em: em("unused")])
    _chain(monkeypatch, "long", "backup")

    outcome = llm.run_turn("sys", "msg", TOOLS, MagicMock(), lambda t: None)

    assert outcome.provider == "long"
    assert "gather their thoughts" in outcome.error
    assert "long" not in llm._demoted
    assert backup.ran is False


def test_no_voice_available_reports_rather_than_raising(monkeypatch):
    _provider(monkeypatch, "dead", available_result=False)
    _chain(monkeypatch, "dead")

    outcome = llm.run_turn("sys", "msg", TOOLS, MagicMock(), lambda t: None)
    assert outcome.provider is None
    assert "no DM voice is available" in outcome.error


def test_unknown_provider_name_in_the_chain_is_ignored(monkeypatch):
    _provider(monkeypatch, "real", script=[lambda ex, em: em("hello")])
    _chain(monkeypatch, "nonsense", "real")

    assert llm.run_turn("sys", "msg", TOOLS, MagicMock(), lambda t: None).provider == "real"


def test_the_table_is_told_who_is_speaking_before_the_words_arrive(monkeypatch):
    events = []

    def narrate_step(execute, emit):
        emit("text")

    _provider(monkeypatch, "voice", script=[narrate_step])
    _chain(monkeypatch, "voice")

    llm.run_turn("sys", "msg", TOOLS, MagicMock(),
                 lambda t: events.append(("narrated", t)),
                 on_provider=lambda n: events.append(("announced", n)))

    assert events == [("announced", "voice"), ("narrated", "text")]


# ---------------------------------------------------------------------------
# the shared tool loop
# ---------------------------------------------------------------------------

class ScriptedToolProvider(llm.ToolLoopProvider):
    """Drives the real ToolLoopProvider loop with canned replies, so the loop's
    own ordering guarantee is what is under test rather than a fake of it.
    The last reply repeats, which is how the runaway case is built."""

    name = "scripted"
    replies = []

    def __init__(self, model=None):
        self.model = model
        self._i = 0

    def available(self):
        return True

    def _begin(self, system, user_message, tools):
        return {}

    def _step(self, state):
        reply = self.replies[min(self._i, len(self.replies) - 1)]
        self._i += 1
        return reply

    def _record(self, state, reply, results):
        pass


def _scripted(monkeypatch, replies):
    cls = type("Scripted", (ScriptedToolProvider,), {"replies": replies})
    monkeypatch.setitem(llm.PROVIDERS, "scripted", cls)
    _chain(monkeypatch, "scripted")


def test_tool_results_are_seen_before_the_next_narration(monkeypatch):
    """The engine rolls, then the model narrates -- never the reverse."""
    order = []
    _scripted(monkeypatch, [
        llm.Reply("", [llm.ToolCall("t1", "roll_check", {"character_id": 1, "ability": "str"})]),
        llm.Reply("The blow lands.", []),
    ])

    def execute(name, args):
        order.append(("executed", name))
        return {"total": 17}

    outcome = llm.run_turn("sys", "msg", TOOLS, execute,
                           lambda t: order.append(("narrated", t)))

    assert outcome.provider == "scripted"
    assert order == [("executed", "roll_check"), ("narrated", "The blow lands.")]


def test_loop_stops_when_no_tools_are_requested(monkeypatch):
    _scripted(monkeypatch, [llm.Reply("Nothing to roll for.", [])])

    execute = MagicMock()
    llm.run_turn("sys", "msg", TOOLS, execute, lambda t: None)
    execute.assert_not_called()


def test_endless_tool_calls_end_the_turn_instead_of_hanging_the_table(monkeypatch):
    _scripted(monkeypatch, [llm.Reply("", [llm.ToolCall("t", "roll_dice", {"expr": "1d6"})])])

    calls = []
    outcome = llm.run_turn("sys", "msg", TOOLS,
                           lambda n, a: calls.append(n) or {}, lambda t: None)

    assert len(calls) == llm.MAX_TOOL_ITERATIONS
    assert "gather their thoughts" in outcome.error


# ---------------------------------------------------------------------------
# Ollama: the JSON-plan path for models that cannot call tools
# ---------------------------------------------------------------------------

def test_intent_schema_flattens_every_tool_argument():
    props = llm.intent_schema(TOOLS)["properties"]["intents"]["items"]["properties"]

    # One flat object -- small models follow that far better than a per-tool union.
    assert set(props) == {"tool", "character_id", "ability", "expr"}
    assert props["tool"]["enum"] == ["roll_check", "roll_dice"]


def test_intent_to_call_drops_arguments_belonging_to_another_tool():
    by_name = {t["name"]: t for t in TOOLS}
    call = llm.intent_to_call(
        {"tool": "roll_dice", "expr": "1d20", "ability": "str", "character_id": 3}, by_name
    )
    assert call.name == "roll_dice"
    assert call.input == {"expr": "1d20"}


def test_intent_to_call_rejects_unknown_or_incomplete_intents():
    by_name = {t["name"]: t for t in TOOLS}
    # The engine is the referee: a bad intent is dropped, never guessed at.
    assert llm.intent_to_call({"tool": "cast_fireball"}, by_name) is None
    assert llm.intent_to_call({"tool": "roll_check", "ability": "str"}, by_name) is None
    assert llm.intent_to_call("not a dict", by_name) is None
    assert llm.intent_to_call({}, by_name) is None


def _fake_ollama(monkeypatch, bodies):
    """Queue JSON bodies for the two /api/chat calls a turn makes, and capture
    what was sent so the prompts themselves can be asserted on."""
    sent = []

    def fake_post(url, json=None, timeout=None):
        sent.append(json)
        return MagicMock(json=lambda: {"message": {"content": _dumps(bodies[len(sent) - 1])}})

    monkeypatch.setattr(llm.requests, "post", fake_post)
    return sent


def test_ollama_rolls_before_it_narrates(monkeypatch):
    """The two-pass design exists because one pass let gemma3 describe a landing
    it had never rolled for."""
    order = []
    sent = _fake_ollama(monkeypatch, [
        {"intents": [{"tool": "roll_dice", "expr": "1d20"}]},
        {"narration": "You vault the gap and land hard."},
    ])

    def execute(name, args):
        order.append(("executed", name))
        return {"total": 14}

    llm.OllamaProvider().run_turn("sys", "I jump the chasm", TOOLS, execute,
                                  lambda t: order.append(("narrated", t)))

    assert order == [("executed", "roll_dice"), ("narrated", "You vault the gap and land hard.")]
    # The narration pass is handed the real result, not asked to imagine one.
    assert "14" in sent[1]["messages"][-1]["content"]


def test_ollama_is_told_not_to_write_prose_on_the_planning_pass(monkeypatch):
    sent = _fake_ollama(monkeypatch, [{"intents": []}, {"narration": "ok"}])
    llm.OllamaProvider().run_turn("sys", "msg", TOOLS, MagicMock(), lambda t: None)

    plan_system = sent[0]["messages"][0]["content"]
    assert "Do not write any prose" in plan_system
    # and the planning pass is constrained to the intent grammar, not free text
    assert sent[0]["format"]["properties"]["intents"]["type"] == "array"


def test_ollama_narrates_when_no_roll_is_needed(monkeypatch):
    sent = _fake_ollama(monkeypatch, [
        {"intents": []},
        {"narration": "You look around the empty hall."},
    ])

    spoken = []
    llm.OllamaProvider().run_turn("sys", "I look around", TOOLS, MagicMock(), spoken.append)

    assert spoken == ["You look around the empty hall."]
    assert "no rolls were needed" in sent[1]["messages"][-1]["content"]


def test_ollama_drops_an_intent_naming_a_tool_that_does_not_exist(monkeypatch):
    _fake_ollama(monkeypatch, [
        {"intents": [{"tool": "summon_dragon"}, {"tool": "roll_dice", "expr": "1d6"}]},
        {"narration": "It happens."},
    ])

    executed = []
    llm.OllamaProvider().run_turn("sys", "msg", TOOLS,
                                  lambda n, a: executed.append(n) or {"total": 3},
                                  lambda t: None)

    assert executed == ["roll_dice"]


def test_ollama_emits_nothing_when_the_model_returns_empty_narration(monkeypatch):
    _fake_ollama(monkeypatch, [{"intents": []}, {"narration": "   "}])

    spoken = []
    llm.OllamaProvider().run_turn("sys", "msg", TOOLS, MagicMock(), spoken.append)
    assert spoken == []


def test_ollama_unparseable_reply_is_reported_as_an_unavailable_voice(monkeypatch):
    """Garbage from a local model must look like a dead voice so the chain steps
    over it, not like a bug in the engine."""
    monkeypatch.setattr(
        llm.requests, "post",
        lambda url, json=None, timeout=None: MagicMock(
            json=lambda: {"message": {"content": "this is not json"}}
        ),
    )

    with pytest.raises(llm.ProviderUnavailable):
        llm.OllamaProvider().run_turn("sys", "msg", TOOLS, MagicMock(), lambda t: None)


def test_ollama_server_error_is_reported_as_an_unavailable_voice(monkeypatch):
    monkeypatch.setattr(
        llm.requests, "post",
        lambda url, json=None, timeout=None: MagicMock(json=lambda: {"error": "model not found"}),
    )

    with pytest.raises(llm.ProviderUnavailable, match="model not found"):
        llm.OllamaProvider().run_turn("sys", "msg", TOOLS, MagicMock(), lambda t: None)


def test_ollama_asks_to_keep_the_model_resident(monkeypatch):
    # Reloading gemma3 off this box's disk measured at 69s -- longer than the turn.
    sent = _fake_ollama(monkeypatch, [{"intents": []}, {"narration": "ok"}])
    provider = llm.OllamaProvider()
    provider.run_turn("sys", "msg", TOOLS, MagicMock(), lambda t: None)

    assert sent[0]["keep_alive"] == provider.keep_alive
    assert sent[0]["stream"] is False


def test_ollama_reports_unavailable_when_the_server_is_not_there(monkeypatch):
    monkeypatch.setattr(llm.requests, "get", MagicMock(side_effect=llm.requests.RequestException()))
    assert llm.OllamaProvider().available() is False


# ---------------------------------------------------------------------------
# dialect translation
# ---------------------------------------------------------------------------

def test_anthropic_error_prefers_the_api_message():
    class E(Exception):
        body = {"error": {"message": "credit balance is too low"}}
        message = "generic"

    assert llm._anthropic_error(E()) == "credit balance is too low"


def test_anthropic_error_falls_back_when_body_is_not_a_dict():
    class E(Exception):
        body = None
        message = "connection error"

    assert llm._anthropic_error(E()) == "connection error"


def test_gemini_schema_strips_keywords_its_dialect_rejects():
    cleaned = llm._strip_unsupported({
        "type": "object",
        "additionalProperties": False,
        "title": "roll",
        "properties": {"expr": {"type": "string", "title": "expression"}},
        "items": {"$schema": "http://json-schema.org/draft-07/schema#", "type": "string"},
    })

    assert "additionalProperties" not in cleaned
    assert "title" not in cleaned
    assert "title" not in cleaned["properties"]["expr"]
    assert "$schema" not in cleaned["items"]
    assert cleaned["properties"]["expr"]["type"] == "string"


def test_gemini_wraps_scalar_results_but_leaves_objects_alone():
    assert llm._wrap(7) == {"result": 7}
    assert llm._wrap({"total": 7}) == {"total": 7}


def test_openai_translates_tools_into_function_shape():
    state = llm.OpenAIProvider()._begin("sys", "msg", TOOLS)

    assert state["tools"][0]["type"] == "function"
    assert state["tools"][0]["function"]["name"] == "roll_check"
    assert state["tools"][0]["function"]["parameters"] == TOOLS[0]["input_schema"]
    assert state["messages"][0] == {"role": "system", "content": "sys"}


def test_openai_records_each_tool_result_as_its_own_message():
    provider = llm.OpenAIProvider()
    state = provider._begin("sys", "msg", TOOLS)
    call = llm.ToolCall("call_1", "roll_dice", {"expr": "1d6"})
    reply = llm.Reply("", [call], raw={"role": "assistant"})

    provider._record(state, reply, [(call, {"total": 4})])

    tool_messages = [m for m in state["messages"] if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call_1"
    assert json.loads(tool_messages[0]["content"]) == {"total": 4}


# ---------------------------------------------------------------------------
# OllamaToolProvider -- a local model that calls tools for real
# ---------------------------------------------------------------------------

def _fake_ollama_chat(monkeypatch, messages):
    """Script /api/chat replies. Each entry is the `message` object Ollama
    would return."""
    sent = []

    def fake_post(url, json=None, timeout=None):
        sent.append(json)
        return MagicMock(json=lambda: {"message": messages[len(sent) - 1]})

    monkeypatch.setattr(llm.requests, "post", fake_post)
    return sent


def test_qwen_calls_a_tool_and_narrates_from_the_real_result(monkeypatch):
    """The whole reason this provider exists: the engine rolls, not the model."""
    sent = _fake_ollama_chat(monkeypatch, [
        {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "roll_check",
                                      "arguments": {"character_id": 1, "ability": "dex"}}}]},
        {"role": "assistant", "content": "The rogue lands it, barely."},
    ])
    said, rolled = [], []

    def execute(name, args):
        rolled.append((name, args))
        return {"total": 18}

    llm.OllamaToolProvider().run_turn("SYSTEM", "I leap the gap.", TOOLS, execute, said.append)

    assert rolled == [("roll_check", {"character_id": 1, "ability": "dex"})]
    assert said == ["The rogue lands it, barely."]
    # the tool result must be in the conversation before the narrating call
    tool_msgs = [m for m in sent[1]["messages"] if m["role"] == "tool"]
    assert tool_msgs and json.loads(tool_msgs[0]["content"]) == {"total": 18}
    assert tool_msgs[0]["tool_name"] == "roll_check"


def test_arguments_are_accepted_as_an_object_or_a_json_string(monkeypatch):
    """Ollama sends a parsed object; some builds still send a string."""
    _fake_ollama_chat(monkeypatch, [
        {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "roll_check", "arguments": '{"ability": "str"}'}}]},
        {"role": "assistant", "content": "Done."},
    ])
    seen = []
    llm.OllamaToolProvider().run_turn("S", "U", TOOLS, lambda n, a: seen.append(a) or {}, lambda t: None)
    assert seen == [{"ability": "str"}]


def test_a_leaked_reasoning_block_is_never_read_aloud(monkeypatch):
    """qwen3 reasons by default and it leaks into prose. Narration goes to a
    speaker, so this is the DM saying 'Okay, the user wants me to...' out loud."""
    _fake_ollama_chat(monkeypatch, [
        {"role": "assistant",
         "content": "<think>Okay, they want a goblin. Let me be vivid.</think>The goblin lunges."},
    ])
    said = []
    llm.OllamaToolProvider().run_turn("S", "U", TOOLS, lambda n, a: {}, said.append)
    assert said == ["The goblin lunges."]


def test_thinking_is_asked_to_stay_off(monkeypatch):
    sent = _fake_ollama_chat(monkeypatch, [{"role": "assistant", "content": "Fine."}])
    llm.OllamaToolProvider().run_turn("S", "U", TOOLS, lambda n, a: {}, lambda t: None)
    assert sent[0]["think"] is False
    assert sent[0]["stream"] is False


def test_an_ollama_error_hands_over_rather_than_crashing(monkeypatch):
    monkeypatch.setattr(llm.requests, "post",
                        lambda url, json=None, timeout=None: MagicMock(
                            json=lambda: {"error": "model not found"}))
    with pytest.raises(llm.ProviderUnavailable):
        llm.OllamaToolProvider().run_turn("S", "U", TOOLS, lambda n, a: {}, lambda t: None)


def test_availability_tracks_whether_the_model_is_pulled(monkeypatch):
    monkeypatch.setattr(llm.requests, "get", MagicMock(
        return_value=MagicMock(json=lambda: {"models": [{"name": "qwen3:4b"}]})))
    assert llm.OllamaToolProvider().available() is True
    assert llm.OllamaToolProvider(model="nothing:8b").available() is False
