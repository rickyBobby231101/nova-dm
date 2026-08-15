import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import conditions as cond
from engine import dice

# ── normalizing ───────────────────────────────────────────────────────────────

def test_plain_strings_are_accepted():
    """Phase 6's portable format declares conditions as a bare list, so strings
    have to keep working."""
    assert cond.normalize(["poisoned", "prone"]) == [{"name": "poisoned"}, {"name": "prone"}]


def test_exhaustion_carries_a_level_and_is_clamped():
    assert cond.normalize([{"name": "exhaustion", "level": 4}]) == [{"name": "exhaustion", "level": 4}]
    assert cond.normalize([{"name": "exhaustion", "level": 99}])[0]["level"] == 6
    assert cond.normalize([{"name": "exhaustion", "level": 0}])[0]["level"] == 1
    assert cond.normalize(["exhaustion"])[0]["level"] == 1
    assert cond.normalize([{"name": "exhaustion", "level": "junk"}])[0]["level"] == 1


def test_unknown_conditions_are_kept_even_though_they_do_nothing():
    """A DM inventing 'cursed' shouldn't have it silently dropped."""
    assert cond.normalize(["cursed"]) == [{"name": "cursed"}]
    assert cond.attack_advantage(["cursed"], []) is None


def test_junk_entries_are_skipped():
    assert cond.normalize([None, 7, "", {"name": ""}, "prone"]) == [{"name": "prone"}]


def test_adding_the_same_condition_twice_replaces_it():
    once = cond.add([], "exhaustion", 2)
    twice = cond.add(once, "exhaustion", 5)
    assert twice == [{"name": "exhaustion", "level": 5}]


def test_remove_is_a_noop_for_something_not_present():
    assert cond.remove([{"name": "prone"}], "poisoned") == [{"name": "prone"}]


# ── durations ─────────────────────────────────────────────────────────────────

def test_a_condition_with_no_duration_lasts_until_cleared():
    applied = cond.add([], "poisoned")
    assert "expires_round" not in applied[0]
    assert cond.remaining_rounds(applied[0], current_round=99) is None


def test_duration_counts_the_round_it_was_applied_in():
    """One round means 'this round' -- gone at the start of the next."""
    applied = cond.add([], "prone", duration_rounds=1, current_round=4)
    assert applied[0]["expires_round"] == 4
    assert cond.remaining_rounds(applied[0], 4) == 1

    three = cond.add([], "prone", duration_rounds=3, current_round=4)
    assert three[0]["expires_round"] == 6
    assert [cond.remaining_rounds(three[0], r) for r in (4, 5, 6, 7)] == [3, 2, 1, 0]


@pytest.mark.parametrize("bad", [0, -2, None, "soon"])
def test_a_nonsense_duration_means_no_duration(bad):
    applied = cond.add([], "poisoned", duration_rounds=bad, current_round=2)
    assert "expires_round" not in applied[0]


def test_expire_splits_what_survives_from_what_ran_out():
    active = cond.add([], "prone", duration_rounds=2, current_round=1)      # through round 2
    active = cond.add(active, "poisoned")                                    # indefinite
    active = cond.add(active, "stunned", duration_rounds=1, current_round=1)  # round 1 only

    kept, done = cond.expire(active, current_round=2)
    assert {c["name"] for c in kept} == {"prone", "poisoned"}
    assert [c["name"] for c in done] == ["stunned"]

    kept, done = cond.expire(kept, current_round=3)
    assert [c["name"] for c in kept] == ["poisoned"]
    assert [c["name"] for c in done] == ["prone"]


def test_reapplying_refreshes_the_duration():
    applied = cond.add([], "poisoned", duration_rounds=2, current_round=1)
    refreshed = cond.add(applied, "poisoned", duration_rounds=2, current_round=3)
    assert refreshed[0]["expires_round"] == 4
    assert len(refreshed) == 1


def test_drop_timed_keeps_the_open_ended_ones():
    active = cond.add([], "prone", duration_rounds=3, current_round=1)
    active = cond.add(active, "poisoned")

    kept, dropped = cond.drop_timed(active)

    assert [c["name"] for c in kept] == ["poisoned"]
    assert [c["name"] for c in dropped] == ["prone"]


def test_describe_shows_the_countdown_when_asked():
    active = cond.add([], "poisoned", duration_rounds=3, current_round=1)
    assert cond.describe(active) == "poisoned"
    assert cond.describe(active, current_round=1) == "poisoned (3 rd)"
    assert cond.describe(active, current_round=3) == "poisoned (1 rd)"


def test_expiry_survives_normalizing_from_storage():
    """Durations go through JSON, so they have to come back as ints."""
    stored = [{"name": "prone", "expires_round": "5"}]
    assert cond.normalize(stored)[0]["expires_round"] == 5


# ── the advantage rules ───────────────────────────────────────────────────────

@pytest.mark.parametrize("attacker,target,expected", [
    ([], [], None),
    # target grants advantage
    ([], ["prone"], cond.ADVANTAGE),
    ([], ["paralyzed"], cond.ADVANTAGE),
    ([], ["restrained"], cond.ADVANTAGE),
    ([], ["unconscious"], cond.ADVANTAGE),
    # attacker suffers disadvantage
    (["poisoned"], [], cond.DISADVANTAGE),
    (["frightened"], [], cond.DISADVANTAGE),
    (["blinded"], [], cond.DISADVANTAGE),
    (["restrained"], [], cond.DISADVANTAGE),
    # invisibility cuts both ways
    (["invisible"], [], cond.ADVANTAGE),
    ([], ["invisible"], cond.DISADVANTAGE),
    # exhaustion only bites at tier 3
    ([{"name": "exhaustion", "level": 2}], [], None),
    ([{"name": "exhaustion", "level": 3}], [], cond.DISADVANTAGE),
    # conditions with no effect on attacks
    ([], ["charmed"], None),
    (["deafened"], [], None),
    # the cancellation rule
    (["poisoned"], ["prone"], None),
    (["invisible"], ["invisible"], None),
    (["poisoned", "invisible"], [], None),
])
def test_attack_advantage_matrix(attacker, target, expected):
    assert cond.attack_advantage(attacker, target) == expected


def test_multiple_sources_of_the_same_side_do_not_stack_into_something_else():
    """Three sources of advantage is still just advantage."""
    assert cond.attack_advantage(["invisible"], ["prone", "restrained"]) == cond.ADVANTAGE


def test_combine_implements_cancellation():
    assert cond.combine(cond.ADVANTAGE, None) == cond.ADVANTAGE
    assert cond.combine(cond.DISADVANTAGE, None) == cond.DISADVANTAGE
    assert cond.combine(cond.ADVANTAGE, cond.DISADVANTAGE) is None
    assert cond.combine(None, None) is None
    assert cond.combine() is None


# ── acting and checks ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("condition", sorted(cond.INCAPACITATING))
def test_incapacitating_conditions_stop_a_combatant_acting(condition):
    assert cond.can_act([condition]) is False


def test_ordinary_conditions_do_not_stop_a_combatant_acting():
    assert cond.can_act(["poisoned", "prone", "frightened"]) is True
    assert cond.can_act([]) is True


def test_ability_checks_suffer_while_impaired():
    assert cond.check_advantage(["poisoned"]) == cond.DISADVANTAGE
    assert cond.check_advantage([{"name": "exhaustion", "level": 1}]) == cond.DISADVANTAGE
    assert cond.check_advantage(["prone"]) is None
    assert cond.check_advantage([]) is None


def test_describe_reads_like_a_feed_line():
    assert cond.describe(["poisoned", {"name": "exhaustion", "level": 3}]) == "poisoned, exhaustion 3"
    assert cond.describe([]) == ""


# ── the dice actually move ────────────────────────────────────────────────────

def test_roll_d20_draws_two_dice_only_when_it_should():
    assert len(dice.roll_d20()["d20_rolls"]) == 1
    assert len(dice.roll_d20("advantage")["d20_rolls"]) == 2
    assert len(dice.roll_d20("disadvantage")["d20_rolls"]) == 2


def test_roll_d20_keeps_the_right_die():
    for _ in range(200):
        adv = dice.roll_d20("advantage")
        assert adv["d20"] == max(adv["d20_rolls"])
        dis = dice.roll_d20("disadvantage")
        assert dis["d20"] == min(dis["d20_rolls"])


def test_advantage_shifts_the_average_not_just_the_bookkeeping():
    """A swapped max/min would still pass a single-roll assertion, so check the
    distribution: advantage should average clearly above a straight roll and
    disadvantage clearly below."""
    n = 3000
    straight = sum(dice.roll_d20()["d20"] for _ in range(n)) / n
    high = sum(dice.roll_d20("advantage")["d20"] for _ in range(n)) / n
    low = sum(dice.roll_d20("disadvantage")["d20"] for _ in range(n)) / n

    assert high > straight + 1.5, f"advantage averaged {high:.2f} vs {straight:.2f}"
    assert low < straight - 1.5, f"disadvantage averaged {low:.2f} vs {straight:.2f}"
