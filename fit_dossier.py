#!/usr/bin/env python3
"""Fit Dossier — ship fitting data gatherer for EVE Online.

Generates a comprehensive markdown dossier for a given ship, including hull stats,
module candidates with prices, drone options, and stacking penalty reference.
Designed for pasting into a Claude chat for AI-assisted fitting decisions.

Does NOT propose fits. Does NOT compute final yields. That's the AI's job (creative)
and PyFA's job (verification).
"""

import argparse
import configparser
import concurrent.futures
import datetime
import math
import os
import re
import sys
import time

# Ensure same-directory imports work
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eve_common as esi

# ── Dogma attribute IDs (stable across ESI versions) ──────────
# Verified via self-test against ESI /dogma/attributes/{id}/

A = {
    # Ship hull — slot layout
    "hiSlots": 14,
    "medSlots": 13,
    "lowSlots": 12,
    "turretSlotsLeft": 102,
    "launcherSlotsLeft": 101,
    "rigSlots": 1137,
    "upgradeCapacity": 1132,

    # Ship hull — fitting resources
    "cpuOutput": 48,
    "powerOutput": 11,
    "capacitorCapacity": 482,
    "rechargeRate": 55,

    # Drones
    "droneBayCapacity": 283,
    "droneBandwidth": 1271,

    # Defence
    "shieldCapacity": 263,
    "armorHP": 265,
    "hp": 9,
    "shieldEmDamageResonance": 271,
    "shieldThermalDamageResonance": 274,
    "shieldKineticDamageResonance": 273,
    "shieldExplosiveDamageResonance": 272,
    "armorEmDamageResonance": 267,
    "armorThermalDamageResonance": 270,
    "armorKineticDamageResonance": 269,
    "armorExplosiveDamageResonance": 268,

    # Navigation
    "mass": 4,
    "agility": 70,
    "maxVelocity": 37,
    "warpSpeedMultiplier": 600,
    "signatureRadius": 552,

    # Targeting
    "maxLockedTargets": 524,
    "scanResolution": 564,

    # Cargo
    "capacity": 38,

    # Module fitting costs
    "cpu": 50,
    "power": 30,
    "upgradeCost": 1153,
    "capacitorNeed": 6,

    # Mining
    "miningAmount": 77,
    "duration": 73,

    # Meta classification
    "metaGroupID": 1692,
    "metaLevelOld": 633,

    # Skill requirements (on modules)
    "requiredSkill1": 182,
    "requiredSkill1Level": 277,
    "requiredSkill2": 183,
    "requiredSkill2Level": 278,
    "requiredSkill3": 184,
    "requiredSkill3Level": 279,

    # Module effect attributes (discovered via ESI dogma)
    "miningAmountBonus": 434,
    "cpuPenaltyPercent": 1082,
    "droneDamageBonus": 1255,
    "capacityBonus": 72,           # shield extender HP bonus (flat)
    "signatureRadiusAdd": 983,     # shield extender sig penalty (flat)
    "emDamageResistanceBonus": 984,
    "explosiveDamageResistanceBonus": 985,
    "kineticDamageResistanceBonus": 986,
    "thermalDamageResistanceBonus": 987,
    "agilityMultiplier": 169,
    "signatureRadiusBonus": 554,
    "structureHPMultiplier": 150,
    "implantBonusVelocity": 1076,  # nanofiber velocity bonus
    "cargoCapacityMultiplier": 149,
    "capacitorRechargeRateMultiplier": 144,
    "shieldBoostMultiplier": 548,
    "capacitorCapacityMultiplier": 147,
    "capacitorBonusFlat": 67,      # cap battery flat cap bonus
    "energyWarfareResistanceBonus": 2267,
    "hullEmDamageResonance": 974,
    "hullExplosiveDamageResonance": 975,
    "hullKineticDamageResonance": 976,
    "hullThermalDamageResonance": 977,
    "generalMiningHoldCapacity": 1556,

    # Rig/module drawback attributes
    "drawback": 1138,
    "shieldRechargeRateMultiplier": 134,

    # Ship special bays
    "fleetHangarCapacity": 912,
    "specialFuelBayCapacity": 1549,
}

# Reverse lookup: attribute_id → name
A_REV = {v: k for k, v in A.items()}

# ── Drawback effect → description mapping ─────────────────────
# Discovered via ESI /dogma/effects/{id}/. Standard drawback effects apply
# attr 1138 ("drawback") as PostPercent (op 6) to a specific ship attribute.
# Many rigs use these standard effects; others use custom per-rig effects
# that do the same thing (apply attr 1138 to a ship attribute).
DRAWBACK_EFFECTS = {
    2712: ("armor HP", 265),           # drawbackArmorHP
    2713: ("ship CPU output", 48),     # drawbackCPUOutput
    2714: ("launcher CPU need", 50),   # drawbackCPUNeedLaunchers
    2716: ("signature radius", 552),   # drawbackSigRad
    2717: ("agility", 70),             # drawbackAgility
    2718: ("shield capacity", 263),    # drawbackShieldCapacity
}

# Reverse lookup: modified_attribute → label (for custom drawback effects)
_DRAWBACK_TARGET_LABELS = {attr_id: label for _, (label, attr_id) in DRAWBACK_EFFECTS.items()}

# Cache for resolved drawback effect targets: effect_id → modified_attribute_id
_drawback_effect_cache = {}


def _resolve_drawback_effect(effect_id):
    """Fetch an effect and check if it applies attr 1138 to a ship attribute.

    Returns the modified_attribute_id if it does, else None. Cached.
    """
    if effect_id in _drawback_effect_cache:
        return _drawback_effect_cache[effect_id]
    result = None
    url = f"{esi.ESI_BASE}/dogma/effects/{effect_id}/?datasource=tranquility"
    edata = esi.esi_get_cached(url)
    if edata:
        for m in edata.get("modifiers", []):
            if (m.get("modifying_attribute_id") == A["drawback"]
                    and m.get("domain") == "shipID"
                    and m.get("operator") == 6):
                result = m["modified_attribute_id"]
                break
    _drawback_effect_cache[effect_id] = result
    return result


def _extract_drawbacks(da, dogma_effects):
    """Extract drawback descriptions from a module/rig's dogma data.

    Returns a list of short drawback strings (e.g. "-10% ship CPU output")
    for display in a table column.
    """
    drawbacks = []

    # 1. Generic drawback attr (1138) applied via effects
    drawback_val = da.get(A["drawback"])
    if drawback_val is not None and drawback_val != 0:
        effect_ids = {e["effect_id"] for e in (dogma_effects or [])}
        sign = "+" if drawback_val > 0 else ""

        # Try standard drawback effects first (no API call needed)
        matched = False
        for eff_id, (label, _) in DRAWBACK_EFFECTS.items():
            if eff_id in effect_ids:
                drawbacks.append(f"{sign}{drawback_val:.0f}% {label}")
                matched = True
                break

        # Fall back to resolving custom effects via ESI
        if not matched:
            for eff_id in effect_ids:
                if eff_id == 2663:  # rigSlot — skip
                    continue
                target_attr = _resolve_drawback_effect(eff_id)
                if target_attr is not None:
                    label = _DRAWBACK_TARGET_LABELS.get(target_attr, f"attr {target_attr}")
                    drawbacks.append(f"{sign}{drawback_val:.0f}% {label}")
                    matched = True
                    break

        if not matched:
            drawbacks.append(f"{sign}{drawback_val:.0f}% (unknown)")

    # 2. cpuPenaltyPercent (1082) — MLU-style "increases CPU of upgraded modules"
    cpu_pen = da.get(A["cpuPenaltyPercent"])
    if cpu_pen is not None and cpu_pen != 0:
        drawbacks.append(f"+{cpu_pen:.0f}% mining laser CPU need")

    # 3. shieldRechargeRateMultiplier (134) > 1.0 — e.g. Processor OC Unit
    srr = da.get(A["shieldRechargeRateMultiplier"])
    if srr is not None and srr > 1.0:
        pct = (srr - 1) * 100
        drawbacks.append(f"+{pct:.0f}% shield recharge time")

    return drawbacks

# ── Fitting skill definitions ─────────────────────────────────
# Each entry: (skill_name, type_id, bonus_attribute_id)
# The bonus_attribute_id is the dogma attribute on the skill item whose value
# is the per-level bonus.  The dogma engine scales it by skillLevel and applies
# it as PostPercent to the corresponding ship attribute.
#
# Values are read from ESI at runtime so we track changes automatically.
FITTING_SKILL_CPU = ("CPU Management", 3426, 424)        # cpuOutputBonus2 → cpuOutput
FITTING_SKILL_PG  = ("Power Grid Management", 3413, 313) # powerEngOutputBonus → powerOutput
AGILITY_SKILLS = [
    ("Spaceship Command", 3327, 151),   # agilityBonus = −2.0 per level
    ("Evasive Maneuvering", 3453, 151), # agilityBonus = −5.0 per level
]


def _skill_bonus_per_level(type_id, bonus_attr_id):
    """Read a skill's per-level bonus attribute from ESI.

    Returns the raw attribute value (e.g. 5.0 for +5 %/level, −5.0 for −5 %/level).
    Falls back to 0 if the lookup fails.
    """
    info = esi.get_type_info(type_id)
    if not info:
        return 0
    da = attrs_dict(info.get("dogma_attributes"))
    return da.get(bonus_attr_id, 0)


# ── Module seed names for group discovery ─────────────────────
# (slot_type, display_category, seed_module_name)
MODULE_SEEDS = [
    # High slots
    ("high", "Strip Miners", "Strip Miner I"),
    ("high", "Modulated Strip Miners", "Modulated Strip Miner II"),
    ("high", "Mining Lasers", "Miner I"),
    ("high", "Salvagers", "Salvager I"),
    ("high", "Tractor Beams", "Small Tractor Beam I"),
    ("high", "Cargo Scanners", "Cargo Scanner I"),
    ("high", "Industrial Cores", "Medium Industrial Core I"),   # group 515 also has Bastion/Siege — filtered below
    ("high", "Mining Foreman Bursts", "Mining Foreman Burst I"),
    ("high", "Remote Shield Boosters", "Small Remote Shield Booster I"),
    ("high", "Compressors", "Medium Asteroid Ore Compressor I"),

    # Mid slots
    ("mid", "Shield Extenders", "Medium Shield Extender I"),
    ("mid", "Shield Hardeners", "Multispectrum Shield Hardener I"),
    ("mid", "Shield Hardeners (EM)", "EM Shield Hardener I"),
    ("mid", "Shield Hardeners (Therm)", "Thermal Shield Hardener I"),
    ("mid", "Shield Hardeners (Kin)", "Kinetic Shield Hardener I"),
    ("mid", "Shield Hardeners (Exp)", "Explosive Shield Hardener I"),
    ("mid", "Shield Boosters", "Medium Shield Booster I"),
    ("mid", "Cap Batteries", "Medium Cap Battery I"),
    ("mid", "Afterburners", "10MN Afterburner I"),

    # Low slots
    ("low", "Mining Laser Upgrades", "Mining Laser Upgrade I"),
    ("low", "Drone Damage Amplifiers", "Drone Damage Amplifier I"),
    ("low", "Damage Controls", "Damage Control I"),
    ("low", "Inertial Stabilizers", "Inertial Stabilizers I"),
    ("low", "Nanofiber Internal Structures", "Nanofiber Internal Structure I"),
    ("low", "Cap Power Relays", "Capacitor Power Relay I"),
    ("low", "Cap Flux Coils", "Capacitor Flux Coil I"),
    ("low", "Reinforced Bulkheads", "Reinforced Bulkheads I"),

    # Rigs
    ("rig", "Shield Rigs", "Medium Core Defense Field Extender I"),
    ("rig", "Shield Rigs (resist)", "Medium EM Shield Reinforcer I"),
    ("rig", "Mining Drone Rigs", "Medium Drone Mining Augmentor I"),
    ("rig", "Engineering Rigs", "Medium Ancillary Current Router I"),
    ("rig", "Engineering Rigs (CPU)", "Medium Processor Overclocking Unit I"),
    ("rig", "Navigation Rigs", "Medium Hyperspatial Velocity Optimizer I"),
    ("rig", "Navigation Rigs (agility)", "Medium Polycarbon Engine Housing I"),
]

# Per-category effect columns: (display_name, attribute_id, format_type)
# format_type: "pct" = show as %, "flat" = flat number, "res" = resonance→resist%,
#              "pctval" = value is already a %, "mul" = multiplier shown as %
CATEGORY_COLUMNS = {
    "Mining Laser Upgrades": [
        ("Yield bonus", 434, "pctval"),       # miningAmountBonus
        ("CPU penalty", 1082, "pctval"),      # cpuPenaltyPercent
    ],
    "Drone Damage Amplifiers": [
        ("Drone dmg", 1255, "pctval"),        # droneDamageBonus
    ],
    "Shield Extenders": [
        ("HP bonus", 72, "flat"),             # capacityBonus
        ("Sig penalty", 983, "flat"),         # signatureRadiusAdd
    ],
    "Shield Hardeners": [
        ("EM resist", 984, "pctval"),         # emDamageResistanceBonus
        ("Therm resist", 987, "pctval"),      # thermalDamageResistanceBonus
        ("Kin resist", 986, "pctval"),        # kineticDamageResistanceBonus
        ("Exp resist", 985, "pctval"),        # explosiveDamageResistanceBonus
    ],
    "Damage Controls": [
        ("Shield res", 271, "res"),           # shieldEmDamageResonance (show EM as representative)
        ("Armor res", 267, "res"),            # armorEmDamageResonance
        ("Hull res", 974, "res"),             # hullEmDamageResonance
    ],
    "Inertial Stabilizers": [
        ("Agility", 169, "pctval"),           # agilityMultiplier
        ("Sig penalty", 554, "pctval"),       # signatureRadiusBonus
    ],
    "Nanofiber Internal Structures": [
        ("Agility", 169, "pctval"),           # agilityMultiplier
        ("Velocity", 1076, "pctval"),         # implantBonusVelocity
        ("Structure HP", 150, "mul"),         # structureHPMultiplier
    ],
    "Reinforced Bulkheads": [
        ("Structure HP", 150, "mul"),         # structureHPMultiplier
        ("Cargo penalty", 149, "mul"),        # cargoCapacityMultiplier
    ],
    "Cap Power Relays": [
        ("Cap recharge", 144, "mul"),         # capacitorRechargeRateMultiplier
        ("Shield boost penalty", 548, "pctval"),  # shieldBoostMultiplier
    ],
    "Cap Flux Coils": [
        ("Cap recharge", 144, "mul"),         # capacitorRechargeRateMultiplier
        ("Cap capacity", 147, "mul"),         # capacitorCapacityMultiplier
    ],
    "Cap Batteries": [
        ("Cap bonus", 67, "flat"),            # capacitorBonus (flat GJ)
        ("Neut resist", 2267, "pctval"),      # energyWarfareResistanceBonus
    ],
    "Shield Boosters": [
        ("Shield HP/cycle", 72, "flat"),      # capacityBonus (shield amount per cycle)
    ],
    "Afterburners": [
        ("Speed boost", 1076, "pctval"),      # implantBonusVelocity / speedFactor
    ],
}

DRONE_SEEDS = [
    ("Mining Drones", "Mining Drone I"),
    ("Light Combat Drones (Gallente)", "Hobgoblin I"),
    ("Light Combat Drones (Minmatar)", "Warrior I"),
    ("Light Combat Drones (Caldari)", "Hornet I"),
    ("Light Combat Drones (Amarr)", "Acolyte I"),
    ("Medium Combat Drones (Gallente)", "Hammerhead I"),
    ("Medium Combat Drones (Minmatar)", "Valkyrie I"),
    ("Medium Combat Drones (Caldari)", "Vespa I"),
    ("Medium Combat Drones (Amarr)", "Infiltrator I"),
    ("Salvage Drones", "Salvage Drone I"),
]

MAX_WORKERS = 8


# ── Utilities ─────────────────────────────────────────────────

def strip_html(text):
    """Remove HTML tags from a string."""
    return re.sub(r'<[^>]+>', '', text or "").strip()


def fmt_isk(v):
    """Format ISK value for display."""
    if v <= 0:
        return "--"
    if v >= 1e9:
        return f"{v/1e9:.2f}B"
    if v >= 1e6:
        return f"{v/1e6:.2f}M"
    if v >= 1e3:
        return f"{v/1e3:.1f}k"
    return f"{v:.2f}"


def fmt_pct(resonance):
    """Convert damage resonance (0-1) to resist % for display."""
    return f"{(1 - resonance) * 100:.0f}%"


def attrs_dict(dogma_attributes):
    """Convert ESI dogma_attributes list to {attribute_id: value} dict."""
    return {a["attribute_id"]: a["value"] for a in (dogma_attributes or [])}


def fmt_cat_val(value, fmt_type):
    """Format a category column value for display."""
    if value is None or value == 0:
        return "--"
    if fmt_type == "pctval":
        # Value is already a percentage (e.g. 5.0 = +5%, -25.0 = -25%)
        sign = "+" if value > 0 else ""
        return f"{sign}{value:.1f}%"
    if fmt_type == "flat":
        if abs(value) >= 1000:
            return f"{value:,.0f}"
        return f"{value:.1f}" if value != int(value) else f"{int(value)}"
    if fmt_type == "res":
        # Resonance multiplier → resist%. 0.925 → 7.5% resist
        return f"{(1 - value) * 100:.1f}%"
    if fmt_type == "mul":
        # Multiplier: 0.85 → -15%, 1.15 → +15%
        pct = (value - 1) * 100
        sign = "+" if pct > 0 else ""
        return f"{sign}{pct:.1f}%"
    return str(value)


def meta_label(attrs):
    """Determine T1/T2/Faction/Meta label from dogma attributes."""
    mg = attrs.get(A["metaGroupID"], 0)
    labels = {1: "T1", 2: "T2", 4: "Faction", 14: "T3"}
    if mg in labels:
        return labels[mg]
    ml = attrs.get(A["metaLevelOld"], 0)
    if ml > 0:
        return f"Meta {int(ml)}"
    return "T1"


# ── Skills ────────────────────────────────────────────────────

def load_skills(path):
    """Load skills from .ini file. Returns (char_info, skills_dict)."""
    config = configparser.ConfigParser()
    config.optionxform = str  # preserve case
    config.read(path, encoding="utf-8")

    char_info = {
        "name": config.get("character", "name", fallback="Unknown"),
        "last_updated": config.get("character", "last_updated", fallback="unknown"),
    }

    skills = {}
    if config.has_section("skills"):
        for name, value in config.items("skills"):
            try:
                skills[name] = int(value)
            except ValueError:
                pass

    return char_info, skills


def validate_skills(char_info, skills):
    """Validate skills and return list of warning strings."""
    warnings = []
    if not skills:
        warnings.append("No skills loaded — all skills will default to 0.")

    try:
        updated = datetime.date.fromisoformat(char_info["last_updated"])
        age_days = (datetime.date.today() - updated).days
        if age_days > 30:
            warnings.append(
                f"Skills last updated {age_days} days ago ({char_info['last_updated']}). "
                "Consider refreshing."
            )
    except (ValueError, TypeError):
        warnings.append("Could not parse last_updated date in skills file.")

    return warnings


def resolve_skill_ids(skills):
    """Resolve skill names to ESI type IDs. Returns {name: type_id}."""
    return esi.search_type_ids(list(skills.keys()))


# ── Ship hull ─────────────────────────────────────────────────

def resolve_ship(ship_name):
    """Resolve a ship name to its type_id."""
    result = esi.search_type_ids([ship_name])
    if ship_name in result:
        return result[ship_name]
    # Try case-insensitive by searching with title case
    result = esi.search_type_ids([ship_name.title()])
    if ship_name.title() in result:
        return result[ship_name.title()]
    return None


def find_ore_hold_attr(ship_attrs):
    """Discover the ore hold attribute ID from a ship's dogma attributes.

    Mining ships have a specialOreHoldCapacity attribute. The exact ID varies
    across SDE versions, so we discover it by checking candidates.
    """
    # Known candidates for ore hold attribute IDs
    for attr_id in [1556, 2655, 2659, 1555, 2653, 1653]:
        if attr_id in ship_attrs and ship_attrs[attr_id] > 500:
            return attr_id

    # Fallback: look for any large capacity attr that isn't cargo (38) or drone bay (283)
    regular_cargo = ship_attrs.get(38, 0)
    drone_bay = ship_attrs.get(283, 0)
    for attr_id, value in ship_attrs.items():
        if value > 1000 and attr_id not in (38, 283, 4) and value != regular_cargo and value != drone_bay:
            info = esi.get_dogma_attribute(attr_id)
            if info and ("ore" in info.get("name", "").lower() or
                         "mining" in info.get("name", "").lower()):
                return attr_id
    return None


def _extract_hull_bonuses(type_info, da, skills):
    """Extract structured hull bonuses from the SDE traits field.

    Uses the EVE Ref SDE API for canonical, human-readable bonus descriptions.
    Falls back to a minimal representation if the SDE is unavailable.

    Returns list of (section_name, [bonus_description_strings]).
    """
    type_id = type_info.get("type_id")
    traits = esi.get_type_traits(type_id) if type_id else {}

    skill_bonuses = []
    role_bonuses = []

    # ── Per-level skill bonuses (traits.types keyed by skill type ID) ──
    skill_types = traits.get("types", {})
    for skill_tid_str, bonuses in skill_types.items():
        # Resolve skill type ID to name for display
        skill_tid = int(skill_tid_str)
        skill_info = esi.get_type_info(skill_tid)
        skill_name = skill_info["name"] if skill_info else f"Skill {skill_tid}"
        level = skills.get(skill_name, 0)

        for _key, b in sorted(bonuses.items(), key=lambda x: x[1].get("importance", 0)):
            bonus = b.get("bonus")
            text = strip_html(b.get("bonus_text", {}).get("en", ""))
            if not text:
                continue

            if bonus is not None:
                sign = "+" if bonus > 0 else ""
                effective = bonus * level
                eff_sign = "+" if effective > 0 else ""
                desc = (f"{sign}{bonus:.0f}% {text} per {skill_name} level "
                        f"\u2192 at {skill_name} {level}: {eff_sign}{effective:.0f}%")
            else:
                desc = f"{text} (per {skill_name} level)"
            skill_bonuses.append(desc)

    # ── Role bonuses ──
    for _key, b in sorted(traits.get("role_bonuses", {}).items(),
                          key=lambda x: x[1].get("importance", 0)):
        bonus = b.get("bonus")
        text = strip_html(b.get("bonus_text", {}).get("en", ""))
        if not text:
            continue

        if bonus is not None:
            sign = "+" if bonus > 0 else ""
            desc = f"{sign}{bonus:.0f}% {text}"
        else:
            # Capability-style bonus (e.g. "can fit Medium Industrial Core")
            desc = text
        role_bonuses.append(desc)

    # ── Misc bonuses (rare) ──
    for _key, b in sorted(traits.get("misc_bonuses", {}).items(),
                          key=lambda x: x[1].get("importance", 0)):
        text = strip_html(b.get("bonus_text", {}).get("en", ""))
        if text:
            bonus = b.get("bonus")
            if bonus is not None:
                sign = "+" if bonus > 0 else ""
                role_bonuses.append(f"{sign}{bonus:.0f}% {text}")
            else:
                role_bonuses.append(text)

    sections = []
    if skill_bonuses:
        sections.append(("Ship skill bonuses (per level)", skill_bonuses))
    if role_bonuses:
        sections.append(("Role bonuses (always-on)", role_bonuses))

    return sections


def parse_ship_hull(type_id, skills, skill_ids):
    """Fetch and parse ship hull data from ESI.

    Returns a dict with all hull stats, both base and skill-adjusted.
    """
    info = esi.get_type_info(type_id)
    if not info:
        print(f"ERROR: Could not fetch type info for {type_id}", file=sys.stderr)
        sys.exit(1)

    da = attrs_dict(info.get("dogma_attributes"))

    # Slot layout
    hi = int(da.get(A["hiSlots"], 0))
    med = int(da.get(A["medSlots"], 0))
    lo = int(da.get(A["lowSlots"], 0))
    rig = int(da.get(A["rigSlots"], 0))
    turret = int(da.get(A["turretSlotsLeft"], 0))
    launcher = int(da.get(A["launcherSlotsLeft"], 0))
    calibration = da.get(A["upgradeCapacity"], 0)

    # Fitting resources (base)
    cpu_base = da.get(A["cpuOutput"], 0)
    pg_base = da.get(A["powerOutput"], 0)
    cap_capacity = da.get(A["capacitorCapacity"], 0)
    cap_recharge = da.get(A["rechargeRate"], 0)

    # Apply CPU Management and PG Management skill bonuses.
    # Read per-level bonus from ESI dogma (PostPercent on ship attribute).
    cpu_skill = skills.get("CPU Management", 0)
    pg_skill = skills.get("Power Grid Management", 0)
    cpu_bonus_per_level = _skill_bonus_per_level(*FITTING_SKILL_CPU[1:])
    pg_bonus_per_level = _skill_bonus_per_level(*FITTING_SKILL_PG[1:])
    cpu_adj = cpu_base * (1 + cpu_bonus_per_level * cpu_skill / 100)
    pg_adj = pg_base * (1 + pg_bonus_per_level * pg_skill / 100)

    # Drone
    drone_bay = da.get(A["droneBayCapacity"], 0)
    drone_bw = da.get(A["droneBandwidth"], 0)

    # Defence
    shield_hp = da.get(A["shieldCapacity"], 0)
    armor_hp = da.get(A["armorHP"], 0)
    structure_hp = da.get(A["hp"], 0)

    shield_res = {
        "EM": da.get(A["shieldEmDamageResonance"], 1),
        "Therm": da.get(A["shieldThermalDamageResonance"], 1),
        "Kin": da.get(A["shieldKineticDamageResonance"], 1),
        "Exp": da.get(A["shieldExplosiveDamageResonance"], 1),
    }
    armor_res = {
        "EM": da.get(A["armorEmDamageResonance"], 1),
        "Therm": da.get(A["armorThermalDamageResonance"], 1),
        "Kin": da.get(A["armorKineticDamageResonance"], 1),
        "Exp": da.get(A["armorExplosiveDamageResonance"], 1),
    }

    # Navigation
    mass = da.get(A["mass"], 0)
    agility_base = da.get(A["agility"], 0)
    max_vel = da.get(A["maxVelocity"], 0)
    warp_speed = da.get(A["warpSpeedMultiplier"], 0)
    sig_radius = da.get(A["signatureRadius"], 0)

    # Apply agility-modifying skills (PostPercent, multiplicative stacking)
    agility_adj = agility_base
    agility_skill_details = []
    for skill_name, skill_tid, bonus_attr in AGILITY_SKILLS:
        level = skills.get(skill_name, 0)
        if level > 0:
            bonus = _skill_bonus_per_level(skill_tid, bonus_attr)
            if bonus:
                factor = 1 + bonus * level / 100
                agility_adj *= factor
                agility_skill_details.append((skill_name, level, bonus))

    align_base = -math.log(0.25) * agility_base * mass / 1_000_000 if mass and agility_base else 0
    align_adj = -math.log(0.25) * agility_adj * mass / 1_000_000 if mass and agility_adj else 0

    # Targeting
    max_targets = int(da.get(A["maxLockedTargets"], 0))
    scan_res = da.get(A["scanResolution"], 0)

    # Cargo and special holds
    cargo = da.get(A["capacity"], 0)
    ore_hold_attr = find_ore_hold_attr(da)
    ore_hold_base = da.get(ore_hold_attr, 0) if ore_hold_attr else 0
    fleet_hangar = da.get(A["fleetHangarCapacity"], 0)
    fuel_bay = da.get(A["specialFuelBayCapacity"], 0)

    # Get group info for ship class name (needed for ore hold skill selection)
    group_info = esi.get_group_info(info.get("group_id", 0))
    ship_class = group_info.get("name", "Unknown") if group_info else "Unknown"

    # Ore hold skill bonus — depends on ship class
    # Mining Barges: "Mining Barge" +5%/level; ICS: "Industrial Command Ships" +5%/level
    ore_hold_skill_name = None
    ore_hold_skill_level = 0
    ore_hold_bonus_pct = 0
    if ship_class == "Industrial Command Ship":
        ore_hold_skill_name = "Industrial Command Ships"
    elif ship_class == "Mining Barge":
        ore_hold_skill_name = "Mining Barge"
    elif ship_class == "Exhumer":
        ore_hold_skill_name = "Exhumers"
    if ore_hold_skill_name:
        ore_hold_skill_level = skills.get(ore_hold_skill_name, 0)
        # Read the per-level bonus from hull's dogma (attr 3187 for barges, varies per hull)
        # Use 5% default if present, matching the hull bonus pattern
        ore_hold_bonus_pct = 5
    ore_hold_adj = ore_hold_base * (1 + ore_hold_bonus_pct * ore_hold_skill_level / 100) if ore_hold_base > 0 else 0

    # Description (contains trait/bonus text)
    description = strip_html(info.get("description", ""))

    return {
        "type_id": type_id,
        "name": info["name"],
        "group_id": info.get("group_id", 0),
        "ship_class": ship_class,
        "description": description,

        "hi_slots": hi, "med_slots": med, "low_slots": lo, "rig_slots": rig,
        "turret_hardpoints": turret, "launcher_hardpoints": launcher,
        "calibration": calibration,

        "cpu_base": cpu_base, "cpu_adj": cpu_adj, "cpu_skill": cpu_skill,
        "pg_base": pg_base, "pg_adj": pg_adj, "pg_skill": pg_skill,
        "cap_capacity": cap_capacity, "cap_recharge": cap_recharge,

        "drone_bay": drone_bay, "drone_bw": drone_bw,

        "shield_hp": shield_hp, "armor_hp": armor_hp, "structure_hp": structure_hp,
        "shield_res": shield_res, "armor_res": armor_res,

        "mass": mass, "agility": agility_base, "agility_adj": agility_adj,
        "max_vel": max_vel, "warp_speed": warp_speed, "sig_radius": sig_radius,
        "align_time": align_base, "align_time_adj": align_adj,
        "agility_skill_details": agility_skill_details,
        "cpu_bonus_per_level": cpu_bonus_per_level,
        "pg_bonus_per_level": pg_bonus_per_level,
        "max_targets": max_targets, "scan_res": scan_res,

        "cargo": cargo, "ore_hold_base": ore_hold_base, "ore_hold_adj": ore_hold_adj,
        "ore_hold_attr": ore_hold_attr,
        "ore_hold_skill_name": ore_hold_skill_name,
        "ore_hold_skill_level": ore_hold_skill_level,
        "ore_hold_bonus_pct": ore_hold_bonus_pct,
        "fleet_hangar": fleet_hangar, "fuel_bay": fuel_bay,

        "structured_bonuses": _extract_hull_bonuses(info, da, skills),
        "raw_attrs": da,
    }


# ── Module group discovery ────────────────────────────────────

def discover_module_groups(progress=True):
    """Resolve seed module names to group IDs.

    Returns:
        module_groups: dict of (slot_type, category_name) -> group_id
        drone_groups: dict of category_name -> group_id
    """
    all_seed_names = [s[2] for s in MODULE_SEEDS] + [s[1] for s in DRONE_SEEDS]

    if progress:
        print("  Resolving module names...", end="", flush=True)
    name_to_id = esi.search_type_ids(all_seed_names)
    if progress:
        print(f" {len(name_to_id)}/{len(all_seed_names)} resolved", flush=True)

    # Fetch type info in parallel to get group_ids
    type_ids_needed = list(name_to_id.values())
    type_infos = {}

    if progress:
        print("  Fetching seed type info...", end="", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(esi.get_type_info, tid): tid for tid in type_ids_needed}
        for future in concurrent.futures.as_completed(futures):
            tid = futures[future]
            type_infos[tid] = future.result()
    if progress:
        print(" done", flush=True)

    # Build module group mapping (deduplicate by group_id per slot)
    module_groups = {}
    seen_groups = {}  # slot_type -> set of group_ids (to avoid duplicates)
    for slot_type, category, seed_name in MODULE_SEEDS:
        tid = name_to_id.get(seed_name)
        if not tid or tid not in type_infos or not type_infos[tid]:
            if progress:
                print(f"  WARNING: Seed '{seed_name}' not found, skipping {category}")
            continue
        gid = type_infos[tid]["group_id"]
        if slot_type not in seen_groups:
            seen_groups[slot_type] = set()
        if gid in seen_groups[slot_type]:
            continue  # already have this group for this slot
        seen_groups[slot_type].add(gid)
        module_groups[(slot_type, category)] = gid

    drone_groups = {}
    seen_drone_gids = set()
    for category, seed_name in DRONE_SEEDS:
        tid = name_to_id.get(seed_name)
        if not tid or tid not in type_infos or not type_infos[tid]:
            continue
        gid = type_infos[tid]["group_id"]
        if gid in seen_drone_gids:
            continue
        seen_drone_gids.add(gid)
        drone_groups[category] = gid

    return module_groups, drone_groups


# ── Skill requirement checking ────────────────────────────────

def check_skill_reqs(mod_attrs, skills, skill_ids):
    """Check if a module's skill prerequisites are met.

    Returns (all_met: bool, missing: list of "SkillName Level" strings).
    """
    id_to_name = {v: k for k, v in skill_ids.items()}
    missing = []

    for i in range(1, 4):
        skill_type_id = mod_attrs.get(A.get(f"requiredSkill{i}"))
        req_level = mod_attrs.get(A.get(f"requiredSkill{i}Level"))
        if not skill_type_id or not req_level:
            continue
        skill_type_id = int(skill_type_id)
        req_level = int(req_level)

        # Look up skill name
        skill_name = id_to_name.get(skill_type_id)
        if not skill_name:
            # Not in our skills file — look up from ESI
            skill_info = esi.get_type_info(skill_type_id)
            if skill_info:
                skill_name = skill_info["name"]

        if skill_name:
            current = skills.get(skill_name, 0)
            if current < req_level:
                missing.append(f"{skill_name} {req_level}")
        else:
            missing.append(f"Unknown Skill (type {skill_type_id}) {req_level}")

    return len(missing) == 0, missing


# ── Module candidate enumeration ──────────────────────────────

def enumerate_slot_candidates(slot_type, module_groups, ship, skills, skill_ids,
                              regions, use_cache, progress=True):
    """Enumerate all module candidates for a given slot type.

    Returns list of category dicts: {name, group_id, candidates: [...]}.
    """
    categories = []

    for (st, cat_name), group_id in module_groups.items():
        if st != slot_type:
            continue

        if progress:
            print(f"  {cat_name}...", end="", flush=True)

        # Fetch group to get all type_ids
        group_info = esi.get_group_info(group_id)
        if not group_info:
            if progress:
                print(" group fetch failed", flush=True)
            continue

        all_type_ids = group_info.get("types", [])

        # Fetch type info in parallel
        type_data = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(esi.get_type_info, tid): tid for tid in all_type_ids}
            for future in concurrent.futures.as_completed(futures):
                tid = futures[future]
                info = future.result()
                if info and info.get("published"):
                    type_data[tid] = info

        # Size prefix for hull-restricted categories (Porpoise=Medium, Orca=Large)
        # rig_size attr 1547: 1=Small, 2=Medium, 3=Large, 4=Capital
        SIZE_LABELS = {1: "Small", 2: "Medium", 3: "Large", 4: "Capital"}
        hull_size = SIZE_LABELS.get(int(ship["raw_attrs"].get(1547, 0)), "")
        # Categories where candidates must match the hull's size prefix
        SIZE_FILTERED_CATS = {"Industrial Cores", "Compressors"}

        # Filter candidates
        filtered = []
        for tid, info in type_data.items():
            da = attrs_dict(info.get("dogma_attributes"))

            # Skip officer (5) and deadspace (6) modules
            mg = da.get(A["metaGroupID"], 0)
            if mg in (5, 6):
                continue

            # Size filter: for categories sharing groups with other ship classes,
            # only include modules whose name starts with the hull's size prefix
            if cat_name in SIZE_FILTERED_CATS and hull_size:
                mod_name = info.get("name", "")
                if not mod_name.startswith(hull_size):
                    continue

            # Skip if single module exceeds ship's total CPU or PG
            cpu = da.get(A["cpu"], 0)
            pg = da.get(A["power"], 0)
            if cpu > ship["cpu_adj"] or pg > ship["pg_adj"]:
                continue

            # For rigs, check calibration
            if slot_type == "rig":
                cal = da.get(A["upgradeCost"], 0)
                if cal > ship["calibration"]:
                    continue

            filtered.append((tid, info, da))

        # Fetch prices in parallel (both buy and sell per region)
        price_data = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {}
            for tid, info, da in filtered:
                for region_id, region_label in regions:
                    futures[pool.submit(esi.fetch_best_sell, region_id, tid, use_cache)] = (tid, region_label, "sell")
                    futures[pool.submit(esi.fetch_best_buy, region_id, tid, use_cache)] = (tid, region_label, "buy")

            for future in concurrent.futures.as_completed(futures):
                tid, label, side = futures[future]
                price, _ = future.result()
                if tid not in price_data:
                    price_data[tid] = {}
                price_data[tid][f"{side}_{label}"] = price

        # Build candidate entries
        candidates = []
        for tid, info, da in filtered:
            reqs_met, missing = check_skill_reqs(da, skills, skill_ids)

            entry = {
                "type_id": tid,
                "name": info["name"],
                "variant": meta_label(da),
                "cpu": da.get(A["cpu"], 0),
                "pg": da.get(A["power"], 0),
                "reqs_met": reqs_met,
                "missing_skills": missing,
                "prices": price_data.get(tid, {}),
            }

            # Slot-type-specific stats
            if slot_type == "rig":
                entry["calibration"] = da.get(A["upgradeCost"], 0)

            # Mining stats
            mining_amt = da.get(A["miningAmount"])
            if mining_amt:
                entry["yield_per_cycle"] = mining_amt
                duration = da.get(A["duration"], 0)
                entry["cycle_time"] = duration / 1000 if duration else 0

            # Cap usage
            cap_need = da.get(A["capacitorNeed"], 0)
            if cap_need > 0:
                entry["cap_need"] = cap_need

            # Duration (for active modules)
            duration = da.get(A["duration"], 0)
            if duration and not mining_amt:
                entry["cycle_time"] = duration / 1000

            # Store all dogma attributes for category column lookup
            entry["dogma"] = da

            # Extract drawbacks (rig/module penalties)
            entry["drawbacks"] = _extract_drawbacks(da, info.get("dogma_effects"))

            candidates.append(entry)

        # Sort: T1 first, then meta, T2, faction
        sort_order = {"T1": 0, "T2": 2, "Faction": 3}
        candidates.sort(key=lambda c: (
            sort_order.get(c["variant"], 1),
            c["name"],
        ))

        if progress:
            print(f" {len(candidates)} candidates", flush=True)

        categories.append({
            "name": cat_name,
            "group_id": group_id,
            "candidates": candidates,
        })

    return categories


# ── Drone enumeration ─────────────────────────────────────────

def enumerate_drones(drone_groups, ship, skills, skill_ids, regions, use_cache,
                     progress=True):
    """Enumerate drone candidates that fit the ship's bay/bandwidth."""
    categories = []

    for cat_name, group_id in drone_groups.items():
        if progress:
            print(f"  {cat_name}...", end="", flush=True)

        group_info = esi.get_group_info(group_id)
        if not group_info:
            continue

        all_type_ids = group_info.get("types", [])

        # Fetch type info
        type_data = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {pool.submit(esi.get_type_info, tid): tid for tid in all_type_ids}
            for future in concurrent.futures.as_completed(futures):
                tid = futures[future]
                info = future.result()
                if info and info.get("published"):
                    type_data[tid] = info

        # Filter and price
        filtered = []
        for tid, info in type_data.items():
            da = attrs_dict(info.get("dogma_attributes"))
            mg = da.get(A["metaGroupID"], 0)
            if mg in (5, 6):
                continue
            volume = info.get("volume", 0)
            # Include if drone fits in bay (volume check) — single drone
            if volume > ship["drone_bay"] and ship["drone_bay"] > 0:
                continue
            filtered.append((tid, info, da))

        # Prices (buy + sell per region)
        price_data = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {}
            for tid, info, da in filtered:
                for region_id, label in regions:
                    futures[pool.submit(esi.fetch_best_sell, region_id, tid, use_cache)] = (tid, label, "sell")
                    futures[pool.submit(esi.fetch_best_buy, region_id, tid, use_cache)] = (tid, label, "buy")
            for future in concurrent.futures.as_completed(futures):
                tid, label, side = futures[future]
                price, _ = future.result()
                if tid not in price_data:
                    price_data[tid] = {}
                price_data[tid][f"{side}_{label}"] = price

        candidates = []
        for tid, info, da in filtered:
            reqs_met, missing = check_skill_reqs(da, skills, skill_ids)
            volume = info.get("volume", 0)
            bw = da.get(A["droneBandwidth"], 0)

            entry = {
                "type_id": tid,
                "name": info["name"],
                "variant": meta_label(da),
                "volume": volume,
                "bandwidth": bw,
                "reqs_met": reqs_met,
                "missing_skills": missing,
                "prices": price_data.get(tid, {}),
            }

            mining_amt = da.get(A["miningAmount"])
            if mining_amt:
                entry["yield_per_cycle"] = mining_amt
                duration = da.get(A["duration"], 0)
                entry["cycle_time"] = duration / 1000 if duration else 0

            candidates.append(entry)

        candidates.sort(key=lambda c: (
            {"T1": 0, "T2": 2, "Faction": 3}.get(c["variant"], 1),
            c["name"],
        ))

        if progress:
            print(f" {len(candidates)} drones", flush=True)

        categories.append({
            "name": cat_name,
            "group_id": group_id,
            "candidates": candidates,
        })

    return categories


# ── Hull cost ─────────────────────────────────────────────────

def fetch_hull_prices(type_id, regions, use_cache):
    """Fetch buy and sell prices for the hull in each region."""
    prices = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {}
        for region_id, label in regions:
            futures[pool.submit(esi.fetch_best_buy, region_id, type_id, use_cache)] = (label, "buy")
            futures[pool.submit(esi.fetch_best_sell, region_id, type_id, use_cache)] = (label, "sell")
        for future in concurrent.futures.as_completed(futures):
            label, side = futures[future]
            price, _ = future.result()
            if label not in prices:
                prices[label] = {}
            prices[label][side] = price
    return prices


# ── Markdown formatting ───────────────────────────────────────

def format_dossier(ship, candidates, drones, hull_prices, goal, region_key,
                   char_info, skills, skill_warnings, regions, include_jita,
                   ore_context=None):
    """Format the complete dossier as markdown."""
    now = datetime.datetime.now().isoformat(timespec="seconds")
    region_name = esi.REGIONS.get(region_key, {}).get("name", region_key)
    n_skills = len(skills)

    lines = []
    w = lines.append  # shorthand

    w(f"# Fit Dossier — {ship['name']}")
    w("")
    w(f"Generated: {now}")
    w(f"Goal: {goal}")
    w(f"Region: {region_name} (price source for \"local\")")
    w(f"Skills profile: {char_info['name']}, last updated {char_info['last_updated']}, "
      f"{n_skills} skills loaded")
    for warn in skill_warnings:
        w(f"WARNING: {warn}")
    w("")
    w("---")
    w("")

    # ── Hull section ──
    w("## Hull")
    w("")
    w(f"Type ID: {ship['type_id']}")
    w(f"Class: {ship['ship_class']}")
    w("")

    # Hull cost
    w("### Hull cost")
    w("")
    w("| Source | Best buy | Best sell | Spread |")
    w("|--------|--------:|---------:|-------:|")
    for region_id, label in regions:
        p = hull_prices.get(label, {})
        buy = p.get("buy", 0)
        sell = p.get("sell", 0)
        spread = f"{((sell - buy) / sell * 100):.1f}%" if sell > 0 and buy > 0 else "--"
        w(f"| {label} | {fmt_isk(buy)} | {fmt_isk(sell)} | {spread} |")
    w("")

    # Slot layout
    w("### Slot layout")
    w("")
    w(f"- High slots: {ship['hi_slots']} (turret hardpoints: {ship['turret_hardpoints']}, "
      f"launcher: {ship['launcher_hardpoints']})")
    w(f"- Mid slots: {ship['med_slots']}")
    w(f"- Low slots: {ship['low_slots']}")
    w(f"- Rig slots: {ship['rig_slots']} (calibration budget: {ship['calibration']:.0f})")
    w("")

    # Fitting resources
    w("### Fitting resources (base / skill-adjusted)")
    w("")
    w(f"- CPU: {ship['cpu_base']:.0f} / {ship['cpu_adj']:.1f} tf  "
      f"(CPU Mgmt {ship['cpu_skill']}: +{ship['cpu_bonus_per_level']:.0f}%/level)")
    w(f"- Power grid: {ship['pg_base']:.0f} / {ship['pg_adj']:.1f} MW  "
      f"(PG Mgmt {ship['pg_skill']}: +{ship['pg_bonus_per_level']:.0f}%/level)")
    w(f"- Drone bay: {ship['drone_bay']:.0f} m\u00b3")
    w(f"- Drone bandwidth: {ship['drone_bw']:.0f} Mbit/s")
    w("")

    # Cargo / mining holds
    w("### Cargo / mining holds")
    w("")
    w(f"- Cargo: {ship['cargo']:.0f} m\u00b3")
    if ship["ore_hold_base"] > 0:
        sk = ship.get("ore_hold_skill_name", "")
        sl = ship.get("ore_hold_skill_level", 0)
        bp = ship.get("ore_hold_bonus_pct", 0)
        w(f"- Mining hold: {ship['ore_hold_base']:,.0f} / {ship['ore_hold_adj']:,.0f} m\u00b3"
          + (f"  ({sk} {sl}: +{sl * bp}%)" if sk else ""))
    if ship.get("fleet_hangar", 0) > 0:
        w(f"- Fleet hangar: {ship['fleet_hangar']:,.0f} m\u00b3")
    if ship.get("fuel_bay", 0) > 0:
        w(f"- Fuel bay: {ship['fuel_bay']:,.0f} m\u00b3  (used by Industrial Core / Bursts)")
    w("")

    # Defence
    w("### Defence (base hull)")
    w("")
    sr = ship["shield_res"]
    ar = ship["armor_res"]
    w(f"- Shield: {ship['shield_hp']:,.0f} HP (resists: "
      f"EM {fmt_pct(sr['EM'])}, Therm {fmt_pct(sr['Therm'])}, "
      f"Kin {fmt_pct(sr['Kin'])}, Exp {fmt_pct(sr['Exp'])})")
    w(f"- Armor: {ship['armor_hp']:,.0f} HP (resists: "
      f"EM {fmt_pct(ar['EM'])}, Therm {fmt_pct(ar['Therm'])}, "
      f"Kin {fmt_pct(ar['Kin'])}, Exp {fmt_pct(ar['Exp'])})")
    w(f"- Structure: {ship['structure_hp']:,.0f} HP")
    w("")

    # Hull bonuses
    w("### Hull bonuses (applied at current skills)")
    w("")
    if ship.get("structured_bonuses"):
        for section_name, bonuses in ship["structured_bonuses"]:
            w(f"**{section_name}:**")
            for bonus in bonuses:
                w(f"- {bonus}")
            w("")
    if ship["description"]:
        w("**Ship description (for additional context):**")
        w(f"> {ship['description'][:1500]}")
        w("")

    # Navigation
    w("### Navigation")
    w("")
    w(f"- Mass: {ship['mass']:,.0f} kg")
    w(f"- Inertia modifier: {ship['agility']:.4f} (base)"
      + (f" / {ship['agility_adj']:.4f} (skill-adjusted)"
         if ship.get('agility_skill_details') else ""))
    w(f"- Max velocity: {ship['max_vel']:.0f} m/s")
    w(f"- Align time: {ship['align_time']:.1f}s (base)"
      + (f" / {ship['align_time_adj']:.1f}s (skill-adjusted)"
         if ship.get('agility_skill_details') else ""))
    if ship.get('agility_skill_details'):
        for sname, slevel, sbonus in ship['agility_skill_details']:
            sign = "+" if sbonus > 0 else ""
            w(f"  - {sname} {slevel}: {sign}{sbonus:.0f}%/level")
    w(f"- Warp speed: {ship['warp_speed']:.1f} AU/s")
    w(f"- Signature radius: {ship['sig_radius']:.0f} m")
    w("")
    w("---")
    w("")

    # ── Module candidates ──
    w("## Module candidates")
    w("")

    slot_labels = {
        "high": f"High slots ({ship['hi_slots']} slots, {ship['turret_hardpoints']} turret hardpoints)",
        "mid": f"Mid slots ({ship['med_slots']} slots)",
        "low": f"Low slots ({ship['low_slots']} slots)",
        "rig": f"Rig slots ({ship['rig_slots']} slots, {ship['calibration']:.0f} calibration)",
    }

    region_labels = [label for _, label in regions]

    for slot_type in ("high", "mid", "low", "rig"):
        cats = candidates.get(slot_type, [])
        if not cats:
            continue

        # For Industrial Command Ships, reorder high-slot categories:
        # utility modules (cores, bursts, compressors) first, lasers last
        if slot_type == "high" and ship["ship_class"] == "Industrial Command Ship":
            laser_cats = {"Strip Miners", "Modulated Strip Miners", "Mining Lasers"}
            cats = sorted(cats, key=lambda c: 1 if c["name"] in laser_cats else 0)

        w(f"### {slot_labels[slot_type]}")
        w("")

        for cat in cats:
            # Flag laser categories on ICS hulls
            if (ship["ship_class"] == "Industrial Command Ship"
                    and cat["name"] in ("Strip Miners", "Modulated Strip Miners", "Mining Lasers")):
                w(f"#### {cat['name']}  *(no hull laser bonus — drone yield is primary)*")
            else:
                w(f"#### {cat['name']}")
            w("")

            if not cat["candidates"]:
                w("*No candidates found.*")
                w("")
                continue

            # Determine which columns to show
            has_yield = any(c.get("yield_per_cycle") for c in cat["candidates"])
            has_cal = slot_type == "rig"
            has_drawback = any(c.get("drawbacks") for c in cat["candidates"])
            cat_cols = CATEGORY_COLUMNS.get(cat["name"], [])

            headers = ["Module", "Variant"]
            aligns = ["l", "l"]
            if has_yield:
                headers += ["Yield/cycle", "Cycle"]
                aligns += ["r", "r"]
            for col_name, _, _ in cat_cols:
                headers.append(col_name)
                aligns.append("r")
            if has_drawback:
                headers.append("Drawback")
                aligns.append("l")
            headers += ["CPU", "PG"]
            aligns += ["r", "r"]
            if has_cal:
                headers += ["Cal"]
                aligns += ["r"]
            headers += ["Reqs met?"]
            aligns += ["c"]
            for label in region_labels:
                headers.append(f"Sell ({label.split()[0]})")
                aligns.append("r")

            sep_parts = ["---:" if a == "r" else ":---:" if a == "c" else "---"
                         for a in aligns]

            w("| " + " | ".join(headers) + " |")
            w("| " + " | ".join(sep_parts) + " |")

            for c in cat["candidates"]:
                row = [c["name"], c["variant"]]
                if has_yield:
                    yld = c.get("yield_per_cycle")
                    cyc = c.get("cycle_time")
                    row.append(f"{yld:.0f} m\u00b3" if yld else "--")
                    row.append(f"{cyc:.0f}s" if cyc else "--")
                dogma = c.get("dogma", {})
                for _, attr_id, fmt_type in cat_cols:
                    val = dogma.get(attr_id)
                    row.append(fmt_cat_val(val, fmt_type))
                if has_drawback:
                    dbs = c.get("drawbacks", [])
                    row.append("; ".join(dbs) if dbs else "\u2014")
                row.append(f"{c['cpu']:.0f}")
                row.append(f"{c['pg']:.0f}")
                if has_cal:
                    row.append(f"{c.get('calibration', 0):.0f}")
                if c["reqs_met"]:
                    row.append("\u2713")
                else:
                    row.append("\u2717 (" + ", ".join(c["missing_skills"]) + ")")
                for label in region_labels:
                    sell = c["prices"].get(f"sell_{label}", 0)
                    row.append(fmt_isk(sell))

                w("| " + " | ".join(row) + " |")

            w("")

    w("---")
    w("")

    # ── Drones ──
    w("## Drones")
    w("")
    w(f"Drone bay: {ship['drone_bay']:.0f} m\u00b3, bandwidth: {ship['drone_bw']:.0f} Mbit/s")
    if ship["ship_class"] == "Industrial Command Ship":
        ics_bonus = ship["raw_attrs"].get(3221, 0)
        ics_level = skills.get("Industrial Command Ships", 0)
        if ics_bonus:
            w(f"Hull bonus: +{ics_bonus:.0f}% drone ore mining yield per ICS level "
              f"(at ICS {ics_level}: +{ics_bonus * ics_level:.0f}%)")
    w("")

    for cat in drones:
        w(f"### {cat['name']}")
        w("")

        if not cat["candidates"]:
            w("*No candidates found.*")
            w("")
            continue

        has_yield = any(c.get("yield_per_cycle") for c in cat["candidates"])

        headers = ["Drone", "Variant"]
        aligns = ["l", "l"]
        if has_yield:
            headers += ["Yield/cycle", "Cycle"]
            aligns += ["r", "r"]
        headers += ["Volume", "BW", "Reqs met?"]
        aligns += ["r", "r", "c"]
        for label in region_labels:
            headers.append(f"Sell ({label.split()[0]})")
            aligns.append("r")

        sep_parts = ["---:" if a == "r" else ":---:" if a == "c" else "---" for a in aligns]
        w("| " + " | ".join(headers) + " |")
        w("| " + " | ".join(sep_parts) + " |")

        # Drone yield hull bonus — ICS hulls get +X% drone mining yield per level
        drone_yield_bonus = 0
        drone_yield_skill = ""
        if ship["ship_class"] == "Industrial Command Ship":
            ics_bonus = ship["raw_attrs"].get(3221, 0)  # drone ore mining yield per level
            ics_level = skills.get("Industrial Command Ships", 0)
            if ics_bonus and ics_level:
                drone_yield_bonus = ics_bonus * ics_level / 100
                drone_yield_skill = f"ICS {ics_level}"

        for c in cat["candidates"]:
            row = [c["name"], c["variant"]]
            if has_yield:
                yld = c.get("yield_per_cycle")
                cyc = c.get("cycle_time")
                if yld and drone_yield_bonus:
                    yld_adj = yld * (1 + drone_yield_bonus)
                    row.append(f"{yld:.0f} ({yld_adj:.0f}) m\u00b3")
                else:
                    row.append(f"{yld:.0f} m\u00b3" if yld else "--")
                row.append(f"{cyc:.0f}s" if cyc else "--")
            row.append(f"{c['volume']:.0f} m\u00b3")
            row.append(f"{c['bandwidth']:.0f}")
            if c["reqs_met"]:
                row.append("\u2713")
            else:
                row.append("\u2717 (" + ", ".join(c["missing_skills"]) + ")")
            for label in region_labels:
                sell = c["prices"].get(f"sell_{label}", 0)
                row.append(fmt_isk(sell))
            w("| " + " | ".join(row) + " |")

        w("")

    w("---")
    w("")

    # ── Ore market context ──
    if ore_context:
        w("## Market context")
        w("")
        w(f"### Top ores in {region_name} (right now, by ISK/m\u00b3)")
        w("")
        w("| # | Ore | ISK/m\u00b3 | Best buy at | Demand (units) |")
        w("|--:|-----|-------:|-------------|---------------:|")
        for i, ore in enumerate(ore_context[:10], 1):
            name = ore.get("name", "?")
            isk_m3 = ore.get("isk_m3", 0)
            sys_name = ore.get("system_name", "--")
            demand = ore.get("demand", 0)
            demand_str = f"{demand:,.0f}" if demand else "--"
            w(f"| {i} | {name} | {isk_m3:.2f} | {sys_name} | {demand_str} |")
        w("")
        w("**Notes for ISK/hr calculation:**")
        w("")
        w("- Multiply yield (m\u00b3/min) \u00d7 top ore ISK/m\u00b3 \u00d7 60 for ISK/hr.")
        w("- For \"set and forget\" mining, weight toward ores with high local demand "
          "rather than top ISK/m\u00b3 if travel cost matters.")
        w("")
        w("---")
        w("")

    # ── Stacking penalty reference ──
    w("## Stacking penalty reference")
    w("")
    w("Modules sharing the same effect are subject to diminishing returns:")
    w("")
    w("| # | Effectiveness |")
    w("|--:|-------------:|")
    penalties = [1.0, 0.869, 0.571, 0.283, 0.106]
    for i, p in enumerate(penalties):
        w(f"| {i+1} | {p*100:.1f}% |")
    w("")
    w("**Groups that stack with each other:**")
    w("")
    w("- Mining Laser Upgrades: stack with each other (mining yield bonus)")
    w("- Drone Damage Amplifiers: stack with each other (drone damage/yield bonus)")
    w("- Shield Hardeners: same damage type stacks (EM+EM stack, EM+Therm don't)")
    w("- Shield Extenders: **do NOT stack-pen** (flat HP)")
    w("- Inertia Stabilizers: stack with each other (agility bonus)")
    w("")
    w("**Cross-effect note:** Two MLUs and one DDA do NOT stack-pen each other "
      "(different effects). Three MLUs do.")
    w("")
    w("---")
    w("")

    # ── Goal ──
    w("## Goal (verbatim)")
    w("")
    w(f"> {goal}")
    w("")
    w("---")
    w("")

    # ── Notes for AI ──
    w("## Notes for the AI step")
    w("")
    w("- Skill prerequisites that are unmet are flagged with \u2717. Don't propose "
      "modules requiring untrained skills as the primary recommendation; flag them "
      "as upgrade paths.")
    w("- Stacking penalty applies per *effect*, not per module type.")
    w("- Faction modules' prices fluctuate. If a faction module's local price "
      "beats T2's Jita price, that may be a contract dump — flag it.")
    w("- The tool deliberately enumerates cross-discipline modules (DDAs in low, "
      "AB in mid, etc.). Use them or dismiss them, but consider them.")
    w("- PyFA is the verification step. Propose 3-5 ranked candidates; Campbell "
      "pastes them into PyFA for ground-truth.")
    w("- **Module/rig drawbacks affect fit budgets.** The Drawback column in each "
      "table shows penalties that are not included in the module's own CPU/PG cost:")
    w("  - Mining Laser Upgrades increase the CPU usage of the mining lasers they "
      "upgrade (stack-penned). When fitting MLUs, recompute strip miner CPU as "
      "base \u00d7 (1 + first_penalty) \u00d7 (1 + second_penalty \u00d7 0.869).")
    w("  - Drone Mining Augmentor rigs reduce ship CPU output. Subtract the "
      "drawback from total CPU before computing fit budget.")
    w("  - Rigs may have other drawbacks (reduced armor HP, increased sig radius, "
      "etc.). These are shown in the Drawback column for each rig.")
    w("- Output proposed fits as EFT blocks so they can be pasted directly into PyFA.")

    # Ship-class-specific notes
    if ship["ship_class"] == "Industrial Command Ship":
        w("- **This hull's primary mining yield comes from drones, not lasers.** "
          "The drone yield hull bonus is reflected in the drone yield columns. "
          "Lasers have no skill bonus on this hull and are generally not the right "
          "choice for solo use.")
        w("- Industrial Core must be active to use compression modules. It consumes "
          "fuel from the fuel bay and prevents warping while active.")

    w("")

    return "\n".join(lines)


# ── Web API data generator ────────────────────────────────────

def generate_dossier_data(ship_name, goal="", region_key="verge",
                          skills_path=None, include_jita=True, use_cache=True):
    """Run the full dossier pipeline and return JSON-serializable data.

    Used by the web UI (ore_scanner.py) to serve the fitter tab.
    """
    if skills_path is None:
        skills_path = os.path.join(os.path.dirname(__file__), "skills.ini")

    # Load skills
    char_info, skills = load_skills(skills_path)
    skill_warnings = validate_skills(char_info, skills)

    # Resolve skill IDs
    skill_ids = resolve_skill_ids(skills)

    # Build region list
    if region_key not in esi.REGIONS:
        return {"error": f"Unknown region: {region_key}"}
    local_region_id = esi.REGIONS[region_key]["id"]
    local_label = esi.REGIONS[region_key]["name"]
    regions = [(local_region_id, local_label)]
    if include_jita and region_key != "jita":
        regions.append((esi.REGIONS["jita"]["id"], "Jita (The Forge)"))

    # Resolve ship
    ship_type_id = resolve_ship(ship_name)
    if not ship_type_id:
        return {"error": f"Ship '{ship_name}' not found in ESI."}

    # Parse ship hull
    ship = parse_ship_hull(ship_type_id, skills, skill_ids)

    # Hull prices
    hull_prices = fetch_hull_prices(ship_type_id, regions, use_cache)

    # Discover module groups
    module_groups, drone_groups = discover_module_groups(progress=False)

    # Enumerate candidates per slot
    candidates = {}
    for slot_type in ("high", "mid", "low", "rig"):
        candidates[slot_type] = enumerate_slot_candidates(
            slot_type, module_groups, ship, skills, skill_ids,
            regions, use_cache, progress=False,
        )

    # Drones
    drones = enumerate_drones(drone_groups, ship, skills, skill_ids,
                              regions, use_cache, progress=False)

    # Format markdown dossier (before modifying dicts for JSON)
    region_labels = [label for _, label in regions]
    markdown = format_dossier(
        ship, candidates, drones, hull_prices, goal,
        region_key, char_info, skills, skill_warnings, regions, include_jita,
    )

    # Ensure dogma dicts have string keys for JSON serialisation
    def _strkeys(d):
        return {str(k): v for k, v in d.items()} if d else {}

    ship_out = dict(ship)
    ship_out["raw_attrs"] = _strkeys(ship_out.get("raw_attrs", {}))

    for slot_type in candidates:
        for cat in candidates[slot_type]:
            for c in cat.get("candidates", []):
                c["dogma"] = _strkeys(c.get("dogma", {}))

    return {
        "ship": ship_out,
        "candidates": candidates,
        "drones": drones,
        "hull_prices": hull_prices,
        "char_info": char_info,
        "skill_warnings": skill_warnings,
        "goal": goal,
        "region": region_key,
        "region_name": local_label,
        "region_labels": region_labels,
        "category_columns": {k: [list(t) for t in v]
                             for k, v in CATEGORY_COLUMNS.items()},
        "markdown": markdown,
    }


# ── Self-test ─────────────────────────────────────────────────

def self_test():
    """Run verification checks. Exit 0 on success, non-zero on failure."""
    errors = []

    def check(label, condition, detail=""):
        if not condition:
            msg = f"FAIL: {label}"
            if detail:
                msg += f" — {detail}"
            errors.append(msg)
            print(f"  [FAIL] {label} {detail}")
        else:
            print(f"  [ OK ] {label}")

    print("Running self-test...\n")

    # 1. ESI reachable
    status = esi.esi_get(f"{esi.ESI_BASE}/status/?datasource=tranquility")
    check("ESI reachable", status is not None)

    # 2. Skills file parses
    skills_path = os.path.join(os.path.dirname(__file__), "skills.ini")
    if os.path.exists(skills_path):
        char_info, skills = load_skills(skills_path)
        check("Skills file parses", len(skills) > 0, f"{len(skills)} skills loaded")

        # Resolve skill names
        skill_ids = resolve_skill_ids(skills)
        unresolved = [n for n in skills if n not in skill_ids]
        check("Skill names resolve", len(unresolved) == 0,
              f"unresolved: {unresolved}" if unresolved else "")
    else:
        check("Skills file exists", False, f"not found at {skills_path}")

    # 3. Known ship lookup (Retriever)
    ret_ids = esi.search_type_ids(["Retriever"])
    ret_type_id = ret_ids.get("Retriever")
    check("Retriever lookup", ret_type_id is not None, f"type_id={ret_type_id}")

    # 4. Retriever type info
    ret_info = esi.get_type_info(ret_type_id) if ret_type_id else None
    check("Retriever type info",
          ret_info is not None and "Retriever" in ret_info.get("name", ""),
          f"name={ret_info.get('name')}" if ret_info else "fetch failed")

    # 5. Known module lookup (Mining Laser Upgrade I)
    mlu_ids = esi.search_type_ids(["Mining Laser Upgrade I"])
    mlu_id = mlu_ids.get("Mining Laser Upgrade I")
    check("MLU I lookup", mlu_id is not None, f"type_id={mlu_id}")

    # 6. Veldspar market in Verge Vendor
    verge_id = esi.REGIONS["verge"]["id"]
    price, _ = esi.fetch_best_buy(verge_id, 1230, use_cache=True)
    check("Veldspar buy price", price > 0, f"price={price:.2f}")

    # 7. Cache round-trip
    esi.cache_set("self_test_key", {"test": True})
    cached = esi.cache_get("self_test_key", 60)
    check("Cache round-trip", cached == {"test": True})

    # 8. Key dogma attributes resolve
    for name, attr_id in [("hiSlots", 14), ("cpuOutput", 48), ("miningAmount", 77)]:
        attr_info = esi.get_dogma_attribute(attr_id)
        check(f"Dogma attr {name} ({attr_id})", attr_info is not None,
              f"name={attr_info.get('name', '?')}" if attr_info else "fetch failed")

    # 9. Ore hold attribute on Retriever
    if ret_info:
        da = attrs_dict(ret_info.get("dogma_attributes"))
        ore_attr = find_ore_hold_attr(da)
        ore_val = da.get(ore_attr, 0) if ore_attr else 0
        check("Retriever ore hold", ore_val > 10000,
              f"attr={ore_attr}, value={ore_val}")

    # 10. Smoke test — Retriever hull stats with specific skill levels.
    #     Verifies the formula is internally consistent: the same ESI bonus
    #     values produce the same results in parse_ship_hull as when computed
    #     manually from the known base stats and ESI-reported per-level bonuses.
    if ret_type_id and ret_info:
        smoke_skills = {
            "CPU Management": 4,
            "Power Grid Management": 4,
            "Evasive Maneuvering": 3,
            "Spaceship Command": 5,
            "Mining Barge": 1,
        }
        smoke_skill_ids = resolve_skill_ids(smoke_skills)
        smoke_ship = parse_ship_hull(ret_type_id, smoke_skills, smoke_skill_ids)

        # Compute expected values from the same ESI data
        ret_da = attrs_dict(ret_info.get("dogma_attributes"))
        cpu_base = ret_da.get(A["cpuOutput"], 0)
        pg_base = ret_da.get(A["powerOutput"], 0)
        cpu_bonus = _skill_bonus_per_level(*FITTING_SKILL_CPU[1:])
        pg_bonus = _skill_bonus_per_level(*FITTING_SKILL_PG[1:])
        expect_cpu = cpu_base * (1 + cpu_bonus * 4 / 100)
        expect_pg = pg_base * (1 + pg_bonus * 4 / 100)

        check("Smoke: Retriever CPU consistent",
              abs(smoke_ship["cpu_adj"] - expect_cpu) < 0.01,
              f"got {smoke_ship['cpu_adj']:.1f}, expect {expect_cpu:.1f} "
              f"(base {cpu_base:.0f}, +{cpu_bonus:.0f}%/lvl × 4)")

        check("Smoke: Retriever PG consistent",
              abs(smoke_ship["pg_adj"] - expect_pg) < 0.01,
              f"got {smoke_ship['pg_adj']:.1f}, expect {expect_pg:.1f} "
              f"(base {pg_base:.0f}, +{pg_bonus:.0f}%/lvl × 4)")

        # Agility: SC 5 + EM 3, multiplicative PostPercent
        ag_base = ret_da.get(A["agility"], 0)
        mass = ret_da.get(A["mass"], 0)
        ag_adj = ag_base
        for sname, stid, sattr in AGILITY_SKILLS:
            slvl = smoke_skills.get(sname, 0)
            if slvl > 0:
                b = _skill_bonus_per_level(stid, sattr)
                ag_adj *= (1 + b * slvl / 100)
        expect_align = -math.log(0.25) * ag_adj * mass / 1e6

        check("Smoke: Retriever align consistent",
              abs(smoke_ship["align_time_adj"] - expect_align) < 0.01,
              f"got {smoke_ship['align_time_adj']:.1f}s, expect {expect_align:.1f}s "
              f"(agility {ag_base} → {ag_adj:.4f})")

        # Sanity: skill-adjusted values differ from base
        check("Smoke: CPU adj > base", smoke_ship["cpu_adj"] > smoke_ship["cpu_base"])
        check("Smoke: PG adj > base", smoke_ship["pg_adj"] > smoke_ship["pg_base"])
        check("Smoke: align adj < base", smoke_ship["align_time_adj"] < smoke_ship["align_time"])

    # 11. Drawback attribute detection
    # MLU I (22542): cpuPenaltyPercent = 10
    mlu_info = esi.get_type_info(22542)
    if mlu_info:
        mlu_da = attrs_dict(mlu_info.get("dogma_attributes"))
        mlu_cpu_pen = mlu_da.get(A["cpuPenaltyPercent"], 0)
        check("Drawback: MLU I cpuPenaltyPercent",
              abs(mlu_cpu_pen - 10) < 0.1,
              f"attr 1082 = {mlu_cpu_pen}, want 10")

        mlu_dbs = _extract_drawbacks(mlu_da, mlu_info.get("dogma_effects"))
        check("Drawback: MLU I surfaced",
              any("mining laser CPU" in d for d in mlu_dbs),
              f"drawbacks={mlu_dbs}")

    # Drone Mining Augmentor I (32043): drawback = -10 (ship CPU output)
    dma_info = esi.get_type_info(32043)
    if dma_info:
        dma_da = attrs_dict(dma_info.get("dogma_attributes"))
        dma_drawback = dma_da.get(A["drawback"], 0)
        check("Drawback: DMA I drawback attr",
              abs(dma_drawback - (-10)) < 0.1,
              f"attr 1138 = {dma_drawback}, want -10")

        dma_dbs = _extract_drawbacks(dma_da, dma_info.get("dogma_effects"))
        check("Drawback: DMA I surfaced",
              any("CPU output" in d for d in dma_dbs),
              f"drawbacks={dma_dbs}")

    print(f"\n{'All checks passed.' if not errors else f'{len(errors)} check(s) failed.'}")
    return 0 if not errors else 1


# ── CLI ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Generate a ship fitting dossier for AI-assisted fitting decisions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--ship", required=False,
                        help="Ship name (e.g. 'Retriever', 'Porpoise')")
    parser.add_argument("--goal", required=False,
                        help="Freeform goal string (e.g. 'max ISK/hr in highsec belts')")
    parser.add_argument("--skills", default=os.path.join(os.path.dirname(__file__), "skills.ini"),
                        help="Path to skills .ini file (default: ./skills.ini)")
    parser.add_argument("--region", default="verge",
                        choices=list(esi.REGIONS.keys()),
                        help="Region for prices (default: verge)")
    parser.add_argument("--no-jita", action="store_true",
                        help="Skip Jita price lookup, only show local region")
    parser.add_argument("--output", "-o",
                        help="Write dossier to file (default: stdout)")
    parser.add_argument("--refresh", action="store_true",
                        help="Bypass cache, force fresh ESI fetches")
    parser.add_argument("--self-test", action="store_true",
                        help="Run verification checks and exit")

    args = parser.parse_args()

    if args.self_test:
        sys.exit(self_test())

    if not args.ship:
        parser.error("--ship is required (unless using --self-test)")
    if not args.goal:
        parser.error("--goal is required (unless using --self-test)")

    if args.refresh:
        print("Clearing cache...")
        esi.cache_clear()

    use_cache = not args.refresh
    include_jita = not args.no_jita

    # Build region list for price lookups
    local_region_id = esi.REGIONS[args.region]["id"]
    local_label = esi.REGIONS[args.region]["name"]
    regions = [(local_region_id, local_label)]
    if include_jita and args.region != "jita":
        jita_id = esi.REGIONS["jita"]["id"]
        regions.append((jita_id, "Jita (The Forge)"))

    print(f"\n  Fit Dossier — {args.ship}")
    print(f"  Goal: {args.goal}")
    print(f"  Region: {local_label}\n")

    # 1. Load skills
    print("Loading skills...")
    char_info, skills = load_skills(args.skills)
    skill_warnings = validate_skills(char_info, skills)
    for w in skill_warnings:
        print(f"  WARNING: {w}")
    print(f"  {char_info['name']}: {len(skills)} skills loaded\n")

    # 2. Resolve skill names to type IDs
    print("Resolving skill IDs...")
    skill_ids = resolve_skill_ids(skills)
    unresolved = [n for n in skills if n not in skill_ids]
    if unresolved:
        print(f"  WARNING: Could not resolve skills: {unresolved}")
    print(f"  {len(skill_ids)}/{len(skills)} resolved\n")

    # 3. Resolve ship
    print(f"Looking up {args.ship}...")
    ship_type_id = resolve_ship(args.ship)
    if not ship_type_id:
        print(f"ERROR: Ship '{args.ship}' not found in ESI.", file=sys.stderr)
        sys.exit(1)
    print(f"  {args.ship} = type {ship_type_id}\n")

    # 4. Parse ship hull
    print("Fetching ship hull data...")
    ship = parse_ship_hull(ship_type_id, skills, skill_ids)
    print(f"  {ship['name']} ({ship['ship_class']})")
    print(f"  Slots: {ship['hi_slots']}H / {ship['med_slots']}M / "
          f"{ship['low_slots']}L / {ship['rig_slots']}R\n")

    # 5. Hull prices
    print("Fetching hull prices...")
    hull_prices = fetch_hull_prices(ship_type_id, regions, use_cache)
    print()

    # 6. Discover module groups
    print("Discovering module groups...")
    module_groups, drone_groups = discover_module_groups()
    print()

    # 7. Enumerate candidates per slot
    candidates = {}
    for slot_type in ("high", "mid", "low", "rig"):
        slot_label = {"high": "High", "mid": "Mid", "low": "Low", "rig": "Rig"}[slot_type]
        print(f"Enumerating {slot_label} slot candidates...")
        candidates[slot_type] = enumerate_slot_candidates(
            slot_type, module_groups, ship, skills, skill_ids,
            regions, use_cache,
        )
        print()

    # 8. Enumerate drones
    print("Enumerating drones...")
    drones = enumerate_drones(drone_groups, ship, skills, skill_ids, regions, use_cache)
    print()

    # 9. Ore market context (top 10 ores in region)
    ore_context = None
    try:
        from ore_scanner import scan as ore_scan, enrich_results as ore_enrich
        print("Fetching ore market context...")
        region_id = esi.REGIONS[args.region]["id"]
        ore_results = ore_scan(region_id, 22000, show_all=False, ore_class="1")
        ore_results = ore_enrich(ore_results)
        ore_context = ore_results[:10]
        print(f"  Top ore: {ore_context[0]['name']} at {ore_context[0]['isk_m3']:.2f} ISK/m\u00b3"
              if ore_context else "  No ore data")
        print()
    except Exception as e:
        print(f"  WARNING: Could not fetch ore context: {e}")
        print()

    # 10. Format dossier
    print("Formatting dossier...")
    dossier = format_dossier(
        ship, candidates, drones, hull_prices, args.goal,
        args.region, char_info, skills, skill_warnings, regions, include_jita,
        ore_context=ore_context,
    )

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(dossier)
        print(f"\nDossier written to {args.output}")
    else:
        print("\n" + "=" * 72)
        print(dossier)

    print("\nDone.")


if __name__ == "__main__":
    main()
