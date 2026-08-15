import copy
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import character, portable

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


def _make_character(name="Thorin", race="dwarf", class_="fighter"):
    return character.create_character(
        player_id=1, name=name, race=race, class_=class_,
        ability_scores={"str": 16, "dex": 12, "con": 14, "int": 10, "wis": 11, "cha": 8},
    )


CARRIED_FIELDS = [
    "name", "race", "class", "level", "xp", "str", "dex", "con", "int_", "wis", "cha",
    "max_hp", "current_hp", "temp_hp", "ac", "speed", "proficiency_bonus", "gold",
    "inspiration", "conditions_json", "notes",
]


@pytest.mark.parametrize("race", ["dwarf", "half-orc", "human"])
def test_round_trip_preserves_every_field(race):
    """The one that matters: create_character applies racial ability bonuses, so an
    import routed through it would re-apply them and a dwarf's CON would climb +2
    on every trip. Races with bonuses are used deliberately."""
    original = _make_character(race=race)
    character.apply_damage(original["id"], 3)
    original = character.get_character(original["id"])

    payload = portable.export_character(original["id"])
    restored = portable.import_character(payload, player_id=2)

    for field in CARRIED_FIELDS:
        assert restored[field] == original[field], f"{field} changed across the round trip"


def test_round_trip_survives_json_serialization():
    """Exports travel as text, so the payload has to survive dumps/loads."""
    original = _make_character()
    blob = json.dumps(portable.export_character(original["id"]))

    restored = portable.import_character(portable.parse(blob), player_id=2)

    assert restored["name"] == original["name"]
    assert restored["con"] == original["con"]


def test_import_creates_a_new_character_for_the_importing_player():
    original = _make_character()
    payload = portable.export_character(original["id"])

    restored = portable.import_character(payload, player_id=99)

    assert restored["id"] != original["id"]
    assert restored["player_id"] == 99
    assert character.get_character(original["id"]) is not None, "the original must be untouched"


def test_export_omits_identity_and_ownership():
    char = _make_character()
    payload = portable.export_character(char["id"])

    assert "id" not in payload["character"]
    assert "player_id" not in payload["character"]
    assert payload["format"] == portable.FORMAT
    assert payload["version"] == portable.VERSION


def test_features_are_carried_across():
    char = _make_character()
    payload = portable.export_character(char["id"])
    assert payload["features"], "fixture assumes a level 1 fighter has features"

    restored = portable.import_character(payload, player_id=2)

    with character._campaign_con() as con:
        slugs = [r["feature_slug"] for r in con.execute(
            "SELECT feature_slug FROM character_features WHERE character_id=?", (restored["id"],)
        ).fetchall()]
    assert slugs == payload["features"]


def test_proficiency_bonus_is_recomputed_not_trusted():
    """Purely derived from level, so a hand-edited file gets repaired."""
    char = _make_character()
    payload = portable.export_character(char["id"])
    payload["character"]["level"] = 9
    payload["character"]["proficiency_bonus"] = 99

    restored = portable.import_character(payload, player_id=2)

    assert restored["proficiency_bonus"] == 4  # level 9 -> +4


def test_current_hp_is_clamped_to_max():
    char = _make_character()
    payload = portable.export_character(char["id"])
    payload["character"]["hp"] = {"max": 20, "current": 500, "temp": 0}

    restored = portable.import_character(payload, player_id=2)

    assert restored["current_hp"] == 20


def test_unknown_keys_are_ignored_so_newer_exports_still_load():
    char = _make_character()
    payload = portable.export_character(char["id"])
    payload["character"]["favourite_snack"] = "ale"
    payload["something_new"] = {"nested": True}

    restored = portable.import_character(payload, player_id=2)

    assert restored["name"] == char["name"]


def test_rejects_a_file_from_a_newer_format_version():
    char = _make_character()
    payload = portable.export_character(char["id"])
    payload["version"] = portable.VERSION + 1

    with pytest.raises(portable.PortableError, match="newer nova-dm"):
        portable.import_character(payload, player_id=2)


def test_rejects_something_that_is_not_a_nova_dm_export():
    with pytest.raises(portable.PortableError, match="isn't a nova-dm character export"):
        portable.import_character({"format": "dndbeyond", "version": 1}, player_id=2)


def test_rejects_a_payload_with_no_character():
    with pytest.raises(portable.PortableError, match="no character in it"):
        portable.import_character({"format": portable.FORMAT, "version": 1}, player_id=2)


@pytest.mark.parametrize("field", ["name", "race", "class"])
def test_rejects_a_character_missing_an_essential_field(field):
    char = _make_character()
    payload = portable.export_character(char["id"])
    payload["character"][field] = ""

    with pytest.raises(portable.PortableError, match=f"missing its {field}"):
        portable.import_character(payload, player_id=2)


@pytest.mark.parametrize("blob", ["", "   ", "{not json", "[1,2,3]", "null"])
def test_parse_rejects_junk_with_a_readable_message(blob):
    with pytest.raises(portable.PortableError) as e:
        portable.import_character(portable.parse(blob), player_id=2)
    assert str(e.value) and "Traceback" not in str(e.value)


def test_missing_numbers_fall_back_instead_of_crashing():
    """A hand-written file shouldn't need every field to be importable."""
    payload = {
        "format": portable.FORMAT, "version": 1,
        "character": {"name": "Sparse", "race": "human", "class": "fighter"},
    }

    restored = portable.import_character(payload, player_id=2)

    assert restored["level"] == 1
    assert restored["max_hp"] >= 1
    assert restored["current_hp"] == restored["max_hp"]
    assert restored["ac"] == 10


def test_export_payload_is_not_mutated_by_import():
    char = _make_character()
    payload = portable.export_character(char["id"])
    before = copy.deepcopy(payload)

    portable.import_character(payload, player_id=2)

    assert payload == before, "importing must not modify the caller's payload"


def test_filename_is_derived_from_the_character_name():
    assert portable.filename_for({"name": "Kael Stormhand"}) == "kael-stormhand.nova-dm.json"
    assert portable.filename_for({"name": "???"}) == "character.nova-dm.json"
