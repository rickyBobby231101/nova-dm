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
    inventing 'cursed' should not lose it, it just has no mechanical effect.

    A condition ends in one of three ways:
      * nothing set -- it lasts until someone clears it. Everything written
        before durations existed looks like this.
      * `expires_round` -- the last round on which it is still active.
      * `expires_position` -- a point in the initiative order, counted as
        (round - 1) * combatants + turn_index. That is how "until the end of
        Kael's next turn" is stored, since a turn boundary is just a position.
        `until_label` carries the phrasing for display.
    """
    out = []
    for entry in conditions or []:
        if isinstance(entry, str):
            name, level = entry.strip().lower(), None
            expires = position = label = None
        elif isinstance(entry, dict):
            name = str(entry.get("name", "")).strip().lower()
            level = entry.get("level")
            expires = entry.get("expires_round")
            position = entry.get("expires_position")
            label = entry.get("until_label")
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
        for key, value in (("expires_round", expires), ("expires_position", position)):
            try:
                if value is not None:
                    item[key] = int(value)
            except (TypeError, ValueError):
                pass
        if label and item.get("expires_position") is not None:
            item["until_label"] = str(label)
        out.append(item)
    return out


def names(conditions) -> set:
    return {c["name"] for c in normalize(conditions)}


def exhaustion_level(conditions) -> int:
    for c in normalize(conditions):
        if c["name"] == "exhaustion":
            return c.get("level", 1)
    return 0


def add(conditions, name: str, level=None, duration_rounds=None, current_round: int = 1,
        expires_position=None, until_label=None) -> list:
    """Applying a condition twice is not an error -- it replaces, so exhaustion
    can be raised or lowered, or a duration refreshed, without removing it first.

    duration_rounds counts the round it is applied in: 1 round means it lasts
    through the current round and is gone at the start of the next.
    expires_position is an initiative position, worked out by the caller (only
    the encounter knows the turn order); it wins if both are given."""
    name = str(name).strip().lower()
    kept = [c for c in normalize(conditions) if c["name"] != name]
    entry = {"name": name}
    if name == "exhaustion":
        entry["level"] = level if level is not None else 1

    if expires_position is not None:
        entry["expires_position"] = expires_position
        if until_label:
            entry["until_label"] = until_label
    else:
        try:
            rounds = int(duration_rounds)
        except (TypeError, ValueError):
            rounds = 0
        if rounds >= 1:
            entry["expires_round"] = int(current_round) + rounds - 1
    return normalize(kept + [entry])


def remaining_rounds(condition, current_round: int):
    """Rounds left including this one, or None when it lasts until cleared."""
    expires = normalize([condition])[0].get("expires_round")
    if expires is None:
        return None
    return max(0, expires - int(current_round) + 1)


def expire(conditions, current_round: int, current_position: int = None) -> tuple:
    """Split into what survives and what has just run out. Round durations end
    when the round moves past them; turn durations end when the initiative order
    reaches the position they were pinned to."""
    kept, done = [], []
    for c in normalize(conditions):
        by_round = c.get("expires_round")
        by_position = c.get("expires_position")
        finished = (
            (by_round is not None and int(current_round) > by_round)
            or (by_position is not None and current_position is not None
                and int(current_position) >= by_position)
        )
        (done if finished else kept).append(c)
    return kept, done


def drop_timed(conditions) -> tuple:
    """Rounds and turn positions only exist inside an encounter, so a condition
    measured in either has nothing left to count once the fight ends. Returns
    (kept, dropped)."""
    kept, dropped = [], []
    for c in normalize(conditions):
        timed = c.get("expires_round") is not None or c.get("expires_position") is not None
        (dropped if timed else kept).append(c)
    return kept, dropped


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


def describe(conditions, current_round: int = None) -> str:
    """Short label for feed lines and the DM's context."""
    parts = []
    for c in normalize(conditions):
        label = f"{c['name']} {c['level']}" if c["name"] == "exhaustion" else c["name"]
        if c.get("until_label"):
            label += f" ({c['until_label']})"
        elif current_round is not None:
            left = remaining_rounds(c, current_round)
            if left is not None:
                label += f" ({left} rd)" if left != 1 else " (1 rd)"
        parts.append(label)
    return ", ".join(parts)
