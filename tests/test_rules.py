import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import rules


def test_ability_mod():
    assert rules.ability_mod(10) == 0
    assert rules.ability_mod(16) == 3
    assert rules.ability_mod(8) == -1
    assert rules.ability_mod(20) == 5


def test_proficiency_bonus():
    assert rules.proficiency_bonus(1) == 2
    assert rules.proficiency_bonus(4) == 2
    assert rules.proficiency_bonus(5) == 3
    assert rules.proficiency_bonus(9) == 4
    assert rules.proficiency_bonus(17) == 6
    assert rules.proficiency_bonus(20) == 6


def test_xp_to_level():
    assert rules.xp_to_level(0) == 1
    assert rules.xp_to_level(299) == 1
    assert rules.xp_to_level(300) == 2
    assert rules.xp_to_level(900) == 3
    assert rules.xp_to_level(355000) == 20
    assert rules.xp_to_level(999999) == 20


def test_hp_on_levelup():
    assert rules.hp_on_levelup(10, 2) == 8  # d10 avg (6) + CON 2
    assert rules.hp_on_levelup(6, -1) == 3  # d6 avg (4) + CON -1, not below 1
    assert rules.hp_on_levelup(6, -5) == 1  # clamped to minimum 1
