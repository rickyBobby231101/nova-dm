"""Dice rolling. All real randomness (random.randint), never LLM-invented."""
import random
import re

from . import rules

_DICE_RE = re.compile(r"^(\d*)d(\d+)([+-]\d+)?$")


def roll(expr: str) -> dict:
    """roll("2d6+3") -> {"expr", "rolls": [...], "modifier": 3, "total"}"""
    m = _DICE_RE.match(expr.strip().replace(" ", ""))
    if not m:
        raise ValueError(f"bad dice expression: {expr}")
    count = int(m.group(1)) if m.group(1) else 1
    sides = int(m.group(2))
    modifier = int(m.group(3)) if m.group(3) else 0
    rolls = [random.randint(1, sides) for _ in range(count)]
    return {"expr": expr, "rolls": rolls, "modifier": modifier, "total": sum(rolls) + modifier}


def roll_d20(adv: str = None) -> dict:
    """One d20, or two and keep the right one. Every d20 in the game comes
    through here -- ability checks, saves and monster attacks alike -- so
    advantage is implemented once instead of per call site.
    adv: None | 'advantage' | 'disadvantage'."""
    d20s = [random.randint(1, 20)]
    if adv in ("advantage", "disadvantage"):
        d20s.append(random.randint(1, 20))
    d20 = max(d20s) if adv == "advantage" else min(d20s) if adv == "disadvantage" else d20s[0]
    return {"d20": d20, "d20_rolls": d20s, "adv": adv}


def roll_check(character: dict, ability: str, proficient: bool = False, adv: str = None) -> dict:
    """ability: 'str'/'dex'/'con'/'int_'/'wis'/'cha' (matches character dict keys).
    adv: None | 'advantage' | 'disadvantage'."""
    col = "int_" if ability in ("int", "int_") else ability
    mod = rules.ability_mod(character[col])
    if proficient:
        mod += character.get("proficiency_bonus", 2)

    rolled = roll_d20(adv)

    return {"ability": ability, "d20": rolled["d20"], "d20_rolls": rolled["d20_rolls"],
            "modifier": mod, "total": rolled["d20"] + mod, "proficient": proficient, "adv": adv}
