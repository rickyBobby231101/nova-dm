import os
import random
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import dice


def test_roll_basic():
    random.seed(1)
    result = dice.roll("2d6+3")
    assert result["expr"] == "2d6+3"
    assert len(result["rolls"]) == 2
    assert all(1 <= r <= 6 for r in result["rolls"])
    assert result["modifier"] == 3
    assert result["total"] == sum(result["rolls"]) + 3
    assert 5 <= result["total"] <= 15


def test_roll_single_die_no_modifier():
    result = dice.roll("1d20")
    assert len(result["rolls"]) == 1
    assert result["modifier"] == 0
    assert 1 <= result["total"] <= 20


def test_roll_bad_expr():
    with pytest.raises(ValueError):
        dice.roll("not-a-dice-expr")


def test_roll_check_advantage_takes_higher():
    char = {"str": 16, "proficiency_bonus": 2}
    random.seed(42)
    # Run many times and confirm advantage's d20 is always >= min of its two rolls
    for _ in range(50):
        result = dice.roll_check(char, "str", proficient=True, adv="advantage")
        assert result["d20"] == max(result["d20_rolls"])
    random.seed(42)
    for _ in range(50):
        result = dice.roll_check(char, "str", proficient=True, adv="disadvantage")
        assert result["d20"] == min(result["d20_rolls"])


def test_roll_check_modifier_includes_proficiency():
    char = {"dex": 14, "proficiency_bonus": 3}
    result = dice.roll_check(char, "dex", proficient=True)
    assert result["modifier"] == 2 + 3  # ability_mod(14)=2, plus prof bonus
    result_unprof = dice.roll_check(char, "dex", proficient=False)
    assert result_unprof["modifier"] == 2
