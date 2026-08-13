"""Pure D&D 5e SRD math -- no I/O. Every derived stat is computed here so
nothing gets bookkept by hand or invented by the LLM."""

XP_THRESHOLDS = [0, 300, 900, 2700, 6500, 14000, 23000, 34000, 48000, 64000,
                 85000, 100000, 120000, 140000, 165000, 195000, 225000,
                 265000, 305000, 355000]  # index i = XP needed for level i+1


# Open5e publishes challenge rating but no XP value, unlike dnd5eapi -- this is the
# SRD's own CR-to-XP table, so encounter XP stays real rather than estimated.
CR_XP = {
    0: 10, 0.125: 25, 0.25: 50, 0.5: 100, 1: 200, 2: 450, 3: 700, 4: 1100, 5: 1800,
    6: 2300, 7: 2900, 8: 3900, 9: 5000, 10: 5900, 11: 7200, 12: 8400, 13: 10000,
    14: 11500, 15: 13000, 16: 15000, 17: 18000, 18: 20000, 19: 22000, 20: 25000,
    21: 33000, 22: 41000, 23: 50000, 24: 62000, 25: 75000, 26: 90000, 27: 105000,
    28: 120000, 29: 135000, 30: 155000,
}


def ability_mod(score: int) -> int:
    return (score - 10) // 2


def xp_for_cr(cr) -> int:
    """XP for a challenge rating. Falls back to the nearest lower CR for anything
    off-table (third-party statblocks occasionally carry odd values)."""
    try:
        cr = float(cr)
    except (TypeError, ValueError):
        return 0
    if cr in CR_XP:
        return CR_XP[cr]
    lower = [c for c in CR_XP if c <= cr]
    return CR_XP[max(lower)] if lower else 0


def proficiency_bonus(level: int) -> int:
    return 2 + (level - 1) // 4


def xp_to_level(xp: int) -> int:
    level = 1
    for i, threshold in enumerate(XP_THRESHOLDS):
        if xp >= threshold:
            level = i + 1
    return level


def hp_on_levelup(hit_die: int, con_mod: int, method: str = "average") -> int:
    """Average method (SRD default): floor(hit_die/2) + 1, plus CON mod.
    Minimum 1 HP gained regardless of a negative CON mod."""
    if method != "average":
        raise ValueError(f"unsupported method: {method}")
    return max(1, (hit_die // 2 + 1) + con_mod)
