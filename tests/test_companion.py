"""
Automated party members — entities acting when nobody plays them.

Daniel, 2026-09-05: "all the entities should be playable or automated parts of
the party ... even then if they are not in campaign personally when I am then
it is automated."

Automation follows the seat, not a flag: anything on the Cathedral seat is
unclaimed, so the table plays it. PLAY AS moves it off, and it stops being
automated in the same motion. There is no npc boolean to keep in sync.

Nothing here calls a model. `decide()` takes an injectable `ask` so the
decision can be tested for shape, budget and failure without a 60-second
generation — measured live at 59-85s per companion on this box, which is also
why nothing fires seven of them automatically.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import character, companion

pytestmark = pytest.mark.skipif(
    not os.path.exists(character.SRD_DB_PATH),
    reason="srd.sqlite not present -- run ingest/ingest_srd.py first"
)


@pytest.fixture(autouse=True)
def clean_campaign_db():
    if os.path.exists(character.CAMPAIGN_DB_PATH):
        os.remove(character.CAMPAIGN_DB_PATH)
    yield
    if os.path.exists(character.CAMPAIGN_DB_PATH):
        os.remove(character.CAMPAIGN_DB_PATH)


def _entity(name="Tillagon"):
    seat = character.create_player(character.NPC_SEAT_NAME)
    return character.create_character(
        seat["id"], name, "dragonborn", "paladin",
        dict(str=16, dex=10, con=16, int=11, wis=13, cha=15))


class TestAutomationFollowsTheSeat:
    def test_an_unclaimed_entity_is_automated(self):
        assert companion.is_automated(_entity()) is True

    def test_claiming_stops_the_automation(self):
        e = _entity()
        me = character.create_player("Chazel")
        character.claim_character(e["id"], me["id"])
        assert companion.is_automated(character.get_character(e["id"])) is False

    def test_a_players_own_character_is_never_automated(self):
        _entity()
        me = character.create_player("Chazel")
        mine = character.create_character(me["id"], "Mira", "elf", "druid",
                                          dict(str=10, dex=14, con=12, int=11, wis=15, cha=10))
        assert companion.is_automated(mine) is False

    def test_the_automated_party_is_exactly_the_unclaimed(self):
        a, b = _entity("Tillagon"), _entity("Zorya")
        me = character.create_player("Chazel")
        character.claim_character(a["id"], me["id"])
        assert [c["name"] for c in companion.automated_party()] == ["Zorya"]


class TestTheDecisionStaysInBudget:
    """A companion that monologues steals the turn from the humans."""

    def test_one_sentence_only(self):
        long = ("I step forward and raise my hammer. Then I shout a warning. "
                "Then I charge down the stair.")
        assert companion.trim(long) == "I step forward and raise my hammer."

    def test_a_run_on_is_cut_to_the_word_budget(self):
        rambling = " ".join(["word"] * 80)
        out = companion.trim(rambling)
        assert len(out.split()) <= companion.MAX_WORDS + 1  # +1 for the ellipsis token
        assert out.endswith("…")

    def test_surrounding_quotes_are_dropped(self):
        assert companion.trim('"I hold the line."') == "I hold the line."

    def test_empty_stays_empty(self):
        assert companion.trim("") == ""
        assert companion.trim(None) == ""

    def test_decide_applies_the_trim(self):
        e = _entity()
        out = companion.decide(e, recent=[], ask=lambda p: "I hold. I wait. I strike.")
        assert out == "I hold."


class TestItNeverStopsTheTable:
    def test_a_failing_model_returns_nothing_rather_than_raising(self):
        """A companion that cannot think must not take the turn down with it."""
        e = _entity()
        def boom(prompt):
            raise RuntimeError("ollama is down")
        assert companion.decide(e, recent=[], ask=boom) == ""


class TestThePromptCarriesWhoTheyAre:
    def test_the_persona_comes_from_the_campaign_cast(self):
        """Reuses the line the DM already reads every turn, rather than a
        second description that would drift from it."""
        from engine import campaign
        campaign.load_seed("campaigns/cathedral.md")
        p = companion.build_prompt(_entity(), [])
        assert "dragon guardian" in p.lower()

    def test_the_sheet_is_included(self):
        p = companion.build_prompt(_entity(), [])
        assert "13/13 HP" in p and "paladin" in p

    def test_it_asks_for_an_attempt_not_an_outcome(self):
        """A companion narrating results would be playing DM."""
        p = companion.build_prompt(_entity(), [])
        assert "say what you attempt, not what happens" in p

    def test_recent_beats_are_included(self):
        p = companion.build_prompt(_entity(), [{"content": "Chazel: I listen at the door."}])
        assert "I listen at the door" in p
