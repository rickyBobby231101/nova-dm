"""Pure D&D 5e SRD math -- no I/O. Every derived stat is computed here so
nothing gets bookkept by hand or invented by the LLM."""

XP_THRESHOLDS = [0, 300, 900, 2700, 6500, 14000, 23000, 34000, 48000, 64000,
                 85000, 100000, 120000, 140000, 165000, 195000, 225000,
                 265000, 305000, 355000]  # index i = XP needed for level i+1


def ability_mod(score: int) -> int:
    return (score - 10) // 2


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
