"""Phase 7: the SRD conditions, and what they do to a roll.

Pure rules, no I/O -- same shape as engine/rules.py. Storage and the UI live
elsewhere; this file is only the question "given these conditions, what happens
to the dice", so it can be read and argued with on its own.

Conditions are stored as a list of objects rather than bare strings, because
exhaustion carries a tier: [{"name": "poisoned"}, {"name": "exhaustion",
"level": 3}]. Plain strings are accepted and normalized, since the Phase 6
portable format declares `conditions` as a bare list.

TWO DELIBERATE SIMPLIFICATIONS, because this app models no positioning:
  * prone always grants advantage to attackers (the melee case). By the SRD it
    should be disadvantage for attacks from further than 5 feet, but nothing here
    knows where anyone is standing.
  * frightened applies its disadvantage unconditionally. The SRD limits it to
    while the source of the fear is in line of sight, which we likewise can't
    know.
Both make the common case right and the uncommon case generous; a DM who cares
can clear the condition.
"""

ADVANTAGE = "advantage"
DISADVANTAGE = "disadvantage"

# The 15 SRD conditions.
ALL = [
    "blinded", "charmed", "deafened", "exhaustion", "frightened", "grappled",
    "incapacitated", "invisible", "paralyzed", "petrified", "poisoned", "prone",
    "restrained", "stunned", "unconscious",
]

# Can't take actions at all.
INCAPACITATING = {"incapacitated", "paralyzed", "petrified", "stunned", "unconscious"}

# Attacking while under these is at disadvantage.
ATTACKER_DISADVANTAGE = {"blinded", "frightened", "poisoned", "prone", "restrained"}

# Being under these hands attackers advantage.
TARGET_GRANTS_ADVANTAGE = {
    "blinded", "paralyzed", "petrified", "prone", "restrained", "stunned", "unconscious",
}

EXHAUSTION_ATTACK_DISADVANTAGE = 3   # tier 3: disadvantage on attack rolls and saves
EXHAUSTION_CHECK_DISADVANTAGE = 1    # tier 1: disadvantage on ability checks


def normalize(conditions) -> list:
    """Accept strings or objects, return objects. Unknown names are kept: a DM
    inventing 'cursed' should not lose it, it just has no mechanical effect."""
    out = []
    for entry in conditions or []:
        if isinstance(entry, str):
            name, level = entry.strip().lower(), None
        elif isinstance(entry, dict):
            name = str(entry.get("name", "")).strip().lower()
            level = entry.get("level")
        else:
            continue
        if not name:
            continue
        item = {"name": name}
        if name == "exhaustion":
            try:
                item["level"] = max(1, min(6, int(level)))
            except (TypeError, ValueError):
                item["level"] = 1
        out.append(item)
    return out


def names(conditions) -> set:
    return {c["name"] for c in normalize(conditions)}


def exhaustion_level(conditions) -> int:
    for c in normalize(conditions):
        if c["name"] == "exhaustion":
            return c.get("level", 1)
    return 0


def add(conditions, name: str, level=None) -> list:
    """Applying a condition twice is not an error -- it replaces, so exhaustion
    can be raised or lowered without removing it first."""
    name = str(name).strip().lower()
    kept = [c for c in normalize(conditions) if c["name"] != name]
    entry = {"name": name}
    if name == "exhaustion":
        entry["level"] = level if level is not None else 1
    return normalize(kept + [entry])


def remove(conditions, name: str) -> list:
    name = str(name).strip().lower()
    return [c for c in normalize(conditions) if c["name"] != name]


def combine(*values) -> str | None:
    """The SRD's cancellation rule, in one place: any advantage plus any
    disadvantage is a straight roll, however many sources each has."""
    has_adv = any(v == ADVANTAGE for v in values)
    has_dis = any(v == DISADVANTAGE for v in values)
    if has_adv and has_dis:
        return None
    if has_adv:
        return ADVANTAGE
    if has_dis:
        return DISADVANTAGE
    return None


def can_act(conditions) -> bool:
    return not (names(conditions) & INCAPACITATING)


def check_advantage(conditions) -> str | None:
    """Ability checks while impaired."""
    impaired = names(conditions) & {"poisoned", "frightened"}
    if impaired or exhaustion_level(conditions) >= EXHAUSTION_CHECK_DISADVANTAGE:
        return DISADVANTAGE
    return None


def attack_advantage(attacker_conditions, target_conditions) -> str | None:
    """What an attack roll gets, given both sides' conditions."""
    attacker, target = names(attacker_conditions), names(target_conditions)

    attacker_side = []
    if attacker & ATTACKER_DISADVANTAGE:
        attacker_side.append(DISADVANTAGE)
    if exhaustion_level(attacker_conditions) >= EXHAUSTION_ATTACK_DISADVANTAGE:
        attacker_side.append(DISADVANTAGE)
    if "invisible" in attacker:
        attacker_side.append(ADVANTAGE)

    target_side = []
    if target & TARGET_GRANTS_ADVANTAGE:
        target_side.append(ADVANTAGE)
    if "invisible" in target:
        target_side.append(DISADVANTAGE)

    return combine(*attacker_side, *target_side)


def describe(conditions) -> str:
    """Short label for feed lines and the DM's context."""
    parts = []
    for c in normalize(conditions):
        parts.append(f"{c['name']} {c['level']}" if c["name"] == "exhaustion" else c["name"])
    return ", ".join(parts)
