#!/usr/bin/env python3
"""PI Dossier — Planetary Industry production chain analyser for EVE Online.

Ranks PI product chains by net ISK/hr at local buy orders, accounting for
POCO taxes and haul time. Generates recommended 5-planet layouts using a
greedy allocator.

All PI schematic data fetched from EVE Ref SDE API (ref-data.everef.net).
Market data from CCP ESI. Stdlib only — no third-party dependencies.
"""

import argparse
import configparser
import concurrent.futures
import copy
import datetime
import functools
import itertools
import math
import os
import sys
import time

# Ensure same-directory imports work
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eve_common as esi

# ── Constants ─────────────────────────────────────────────────

# System IDs that routes must not pass through (pirate-infested etc.).
# Populated per-run by generate_pi_dossier_data from ignored inventory
# systems + cfg avoid_systems; every route/jump lookup passes it to ESI.
_AVOID_IDS = set()

EVEREF_BASE = "https://ref-data.everef.net"
CACHE_TTL_PI = 30 * 86400  # 30 days — PI schematics are very stable

# ESI group IDs for PI commodity tiers
PI_GROUPS = {
    "P0": [1032, 1033, 1035],  # Solid / Liquid-Gas / Organic raw resources
    "P1": [1042],               # Basic Commodities
    "P2": [1034],               # Refined Commodities
    "P3": [1040],               # Specialized Commodities
    "P4": [1041],               # Advanced Commodities
}

# Planet type → extractable P0 resource names (stable EVE game design data).
# Verified against EVE University wiki and in-game PI interface.
PLANET_P0_MAP = {
    "Barren":    ["Aqueous Liquids", "Base Metals", "Carbon Compounds",
                  "Microorganisms", "Noble Metals"],
    "Gas":       ["Aqueous Liquids", "Base Metals", "Ionic Solutions",
                  "Noble Gas", "Reactive Gas"],
    "Ice":       ["Aqueous Liquids", "Heavy Metals", "Microorganisms",
                  "Noble Gas", "Planktic Colonies"],
    "Lava":      ["Base Metals", "Felsic Magma", "Heavy Metals",
                  "Non-CS Crystals", "Suspended Plasma"],
    "Oceanic":   ["Aqueous Liquids", "Carbon Compounds", "Complex Organisms",
                  "Microorganisms", "Planktic Colonies"],
    "Plasma":    ["Base Metals", "Heavy Metals", "Noble Metals",
                  "Non-CS Crystals", "Suspended Plasma"],
    "Storm":     ["Aqueous Liquids", "Base Metals", "Ionic Solutions",
                  "Noble Gas", "Suspended Plasma"],
    "Temperate": ["Aqueous Liquids", "Autotrophs", "Carbon Compounds",
                  "Complex Organisms", "Microorganisms"],
}

# Reverse: P0 name → set of planet types that produce it
P0_PLANET_MAP = {}
for _ptype, _p0s in PLANET_P0_MAP.items():
    for _p0 in _p0s:
        P0_PLANET_MAP.setdefault(_p0, set()).add(_ptype)

# POCO tax estimated prices per unit (NPC-set, stable since Rubicon 2013).
# These are NOT market prices — they're the fixed values POCOs use for tax.
# Export tax = quantity × estimated_price × tax_rate
# Import tax = quantity × estimated_price × 0.5 × tax_rate
PI_TAX_BASE = {
    "P0": 5,
    "P1": 500,
    "P2": 9000,
    "P3": 70000,
    "P4": 1350000,
}

# PI facility power/CPU costs. Verified 2026-07-26 against ESI dogma
# (category 41 Planetary Industry): attr 15 powerLoad, attr 49 cpuLoad,
# attr 1691 ecuExtractorHeadPower, attr 1690 ecuExtractorHeadCPU.
FACILITY_COSTS = {
    "ecu_base":     {"pg": 2600, "cpu": 400},
    "ecu_per_head": {"pg": 550,  "cpu": 110},
    "bif":          {"pg": 800,  "cpu": 200},
    "aif":          {"pg": 700,  "cpu": 500},
    "launchpad":    {"pg": 700,  "cpu": 3600},
    "storage":      {"pg": 700,  "cpu": 500},
    "htif":         {"pg": 400,  "cpu": 1100},  # High-Tech Industry Facility (P4)
}

# On-planet holding capacity (m3). Goods sitting in the customs office can't
# feed a factory — they have to be imported onto the planet first, so a planet
# left running unattended must physically hold its own input buffer.
FACILITY_M3 = {"launchpad": 10000, "storage": 12000}

# P4 needs a High-Tech Production Plant, and that facility only exists for two
# planet types. Verified 2026-07-26 against ESI group 1028 (Processors): 18
# types = Basic + Advanced Industry Facility for all 8 planet types, plus
# exactly "Barren High-Tech Production Plant" and "Temperate High-Tech
# Production Plant". Basic/Advanced are unrestricted, so P1-P3 sites anywhere.
HTIF_PLANET_TYPES = {"Barren", "Temperate"}

DEFAULT_ECU_HEADS = 10
DEFAULT_EXTRACTION_RATE = 8000  # P0/hr per 10-head ECU (conservative)

# Density-to-yield estimation table (Phase 3).
# Per-head P0/hr estimates indexed by density band and head-count band.
# Calibrated against Cozzynk's 4 observed data points at Planetology II.
# Calibration data:
#   23% density, 3 heads → 2036/head/hr
#   13% density, 4 heads → 1798/head/hr
#   16% density, 4 heads → 1491/head/hr
#    5% density, 7 heads → 770/head/hr
DENSITY_YIELD_PER_HEAD = {
    # (min_pct, max_pct): per_head_hr at 10 heads (default estimation target)
    (0, 3):    300,    # very low (1-3%) — barely extractable, hard to find viable hotspot
    (3, 6):    750,    # low
    (6, 10):   1000,   # low-medium
    (10, 15):  1350,   # medium (calibrated: 13% → 1798/4heads, but 10-head yields less/head)
    (15, 20):  1500,   # medium-high (calibrated: 16% → 1491/4heads)
    (20, 26):  1700,   # high (calibrated: 23% → 2036/3heads, 10-head estimate ~1700)
    (26, 35):  1900,   # very high
    (35, 101): 2200,   # exceptional
}

# Command Center Upgrades level → (powergrid, cpu) budget.
# Verified 2026-07-26 against ESI: every type in group 1027 carries attr 277
# (required CCU level) with attr 11 / attr 48 as its PG / CPU output. The
# table used to be shifted one level down, understating every budget.
CCU_BUDGETS = {
    0: (6000, 1675),
    1: (9000, 7057),
    2: (12000, 12136),
    3: (15000, 17215),
    4: (17000, 21315),
    5: (19000, 25415),
}


# ── Config loading ────────────────────────────────────────────

def _ini_path(filename):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)


def load_skills():
    """Load character info and PI skills from skills.ini."""
    cp = configparser.ConfigParser()
    cp.optionxform = str  # preserve case
    cp.read(_ini_path("skills.ini"), encoding="utf-8")
    char_info = {
        "name": cp.get("character", "name", fallback="Unknown"),
        "last_updated": cp.get("character", "last_updated", fallback="?"),
    }
    skills = {}
    if cp.has_section("skills"):
        for k, v in cp.items("skills"):
            try:
                skills[k] = int(v)
            except ValueError:
                pass
    pi_skills = {
        "ccu": skills.get("Command Center Upgrades", 0),
        "ic": skills.get("Interplanetary Consolidation", 0),
        "planetology": skills.get("Planetology", 0),
        "adv_planetology": skills.get("Advanced Planetology", 0),
        "remote_sensing": skills.get("Remote Sensing", 0),
        "accounting": skills.get("Accounting", 0),
        "broker_relations": skills.get("Broker Relations", 0),
    }
    return char_info, pi_skills


SALES_TAX_BASE = 0.045          # market transaction tax before skills
SALES_TAX_REDUCTION = 0.11      # Accounting: -11% per level
BROKER_FEE_BASE = 0.03          # NPC-station order fee before skills
BROKER_FEE_REDUCTION = 0.003    # Broker Relations: -0.3%/level


def load_pi_config():
    """Load pi_config.ini.

    pg/cpu budgets and Jita sales tax default from the trained skills in
    skills.ini (CCU level, Accounting level) when the ini doesn't set them.
    """
    cp = configparser.ConfigParser()
    cp.optionxform = str
    cp.read(_ini_path("pi_config.ini"), encoding="utf-8")

    _, pi_skills = load_skills()
    ccu_pg, ccu_cpu = CCU_BUDGETS.get(pi_skills["ccu"], CCU_BUDGETS[5])
    skill_sales_tax = SALES_TAX_BASE * (
        1 - SALES_TAX_REDUCTION * pi_skills["accounting"])
    skill_broker_fee = (BROKER_FEE_BASE
                        - BROKER_FEE_REDUCTION * pi_skills["broker_relations"])

    cfg = {
        "home_system": cp.get("pi", "home_system", fallback="Jufvitte"),
        "max_market_jumps": cp.getint("pi", "max_market_jumps", fallback=5),
        "max_haul_minutes": cp.getfloat("pi", "max_haul_minutes_per_day", fallback=60),
        "tax_rate": cp.getfloat("pi", "default_tax_rate", fallback=0.15),
        "hauler_m3": cp.getfloat("pi", "hauler_capacity_m3", fallback=9000),
        # Haul cadence: one collection/delivery run every N days
        "haul_every_days": cp.getfloat("pi", "haul_every_days", fallback=1),
        # Customs office capacity — bounds what a planet can stockpile
        # between hauls (35,000 m3, fixed game value)
        "poco_buffer_m3": cp.getfloat("pi", "poco_buffer_m3", fallback=35000),
        # Days a planet must run on its own on-planet stock between visits.
        # 1 = you log in daily to move POCO<->planet even though the hauler
        # only comes every haul_every_days. Set = haul_every_days if you only
        # touch PI on haul day (needs far more Storage Facilities).
        "on_planet_buffer_days": cp.getfloat("pi", "on_planet_buffer_days",
                                             fallback=1.0),
        "pg_budget": cp.getfloat("pi", "power_per_planet", fallback=ccu_pg),
        "cpu_budget": cp.getfloat("pi", "cpu_per_planet", fallback=ccu_cpu),
        # Market transaction tax on every sell (buy orders and sell orders)
        "sales_tax": cp.getfloat("pi", "sales_tax", fallback=skill_sales_tax),
        # Order-placement fee — only paid when sourcing via own buy orders
        "broker_fee": cp.getfloat("pi", "broker_fee", fallback=skill_broker_fee),
        "max_planets": cp.getint("pi", "max_planets", fallback=5),
        # When true, value every final product at the Jita top buy order
        # (instant dump) instead of the thin in-range local buy book. Matches
        # a set-and-forget "haul to Jita and sell to buy orders" playstyle.
        "sell_at_jita": cp.getboolean("pi", "sell_at_jita", fallback=False),
        "avoid_systems": [s.strip() for s in
                          cp.get("pi", "avoid_systems", fallback="").split(",")
                          if s.strip()],
    }

    cfg["haul"] = {
        "sec_per_jump": cp.getfloat("haul_model", "seconds_per_jump", fallback=45),
        "sec_per_planet": cp.getfloat("haul_model", "seconds_per_planet_stop", fallback=180),
        "sec_per_station": cp.getfloat("haul_model", "seconds_per_station_stop", fallback=180),
        "daily_overhead": cp.getfloat("haul_model", "daily_overhead_seconds", fallback=300),
    }

    cfg["tax_overrides"] = {}
    if cp.has_section("tax_overrides"):
        for k, v in cp.items("tax_overrides"):
            try:
                cfg["tax_overrides"][k] = float(v)
            except ValueError:
                pass

    return cfg


def load_planet_inventory():
    """Load planet_inventory.ini → {system: {planet_type: count}}.

    A system section may carry `_ignored = 1` — its data is kept (and
    round-trips through the web editor) but active_inventory() excludes
    it from all calculations.
    """
    cp = configparser.ConfigParser()
    cp.optionxform = str
    cp.read(_ini_path("planet_inventory.ini"), encoding="utf-8")
    inv = {}
    for section in cp.sections():
        inv[section] = {}
        for ptype, count in cp.items(section):
            if ptype == "_ignored":
                if count.strip().lower() in ("1", "true", "yes"):
                    inv[section]["_ignored"] = True
                continue
            try:
                inv[section][ptype] = int(count)
            except ValueError:
                pass
    return inv


def active_inventory(planet_inv):
    """Inventory usable for calculations: drops ignored systems and meta keys."""
    out = {}
    for system, planets in planet_inv.items():
        if planets.get("_ignored"):
            continue
        out[system] = {pt: c for pt, c in planets.items()
                       if not pt.startswith("_")}
    return out


def _underscore_to_name(s):
    """Convert config key 'Aqueous_Liquids' to EVE Ref name 'Aqueous Liquids'.
    Special case: 'Non_CS_Crystals' → 'Non-CS Crystals'."""
    if s.startswith("Non_CS"):
        return "Non-CS Crystals"
    return s.replace("_", " ")


def _name_to_underscore(s):
    """Convert EVE Ref name 'Aqueous Liquids' to config key 'Aqueous_Liquids'."""
    return s.replace(" ", "_").replace("-", "_")


def _parse_section_key(section):
    """Parse config section into (system, ptype, instance).

    Supports:
      [Jufvitte.Gas.A]  → ('Jufvitte', 'Gas', 'A')
      [Jufvitte.Gas]    → ('Jufvitte', 'Gas', 'A')  (legacy, treat as instance A)
    """
    parts = section.split(".")
    if len(parts) >= 3:
        return parts[0], parts[1], parts[2]
    elif len(parts) == 2:
        return parts[0], parts[1], "A"
    return None, None, None


def _section_key_str(system, ptype, instance):
    """Build config section string."""
    return f"{system}.{ptype}.{instance}"


def load_extraction_rates():
    """Load planet_extraction.ini (per-resource per-instance format).

    Sections: [System.PlanetType.Instance] (e.g. [Jufvitte.Gas.A])
    Legacy [System.PlanetType] sections treated as instance A.
    Returns: {section_key_str: {p0_name: rate}}
    """
    cp = configparser.ConfigParser()
    cp.optionxform = str
    cp.read(_ini_path("planet_extraction.ini"), encoding="utf-8")
    rates = {}
    for section in cp.sections():
        if "." not in section:
            continue
        system, ptype, instance = _parse_section_key(section)
        if not system:
            continue
        key = _section_key_str(system, ptype, instance)
        rates[key] = {}
        for resource, rate in cp.items(section):
            try:
                p0_name = _underscore_to_name(resource)
                rates[key][p0_name] = float(rate)
            except ValueError:
                pass
    return rates


def load_planet_density():
    """Load planet_density.ini → per-resource density % per planet instance.

    Returns: {section_key_str: {p0_name: density_pct}}
    """
    path = _ini_path("planet_density.ini")
    if not os.path.exists(path):
        return {}
    cp = configparser.ConfigParser()
    cp.optionxform = str
    cp.read(path, encoding="utf-8")
    densities = {}
    for section in cp.sections():
        if "." not in section:
            continue
        system, ptype, instance = _parse_section_key(section)
        if not system:
            continue
        key = _section_key_str(system, ptype, instance)
        densities[key] = {}
        for resource, pct in cp.items(section):
            try:
                p0_name = _underscore_to_name(resource)
                densities[key][p0_name] = float(pct)
            except ValueError:
                pass
    return densities


def load_planet_taxes():
    """Load planet_taxes.ini → per-planet tax rates.

    Returns: {section_key_str: tax_rate_float}
    e.g. {"Jufvitte.Gas.A": 0.15, "Jufvitte.Barren.B": 0.10}
    """
    path = _ini_path("planet_taxes.ini")
    if not os.path.exists(path):
        return {}
    cp = configparser.ConfigParser()
    cp.optionxform = str
    cp.read(path, encoding="utf-8")
    taxes = {}
    if cp.has_section("taxes"):
        for key, val in cp.items("taxes"):
            try:
                taxes[key] = float(val)
            except ValueError:
                pass
    return taxes


def _estimate_from_density(density_pct, heads=DEFAULT_ECU_HEADS):
    """Estimate P0/hr from density % using the static band table.
    Returns 0 for 0% density — resource cannot be extracted.
    """
    if density_pct <= 0:
        return 0
    for (lo, hi), per_head in DENSITY_YIELD_PER_HEAD.items():
        if lo < density_pct <= hi:  # exclusive lower bound: 0% yields nothing
            return per_head * heads
    return 2200 * heads


# Auto-calibration of the density→yield model from observed rates.
CALIBRATION_MIN_POINTS_FIT = 4    # power-law fit needs at least this many points
CALIBRATION_FULL_WEIGHT_POINTS = 8  # fit reaches full weight here
CALIBRATION_SCALE_CLAMP = (0.3, 3.0)  # sanity bounds on the table scale factor


def build_density_estimator(extraction_rates, density_data,
                            heads=DEFAULT_ECU_HEADS):
    """Self-calibrating P0/hr estimator from density %.

    Every observed rate whose planet also has a density scan is a
    calibration point (density_pct, observed_rate). With 1-3 points the
    static band table is scaled by the median observed/predicted ratio —
    even a couple of observations firm up every estimate. From
    CALIBRATION_MIN_POINTS_FIT points spanning a 2x density spread, a
    log-log power fit (rate = a * density^b) blends in, reaching full
    weight at CALIBRATION_FULL_WEIGHT_POINTS. OBS entries always override
    estimates on their own planet regardless.

    Returns (estimate_fn, info): estimate_fn(density_pct) -> P0/hr at
    `heads` extractor heads; info = {points, scale, fit} for display.
    """
    points = []
    for key, obs_map in extraction_rates.items():
        dens_map = density_data.get(key, {})
        for p0, rate in obs_map.items():
            d = dens_map.get(p0)
            if d and d > 0 and rate > 0:
                points.append((d, rate))

    info = {"points": len(points), "scale": 1.0, "fit": None}
    if not points:
        return (lambda d: _estimate_from_density(d, heads)), info

    ratios = sorted(rate / max(1.0, _estimate_from_density(d, heads))
                    for d, rate in points)
    n = len(ratios)
    scale = (ratios[n // 2] if n % 2
             else (ratios[n // 2 - 1] + ratios[n // 2]) / 2)
    scale = min(max(scale, CALIBRATION_SCALE_CLAMP[0]),
                CALIBRATION_SCALE_CLAMP[1])
    info["scale"] = scale

    a = b = None
    fit_weight = 0.0
    densities = [d for d, _ in points]
    if (len(points) >= CALIBRATION_MIN_POINTS_FIT
            and max(densities) / min(densities) >= 2):
        lx = [math.log(d) for d, _ in points]
        ly = [math.log(rate) for _, rate in points]
        mean_x = sum(lx) / len(lx)
        mean_y = sum(ly) / len(ly)
        sxx = sum((x - mean_x) ** 2 for x in lx)
        if sxx > 0:
            b = sum((x - mean_x) * (y - mean_y) for x, y in zip(lx, ly)) / sxx
            b = min(max(b, 0.2), 1.5)  # yield grows monotonically, sub-quadratic
            a = math.exp(mean_y - b * mean_x)
            fit_weight = min(1.0,
                             (len(points) - (CALIBRATION_MIN_POINTS_FIT - 1))
                             / (CALIBRATION_FULL_WEIGHT_POINTS
                                - (CALIBRATION_MIN_POINTS_FIT - 1)))
            info["fit"] = {"a": a, "b": b, "weight": fit_weight}

    def estimate(density_pct):
        if density_pct <= 0:
            return 0
        scaled = _estimate_from_density(density_pct, heads) * scale
        if a is not None and fit_weight > 0:
            return fit_weight * (a * density_pct ** b) + (1 - fit_weight) * scaled
        return scaled

    return estimate, info


def _build_instance_rates(inv, extraction_rates, density_data,
                          estimator=None):
    """Per-instance P0 rate table for every planet in the active inventory.

    Returns {(system, ptype, instance): {p0_name: (rate, tag)}}.
    Priority per instance: observed (OBS) > density estimate (EST).
    A density-scanned planet that lacks a resource gets no entry for it
    (the resource cannot be extracted there). A system+ptype with no scan
    data at all gets a synthetic 'A' instance at the conservative default
    rate (DFL) — same semantics the old get_p0_rate() had.

    estimator: optional density->rate function (e.g. from
    build_density_estimator); defaults to the static band table.
    """
    if estimator is None:
        estimator = _estimate_from_density
    table = {}
    for system, planets in inv.items():
        for ptype, count in planets.items():
            if count <= 0 or ptype not in PLANET_P0_MAP:
                continue
            prefix = f"{system}.{ptype}."
            keys = [k for k in extraction_rates if k.startswith(prefix)]
            keys += [k for k in density_data
                     if k.startswith(prefix) and k not in keys]
            if not keys:
                table[(system, ptype, "A")] = {
                    p0: (DEFAULT_EXTRACTION_RATE, "DFL")
                    for p0 in PLANET_P0_MAP[ptype]}
                continue
            density_scanned = any(density_data.get(k) for k in keys)
            covered = set()
            for key in keys:
                inst = key.split(".")[-1]
                obs_map = extraction_rates.get(key, {})
                dens_map = density_data.get(key, {})
                rates = {}
                for p0 in PLANET_P0_MAP[ptype]:
                    obs = obs_map.get(p0)
                    if obs is not None and obs > 0:
                        rates[p0] = (obs, "OBS")
                        covered.add(p0)
                        continue
                    if dens_map:
                        d = dens_map.get(p0)
                        if d is not None and d > 0:
                            rates[p0] = (estimator(d), "EST")
                            covered.add(p0)
                table[(system, ptype, inst)] = rates
            if not density_scanned:
                # Only observed data, no density scan: resources without an
                # observation are unknown, not absent — default them.
                first = table[(system, ptype, keys[0].split(".")[-1])]
                for p0 in PLANET_P0_MAP[ptype]:
                    if p0 not in covered:
                        first.setdefault(p0, (DEFAULT_EXTRACTION_RATE, "DFL"))
    return table


def _build_p0_candidates(instance_rates):
    """Ranked extraction candidates per P0 resource.

    Returns ({p0: [(rate, tag, system, ptype, instance), ...] desc},
             {p0: {system: [same tuples, desc]}}).
    """
    cands = {}
    for (system, ptype, inst), rates in instance_rates.items():
        for p0, (rate, tag) in rates.items():
            if rate > 0:
                cands.setdefault(p0, []).append((rate, tag, system, ptype, inst))
    by_system = {}
    for p0, lst in cands.items():
        lst.sort(key=lambda c: (-c[0], c[2], c[4]))
        sysmap = {}
        for c in lst:
            sysmap.setdefault(c[2], []).append(c)
        by_system[p0] = sysmap
    return cands, by_system


# ── EVE Ref data fetching ─────────────────────────────────────

def everef_type(type_id):
    """Fetch type info from EVE Ref (cached 30 days)."""
    url = f"{EVEREF_BASE}/types/{type_id}"
    return esi.esi_get_cached(url, CACHE_TTL_PI)


def everef_schematic(schematic_id):
    """Fetch schematic from EVE Ref (cached 30 days)."""
    url = f"{EVEREF_BASE}/schematics/{schematic_id}"
    return esi.esi_get_cached(url, CACHE_TTL_PI)


def fetch_pi_types(progress=False):
    """Discover all PI type IDs and fetch their info from EVE Ref.

    Returns:
        types: {type_id: {name, tier, volume, base_price, produced_by, used_by}}
        by_name: {name: type_id}
    """
    types = {}
    by_name = {}

    for tier, group_ids in PI_GROUPS.items():
        type_ids = []
        for gid in group_ids:
            g = esi.get_group_info(gid)
            if g and "types" in g:
                type_ids.extend(g["types"])

        if progress:
            print(f"  {tier}: {len(type_ids)} types discovered")

        # Fetch type info in parallel
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(everef_type, tid): tid for tid in type_ids}
            for fut in concurrent.futures.as_completed(futures):
                tid = futures[fut]
                data = fut.result()
                if not data:
                    continue
                name = data.get("name", {})
                if isinstance(name, dict):
                    name = name.get("en", f"Type {tid}")
                types[tid] = {
                    "type_id": tid,
                    "name": name,
                    "tier": tier,
                    "volume": data.get("volume", 0),
                    "base_price": data.get("base_price", 0),
                    "produced_by": data.get("produced_by_schematic_ids", []),
                    "used_by": data.get("used_by_schematic_ids", []),
                }
                by_name[name] = tid

    return types, by_name


def fetch_schematics(pi_types, progress=False):
    """Fetch all PI schematics referenced by known types.

    Returns:
        schematics: {schematic_id: {name, cycle_time, inputs, output}}
    """
    # Collect all schematic IDs from produced_by and used_by
    schematic_ids = set()
    for t in pi_types.values():
        schematic_ids.update(t.get("produced_by", []))
        schematic_ids.update(t.get("used_by", []))

    if progress:
        print(f"  Fetching {len(schematic_ids)} schematics...")

    schematics = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(everef_schematic, sid): sid
                   for sid in schematic_ids}
        for fut in concurrent.futures.as_completed(futures):
            sid = futures[fut]
            data = fut.result()
            if not data:
                continue
            name = data.get("name", {})
            if isinstance(name, dict):
                name = name.get("en", f"Schematic {sid}")

            # Parse materials (inputs) and products (outputs)
            inputs = []
            for m in data.get("materials", {}).values():
                inputs.append({
                    "type_id": m["type_id"],
                    "quantity": m["quantity"],
                })

            output = None
            for p in data.get("products", {}).values():
                output = {
                    "type_id": p["type_id"],
                    "quantity": p["quantity"],
                }
                break  # PI schematics have exactly one output

            if output:
                schematics[sid] = {
                    "schematic_id": sid,
                    "name": name,
                    "cycle_time": data.get("cycle_time", 3600),
                    "inputs": inputs,
                    "output": output,
                }

    return schematics


def build_chain_graph(pi_types, schematics):
    """Build production chain graph.

    Returns:
        chains: {output_type_id: {
            tier, output, schematic, p0_inputs,
            p1_inputs (for P2+), p2_inputs (for P3),
            all_p0_names, planet_types_needed
        }}
    """
    chains = {}

    for tid, tinfo in pi_types.items():
        if tinfo["tier"] == "P0":
            continue  # P0s are raw resources, not producible

        # Find the production schematic
        for sid in tinfo.get("produced_by", []):
            sch = schematics.get(sid)
            if not sch:
                continue

            chain = {
                "output_type_id": tid,
                "output_name": tinfo["name"],
                "tier": tinfo["tier"],
                "volume": tinfo["volume"],
                "base_price": tinfo["base_price"],
                "schematic": sch,
                "inputs": [],
                "p0_inputs": [],  # Ultimate P0 raw materials needed
            }

            # Resolve inputs
            for inp in sch["inputs"]:
                inp_type = pi_types.get(inp["type_id"])
                if inp_type:
                    chain["inputs"].append({
                        "type_id": inp["type_id"],
                        "name": inp_type["name"],
                        "tier": inp_type["tier"],
                        "quantity": inp["quantity"],
                    })

            # Trace P0 dependencies
            _trace_p0_inputs(chain, pi_types, schematics)

            # Determine which planet types can produce required P0s
            p0_names = set()
            for p0 in chain["p0_inputs"]:
                p0_names.add(p0["name"])
            chain["all_p0_names"] = p0_names

            # For each P0, find compatible planet types
            chain["p0_planet_types"] = {}
            for p0_name in p0_names:
                chain["p0_planet_types"][p0_name] = P0_PLANET_MAP.get(p0_name, set())

            chains[tid] = chain
            break  # Use first production schematic

    return chains


def _trace_p0_inputs(chain, pi_types, schematics):
    """Recursively trace a chain's inputs down to P0 raw materials."""
    p0_inputs = []
    _visited = set()

    def _trace(inputs, multiplier=1.0):
        for inp in inputs:
            tid = inp["type_id"]
            if tid in _visited:
                continue
            _visited.add(tid)

            t = pi_types.get(tid)
            if not t:
                continue

            if t["tier"] == "P0":
                p0_inputs.append({
                    "type_id": tid,
                    "name": t["name"],
                })
            else:
                # Find this type's production schematic and trace deeper
                for sid in t.get("produced_by", []):
                    sch = schematics.get(sid)
                    if sch:
                        _trace(sch["inputs"], multiplier)
                        break

    _trace(chain["schematic"]["inputs"])
    chain["p0_inputs"] = p0_inputs


# ── Market data ───────────────────────────────────────────────

CACHE_TTL_HISTORY = 86400  # 1 day — ESI history updates daily


def _fetch_market_history(region_id, type_id):
    """Fetch market history from ESI (cached 1 day). Returns list of daily entries."""
    url = (f"{esi.ESI_BASE}/markets/{region_id}/history/"
           f"?datasource=tranquility&type_id={type_id}")
    return esi.esi_get_cached(url, CACHE_TTL_HISTORY) or []


def _compute_history_stats(history, days=30):
    """Compute VWAP, volume, and trade activity over the last N calendar days.

    Filters by actual date strings so the window is always a fixed calendar
    period. Previous version used history[-N:] which for rarely-traded products
    could span months (inflating active_days to 30/30).

    Returns: {vwap, avg_daily_volume, total_volume, active_days, days_sampled,
              total_order_count}
    """
    cutoff = (datetime.datetime.utcnow()
              - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    recent = [d for d in history if d.get("date", "") >= cutoff]

    empty = {"vwap": 0, "avg_daily_volume": 0, "total_volume": 0,
             "active_days": 0, "days_sampled": days,
             "total_order_count": 0}
    if not recent:
        return empty

    total_value = 0
    total_volume = 0
    active_days = 0
    total_orders = 0
    for d in recent:
        vol = d.get("volume", 0)
        avg = d.get("average", 0)
        oc = d.get("order_count", 0)
        total_orders += oc
        if vol > 0:
            total_value += avg * vol
            total_volume += vol
            active_days += 1

    vwap = total_value / total_volume if total_volume > 0 else 0

    return {
        "vwap": vwap,
        "avg_daily_volume": total_volume / days,  # per calendar day, not per entry
        "total_volume": total_volume,
        "active_days": active_days,
        "days_sampled": days,
        "total_order_count": total_orders,
    }


SUSTAINED_PRICE_WINDOW_DAYS = 30  # blend price over this many days of production
SHALLOW_BUY_THRESHOLD_DAYS = 7   # flag if real buy depth < this many days
MIN_REAL_ORDER_PRICE = 2         # orders at <= this price are treated as stubs
LOW_ACTIVITY_ORDER_THRESHOLD = 10  # flag if fewer trades than this in 30d
ORDERS_FOR_FULL_ACTIVITY = 20     # this many orders/30d = activity_factor 1.0

# Jita instant-dump modelling. A haul arrives with one window's production and
# sells it in one go, so the price that matters is the average fill across the
# book — not the top order. Region-wide orders are filtered to the Jita system:
# an instant dump means orders that actually reach Jita 4-4.
JITA_SYSTEM_ID = 30000142
SLIPPAGE_WARN = 0.03        # flag when book-walk fill is this far below top
BOOK_SHARE_WARN = 0.25      # flag when one haul is this share of 30d throughput


def _fetch_buy_orders_in_range(region_id, type_id, home_system_id, max_jumps):
    """Fetch buy orders filtered to stations within max_jumps of home system.

    Returns: {
        "best_price": float, "best_order": dict, "real_depth": int,
        "book": [(price, volume, system_name, jumps), ...] sorted desc by price,
        "best_any_price": float
    }
    """
    url = (f"{esi.ESI_BASE}/markets/{region_id}/orders/"
           f"?datasource=tranquility&order_type=buy&type_id={type_id}")
    orders = esi.esi_get_cached(url, esi.CACHE_TTL_MARKET) or []
    buy_orders = [o for o in orders if o.get("is_buy_order", True)]

    empty = {"best_price": 0, "best_order": None, "real_depth": 0,
             "book": [], "best_any_price": 0}

    if not buy_orders:
        return empty

    best_any_price = max(o["price"] for o in buy_orders)

    # Filter to orders within jump range
    in_range = []
    for o in buy_orders:
        sys_id = o.get("system_id", 0)
        if sys_id == home_system_id:
            jumps = 0
        else:
            jumps = esi.get_jump_count(home_system_id, sys_id,
                                       avoid=_AVOID_IDS)
            if jumps < 0 or jumps > max_jumps:
                continue
        in_range.append((o["price"], o.get("volume_remain", 0),
                         esi.resolve_system_name(sys_id), jumps, o))

    if not in_range:
        return {**empty, "best_any_price": best_any_price}

    in_range.sort(key=lambda x: -x[0])
    best_order = in_range[0][4]
    best_price = in_range[0][0]
    real_depth = sum(vol for price, vol, _, _, _ in in_range
                     if price > MIN_REAL_ORDER_PRICE)
    book = [(price, vol, sys_name, jumps)
            for price, vol, sys_name, jumps, _ in in_range]

    return {
        "best_price": best_price,
        "best_order": best_order,
        "real_depth": real_depth,
        "book": book,
        "best_any_price": best_any_price,
    }


def _compute_sustained_price(book, units_per_day, vwap, days=SUSTAINED_PRICE_WINDOW_DAYS):
    """Walk the buy order book and blend with VWAP over N days of production.

    For each order above the stub threshold, sell into it until exhausted.
    Remaining production (no buy orders left) assumes user posts sell orders
    at VWAP — the typical regional trade price.

    Returns: (sustained_price_per_unit, real_buy_days)
    """
    total_to_sell = units_per_day * days
    if total_to_sell <= 0:
        return vwap, 0

    sold = 0
    revenue = 0.0
    real_buy_units = 0

    for price, volume, _, _ in book:
        if price <= MIN_REAL_ORDER_PRICE:
            continue  # skip 1-ISK stubs
        can_sell = min(volume, total_to_sell - sold)
        revenue += can_sell * price
        sold += can_sell
        real_buy_units += can_sell
        if sold >= total_to_sell:
            break

    # Remaining production sold at VWAP (user posts sell orders)
    remaining = total_to_sell - sold
    if remaining > 0:
        fill_price = vwap if vwap > 0 else 0
        revenue += remaining * fill_price
        sold += remaining

    sustained = revenue / sold if sold > 0 else 0
    real_buy_days = real_buy_units / units_per_day if units_per_day > 0 else 0
    return sustained, real_buy_days


def _fetch_jita_book(type_id, buy):
    """Jita-system order book as [(price, volume), ...], best price first.

    buy=True gives the buy orders you dump output into; buy=False the sell
    orders you lift to source factory inputs. Same cached URL the top-of-book
    lookup used, so depth costs no extra request.
    """
    side = "buy" if buy else "sell"
    url = (f"{esi.ESI_BASE}/markets/{esi.REGIONS['jita']['id']}/orders/"
           f"?datasource=tranquility&order_type={side}&type_id={type_id}")
    orders = esi.esi_get_cached(url, esi.CACHE_TTL_MARKET) or []
    book = [(o["price"], o.get("volume_remain", 0)) for o in orders
            if bool(o.get("is_buy_order", False)) == buy
            and o.get("system_id") == JITA_SYSTEM_ID
            and o.get("volume_remain", 0) > 0
            and o.get("price", 0) > MIN_REAL_ORDER_PRICE]
    book.sort(key=lambda pv: -pv[0] if buy else pv[0])
    return book


def _walk_book(book, qty):
    """Fill qty units against a price-sorted book.

    Returns (avg_fill_price, filled_qty). filled_qty < qty means the book ran
    out — the caller decides what the remainder is worth.
    """
    if not book or qty <= 0:
        return 0.0, 0.0
    filled = 0.0
    value = 0.0
    for price, vol in book:
        take = min(vol, qty - filled)
        value += take * price
        filled += take
        if filled >= qty:
            break
    return (value / filled if filled > 0 else 0.0), filled


def _jita_fill(prices, qty, selling, window_days=1.0):
    """Realised per-unit price for pushing `qty` units through Jita in one go.

    selling=True  -> dumping output into buy orders (walks down the book)
    selling=False -> sourcing inputs from sell orders (walks up the book)

    Whatever the live book can't absorb is priced at the 30-day Jita VWAP —
    that quantity isn't an instant trade, you'd have to work the order in.
    `window_days` is only used to express the haul as a share of the market's
    normal throughput, which is the real "is this sustainable" signal: a book
    refills between hauls, so a deep 30d history matters more than one snapshot.

    Returns {price, top, filled_frac, slippage, book_share, depth_units, vwap}.
    """
    book = prices.get("jita_buy_book" if selling else "jita_sell_book", [])
    vwap = prices.get("jita_vwap", 0) or 0
    top = book[0][0] if book else 0.0
    depth = sum(v for _, v in book)
    if qty <= 0:
        return {"price": top or vwap, "top": top, "filled_frac": 1.0,
                "slippage": 0.0, "book_share": 0.0, "depth_units": depth,
                "vwap": vwap}

    avg, filled = _walk_book(book, qty)
    remainder = max(0.0, qty - filled)
    if filled > 0:
        price = (avg * filled + vwap * remainder) / qty
    else:
        price = vwap
    # Positive slippage always means "worse than top of book" for both sides.
    slippage = ((top - price) / top if selling else (price - top) / top) \
        if top > 0 else 0.0
    daily_vol = prices.get("jita_avg_daily_vol", 0) or 0
    normal_throughput = daily_vol * max(window_days, 1e-9)
    return {
        "price": price,
        "top": top,
        "filled_frac": min(1.0, filled / qty),
        "slippage": slippage,
        "book_share": (qty / normal_throughput) if normal_throughput > 0 else 0.0,
        "depth_units": depth,
        "vwap": vwap,
    }


def _depth_flags(fill, label):
    """Human-readable warnings for a Jita book walk (empty list = clean)."""
    out = []
    if fill["top"] <= 0:
        out.append(f"NO JITA BOOK ({label})")
        return out
    if fill["filled_frac"] < 1.0:
        out.append(f"{label} BOOK ONLY FILLS {fill['filled_frac'] * 100:.0f}%")
    if fill["slippage"] > SLIPPAGE_WARN:
        out.append(f"{label} SLIPPAGE {fill['slippage'] * 100:.0f}%")
    if fill["book_share"] > BOOK_SHARE_WARN:
        out.append(f"{label} IS {fill['book_share'] * 100:.0f}% OF 30d VOLUME")
    return out


def fetch_pi_market(pi_types, local_region_id, home_system_id, max_jumps,
                    progress=False):
    """Fetch market data for all PI products.

    Uses two pricing signals:
    - **Best in-range buy order** (within max_jumps of home) — the actual
      price you'd sell at. Primary signal for ISK/hr calculations.
    - **30-day regional VWAP** (from ESI market history) — what the item
      typically trades at across the region. Reference / sanity check.

    Returns: {type_id: {local_buy, local_depth, local_vwap, ...}}
    """
    jita_region_id = esi.REGIONS["jita"]["id"]
    type_ids = [tid for tid, t in pi_types.items() if t["tier"] != "P0"]

    if progress:
        print(f"  Fetching market data for {len(type_ids)} PI products "
              f"(buy orders within {max_jumps}j of home)...")

    prices = {}

    def _fetch_one(tid):
        # Local region: in-range buy orders + history
        local_data = _fetch_buy_orders_in_range(
            local_region_id, tid, home_system_id, max_jumps)

        local_hist = _fetch_market_history(local_region_id, tid)
        local_stats = _compute_history_stats(local_hist, days=30)

        # Buyer location for display (from top order)
        local_buyer_system = ""
        local_buyer_jumps = 0
        if local_data["best_order"]:
            sys_id = local_data["best_order"].get("system_id", 0)
            local_buyer_system = esi.resolve_system_name(sys_id)
            local_buyer_jumps = esi.get_jump_count(home_system_id, sys_id,
                                                   avoid=_AVOID_IDS)
            if local_buyer_jumps < 0:
                local_buyer_jumps = 0

        # Jita: history + live buy + live sell (no jump filtering — it's a
        # destination). Sell price is what you'd PAY to buy this commodity as
        # a factory input (import-and-upgrade model).
        jita_hist = _fetch_market_history(jita_region_id, tid)
        jita_stats = _compute_history_stats(jita_hist, days=30)
        # Full Jita-system books, not just top of book — a haul is one big
        # trade and walks the book down (or up, when buying inputs).
        jita_buy_book = _fetch_jita_book(tid, True)
        jita_sell_book = _fetch_jita_book(tid, False)
        jita_live = jita_buy_book[0][0] if jita_buy_book else 0
        jita_sell = jita_sell_book[0][0] if jita_sell_book else 0

        return tid, {
            # Buy order book within range (sorted desc by price)
            "local_buy": local_data["best_price"],
            "local_real_depth": local_data["real_depth"],
            "local_book": local_data["book"],
            "local_buyer_system": local_buyer_system,
            "local_buyer_jumps": local_buyer_jumps,
            "local_any_buy": local_data["best_any_price"],
            # 30-day VWAP — reference / sustained-sale fallback
            "local_vwap": local_stats["vwap"],
            "local_avg_daily_vol": local_stats["avg_daily_volume"],
            "local_total_vol": local_stats["total_volume"],
            "local_active_days": local_stats["active_days"],
            "local_order_count": local_stats["total_order_count"],
            # Jita
            "jita_vwap": jita_stats["vwap"],
            "jita_avg_daily_vol": jita_stats["avg_daily_volume"],
            "jita_total_vol": jita_stats["total_volume"],
            "jita_active_days": jita_stats["active_days"],
            "jita_order_count": jita_stats["total_order_count"],
            "jita_buy": jita_live,
            "jita_sell": jita_sell,
            "jita_buy_book": jita_buy_book,
            "jita_sell_book": jita_sell_book,
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for tid, result in pool.map(lambda t: _fetch_one(t), type_ids):
            prices[tid] = result

    return prices


# ── Layout computation ────────────────────────────────────────

def _ecu_pg_cpu(heads=DEFAULT_ECU_HEADS):
    """PG/CPU for one ECU with N extractor heads."""
    c = FACILITY_COSTS
    pg = c["ecu_base"]["pg"] + heads * c["ecu_per_head"]["pg"]
    cpu = c["ecu_base"]["cpu"] + heads * c["ecu_per_head"]["cpu"]
    return pg, cpu


def _planet_budget_remaining(pg_budget, cpu_budget, facilities,
                             heads=DEFAULT_ECU_HEADS):
    """Calculate remaining PG/CPU after placing facilities."""
    pg_used = 0
    cpu_used = 0
    c = FACILITY_COSTS
    for f, count in facilities.items():
        if f == "ecu":
            epg, ecpu = _ecu_pg_cpu(heads)
            pg_used += epg * count
            cpu_used += ecpu * count
        elif f in c:
            pg_used += c[f]["pg"] * count
            cpu_used += c[f]["cpu"] * count
    return pg_budget - pg_used, cpu_budget - cpu_used


def _bif_p1_output(p0_per_hr, num_bifs):
    """Calculate actual P1 output for N BIFs given a P0 extraction rate.

    A BIF needs 6000 P0/hr for full 40 P1/hr output. If underfed, it
    produces proportionally less. P0 supply is split evenly across BIFs.
    """
    bif_full_input = 6000
    bif_full_output = 40
    if num_bifs == 0:
        return 0
    p0_per_bif = p0_per_hr / num_bifs
    utilization = min(1.0, p0_per_bif / bif_full_input)
    return num_bifs * bif_full_output * utilization


def _copy_layout(layout):
    """Detached copy of a cached layout — callers mutate these in place."""
    if layout is None:
        return None
    out = dict(layout)
    out["facilities"] = dict(layout["facilities"])
    for k in ("p0_consumed_hr", "bif_split"):
        if isinstance(out.get(k), list):
            out[k] = list(out[k])
    return out


def compute_p1_layout(chain, extraction_rate, pg_budget, cpu_budget):
    """Compute layout for a P1 extraction planet.

    Head count is searched, not assumed: an ECU costs 2,600 PG plus 550 per
    head, so on a tight planet a real player drops heads to free power for
    BIFs. Extraction rate is calibrated at DEFAULT_ECU_HEADS and scaled
    linearly with the head count actually placed.

    Returns: {facilities, units_hr, volume_hr, pg_used, cpu_used} or None
    """
    return _copy_layout(_p1_layout_cached(
        chain["volume"], extraction_rate, pg_budget, cpu_budget))


# The allocator re-evaluates the same (rate, budget) pairs tens of thousands of
# times; the head-count search made that hot loop far too slow uncached.
@functools.lru_cache(maxsize=200000)
def _p1_layout_cached(volume, extraction_rate, pg_budget, cpu_budget):
    bif_input_rate = 6000
    best = None

    for heads in range(DEFAULT_ECU_HEADS, 0, -1):
        p0_per_hr = extraction_rate * heads / DEFAULT_ECU_HEADS
        # How many full BIFs can this extraction rate support?
        # Always place at least 1 BIF (it runs at partial capacity if underfed)
        max_bifs = max(1, int(p0_per_hr / bif_input_rate))
        for num_bifs in range(max_bifs, 0, -1):
            facilities = {"ecu": 1, "bif": num_bifs, "launchpad": 1}
            pg_rem, cpu_rem = _planet_budget_remaining(
                pg_budget, cpu_budget, facilities, heads)
            if pg_rem < 0 or cpu_rem < 0:
                continue
            # Actual P1 output accounts for partial BIF feeding
            units_hr = _bif_p1_output(p0_per_hr, num_bifs)
            if best is None or units_hr > best["units_hr"]:
                best = {
                    "facilities": facilities,
                    "units_hr": units_hr,
                    "volume_hr": units_hr * volume,
                    "pg_used": pg_budget - pg_rem,
                    "cpu_used": cpu_budget - cpu_rem,
                    "role": "extractor",
                    "ecu_heads": heads,
                    "p0_consumed_hr": min(p0_per_hr,
                                          num_bifs * bif_input_rate),
                }
            break  # more BIFs never helps at this head count

    return best


def compute_p2_selfcontained_layout(chain, extraction_rates, pg_budget, cpu_budget):
    """Compute layout for a self-contained P2 planet (both P0s on same planet).

    extraction_rates: list of 2 P0 extraction rates for the planet.
    Returns layout dict or None.
    """
    return _copy_layout(_p2sc_layout_cached(
        chain["volume"], tuple(extraction_rates), pg_budget, cpu_budget))


@functools.lru_cache(maxsize=200000)
def _p2sc_layout_cached(volume, extraction_rates, pg_budget, cpu_budget):
    bif_input_rate = 6000
    aif_p1_input_rate = 40  # P1/hr each type per AIF
    aif_output_rate = 5     # P2/hr per AIF
    best = None

    # Two ECUs at 10 heads is 16,200 PG on its own — more than a level-5
    # command centre can spare once BIFs, an AIF and a launchpad are placed.
    # Search head counts so this layout is judged the way a player would build
    # it (fewer heads, still self-contained) rather than declared impossible.
    for heads in range(DEFAULT_ECU_HEADS, 0, -1):
        rates = [r * heads / DEFAULT_ECU_HEADS for r in extraction_rates]
        # Each P0 line: 1+ BIFs, actual P1 output depends on extraction rate
        bifs_per_p0 = []
        p1_outputs = []
        for rate in rates:
            num_bifs = max(1, int(rate / bif_input_rate))
            bifs_per_p0.append(num_bifs)
            p1_outputs.append(_bif_p1_output(rate, num_bifs))

        # AIF throughput limited by the slower P1 line
        min_p1 = min(p1_outputs)
        # Each AIF needs 40 P1/hr from each input; fractional throughput allowed
        max_aifs_by_p1 = min_p1 / aif_p1_input_rate  # may be < 1.0

        total_bifs = sum(bifs_per_p0)
        for num_aifs in range(max(1, int(max_aifs_by_p1)), 0, -1):
            facilities = {"ecu": 2, "bif": total_bifs, "aif": num_aifs,
                          "launchpad": 1}
            pg_rem, cpu_rem = _planet_budget_remaining(
                pg_budget, cpu_budget, facilities, heads)
            if pg_rem < 0 or cpu_rem < 0:
                continue
            # Actual output: limited by slower P1 line and AIF count
            units_hr = min(num_aifs, max_aifs_by_p1) * aif_output_rate
            if best is None or units_hr > best["units_hr"]:
                best = {
                    "facilities": facilities,
                    "units_hr": units_hr,
                    "volume_hr": units_hr * volume,
                    "pg_used": pg_budget - pg_rem,
                    "cpu_used": cpu_budget - cpu_rem,
                    "role": "self-contained",
                    "ecu_heads": heads,
                    "p0_consumed_hr": [min(r, b * bif_input_rate)
                                       for r, b in zip(rates, bifs_per_p0)],
                    "bif_split": list(bifs_per_p0),
                }
            break  # fewer AIFs never helps at this head count

    return best


def compute_factory_layout(chain, pg_budget, cpu_budget):
    """Compute layout for a P2/P3 factory planet (no extractors, only AIFs).

    Returns layout dict or None.
    """
    aif_output_rate = 5 if chain["tier"] == "P2" else 3
    c = FACILITY_COSTS

    # Factory planet: just AIFs + launchpad(s)
    # Start with 1 launchpad, fill with AIFs
    lp_pg, lp_cpu = c["launchpad"]["pg"], c["launchpad"]["cpu"]
    aif_pg, aif_cpu = c["aif"]["pg"], c["aif"]["cpu"]

    pg_avail = pg_budget - lp_pg
    cpu_avail = cpu_budget - lp_cpu

    if pg_avail < aif_pg or cpu_avail < aif_cpu:
        return None

    max_aifs_pg = int(pg_avail / aif_pg)
    max_aifs_cpu = int(cpu_avail / aif_cpu)
    num_aifs = min(max_aifs_pg, max_aifs_cpu)

    if num_aifs < 1:
        return None

    facilities = {"aif": num_aifs, "launchpad": 1}
    pg_rem, cpu_rem = _planet_budget_remaining(pg_budget, cpu_budget, facilities)

    units_hr = num_aifs * aif_output_rate
    volume_hr = units_hr * chain["volume"]

    return {
        "facilities": facilities,
        "units_hr": units_hr,
        "volume_hr": volume_hr,
        "pg_used": pg_budget - pg_rem,
        "cpu_used": cpu_budget - cpu_rem,
        "role": "factory",
    }


def _p3_factory_cascade(chain, p1_supply, ctx):
    """Trace P1 supply -> P2 AIFs -> P3 AIFs for a P3 combined factory.

    p1_supply: {p1_type_id: units/hr available}.
    Returns {units_hr, p2_aif_details, p3_aif_count, total_aifs,
             aif_breakdown, flags}. Pure throughput math — no PG/CPU check,
     so it can be re-run with user-overridden P1 supply for what-if recalc.
    """
    pi_types = ctx["pi_types"]
    schematics = ctx["schematics"]
    flags = []

    p3_cycle = chain["schematic"]["cycle_time"]
    p3_out_qty = chain["schematic"]["output"]["quantity"]
    p3_out_per_aif_hr = p3_out_qty * (3600 / p3_cycle)

    # For each P2 input of the P3 chain, compute max AIFs from P1 supply
    p2_aif_details = []
    for p2_inp in chain["inputs"]:
        p2_tid = p2_inp["type_id"]
        p2_type = pi_types.get(p2_tid)
        if not p2_type:
            continue

        # Find P2 production schematic
        p2_sch = None
        for sid in p2_type.get("produced_by", []):
            p2_sch = schematics.get(sid)
            if p2_sch:
                break
        if not p2_sch:
            flags.append(f"NO SCHEMATIC ({p2_type['name']})")
            continue

        p2_cycle = p2_sch["cycle_time"]
        p2_out_qty = p2_sch["output"]["quantity"]
        p2_out_per_aif_hr = p2_out_qty * (3600 / p2_cycle)

        # Max P2 AIFs from P1 supply (each P2 AIF needs X P1/hr per input)
        max_aifs = float('inf')
        for p1_inp in p2_sch["inputs"]:
            p1_tid = p1_inp["type_id"]
            p1_need_per_aif_hr = p1_inp["quantity"] * (3600 / p2_cycle)
            supply = p1_supply.get(p1_tid, 0)
            if p1_need_per_aif_hr > 0:
                max_aifs = min(max_aifs, supply / p1_need_per_aif_hr)

        aif_count = int(max_aifs) if max_aifs != float('inf') else 0
        p2_aif_details.append({
            "name": p2_type["name"],
            "type_id": p2_tid,
            "aif_count": aif_count,
            "out_per_aif_hr": p2_out_per_aif_hr,
            "total_hr": aif_count * p2_out_per_aif_hr,
        })

    # P3 AIFs limited by P2 supply
    max_p3_aifs = 0.0
    if p2_aif_details:
        max_p3_aifs = float('inf')
        for i, p2_inp in enumerate(chain["inputs"]):
            p2_need_per_p3_hr = p2_inp["quantity"] * (3600 / p3_cycle)
            p2_supply = p2_aif_details[i]["total_hr"] if i < len(p2_aif_details) else 0
            if p2_need_per_p3_hr > 0:
                max_p3_aifs = min(max_p3_aifs, p2_supply / p2_need_per_p3_hr)
        if max_p3_aifs == float('inf'):
            max_p3_aifs = 0.0

    p3_aif_count = int(max_p3_aifs)
    if p3_aif_count == 0 and max_p3_aifs >= 0.1:
        p3_aif_count = 1  # allow partial throughput

    actual_p3 = min(p3_aif_count, max_p3_aifs)
    units_hr = actual_p3 * p3_out_per_aif_hr

    # Trim excess P2 AIFs — don't produce more P2 than the P3 stage consumes
    for i, p2_inp in enumerate(chain["inputs"]):
        if i < len(p2_aif_details):
            p2_need_total = p2_inp["quantity"] * (3600 / p3_cycle) * actual_p3
            out_per = p2_aif_details[i]["out_per_aif_hr"]
            if out_per > 0:
                needed = math.ceil(p2_need_total / out_per)
                p2_aif_details[i]["aif_count"] = min(
                    p2_aif_details[i]["aif_count"], needed)
                p2_aif_details[i]["total_hr"] = (
                    p2_aif_details[i]["aif_count"] * out_per)

    total_p2_aifs = sum(d["aif_count"] for d in p2_aif_details)
    total_aifs = total_p2_aifs + p3_aif_count

    # Build AIF breakdown for display
    aif_breakdown = []
    for d in p2_aif_details:
        aif_breakdown.append(
            f"{d['aif_count']} AIF -> {d['total_hr']:.0f} {d['name']}/hr")
    aif_breakdown.append(
        f"{p3_aif_count} AIF -> {units_hr:.1f} {chain['output_name']}/hr")

    return {
        "units_hr": units_hr,
        "p2_aif_details": p2_aif_details,
        "p3_aif_count": p3_aif_count,
        "total_aifs": total_aifs,
        "aif_breakdown": aif_breakdown,
        "flags": flags,
    }


# ── Chain analysis ────────────────────────────────────────────

def _build_p0_to_p1(pi_types, schematics):
    """Map P0 name -> its P1 product {name, volume, type_id}."""
    p0_to_p1 = {}
    for _sid, sch in schematics.items():
        s_inputs = sch.get("inputs", [])
        s_output = sch.get("output", {})
        if len(s_inputs) != 1:
            continue
        inp_t = pi_types.get(s_inputs[0]["type_id"])
        out_t = pi_types.get(s_output.get("type_id"))
        if (inp_t and out_t and inp_t.get("tier") == "P0"
                and out_t.get("tier") == "P1"):
            p0_to_p1[inp_t["name"]] = {
                "name": out_t["name"], "volume": out_t["volume"],
                "type_id": s_output["type_id"]}
    return p0_to_p1


def _build_analysis_ctx(chains, pi_types, schematics, planet_inv,
                        extraction_rates, density_data, cfg,
                        planet_taxes=None):
    """Shared context for chain analysis.

    Precomputes per-instance rate tables and ranked candidate lists so a
    chain's planets can be re-resolved cheaply for any subset of systems
    (combo-local selection) or with instances excluded (deconfliction).
    """
    inv = active_inventory(planet_inv)
    estimator, calibration = build_density_estimator(extraction_rates,
                                                     density_data)
    instance_rates = _build_instance_rates(inv, extraction_rates,
                                           density_data, estimator)
    p0_candidates, p0_by_system = _build_p0_candidates(instance_rates)
    return {
        "calibration": calibration,
        "chains": chains,
        "pi_types": pi_types,
        "schematics": schematics,
        "inv": inv,
        "instance_rates": instance_rates,
        "p0_candidates": p0_candidates,
        "p0_by_system": p0_by_system,
        "planet_taxes": planet_taxes or {},
        "cfg": cfg,
        "pg_budget": cfg["pg_budget"],
        "cpu_budget": cfg["cpu_budget"],
        "p0_to_p1": _build_p0_to_p1(pi_types, schematics),
        "sc_cache": {},   # tid -> self-contained candidate list
        "vc_memo": {},    # (tid, selection) -> built vc dict
    }


def _best_candidate(p0_name, ctx, combo_set=None, exclude=frozenset()):
    """Best (rate, tag, system, ptype, instance) for extracting a P0.

    combo_set restricts to those systems; exclude skips claimed instances.
    """
    if combo_set is None:
        for cand in ctx["p0_candidates"].get(p0_name, []):
            if (cand[2], cand[3], cand[4]) not in exclude:
                return cand
        return None
    by_sys = ctx["p0_by_system"].get(p0_name, {})
    best = None
    for system in combo_set:
        for cand in by_sys.get(system, []):
            if (cand[2], cand[3], cand[4]) in exclude:
                continue
            if best is None or cand[0] > best[0]:
                best = cand
            break  # per-system lists are sorted desc
    return best


def _sc_candidates_for_chain(chain, ctx):
    """Self-contained P2 candidates — both P0s extracted on ONE physical
    planet instance, using that instance's own rates. Best-first, cached.
    """
    tid = chain["output_type_id"]
    cached = ctx["sc_cache"].get(tid)
    if cached is not None:
        return cached
    p0_names = [p["name"] for p in chain["p0_inputs"]]
    ptype_sets = [P0_PLANET_MAP.get(n, set()) for n in p0_names]
    common = set.intersection(*ptype_sets) if ptype_sets else set()
    cands = []
    for (system, ptype, inst), rates in ctx["instance_rates"].items():
        if ptype not in common:
            continue
        pair = [rates.get(n) for n in p0_names]
        if any(r is None or r[0] <= 0 for r in pair):
            continue
        layout = compute_p2_selfcontained_layout(
            chain, [r[0] for r in pair], ctx["pg_budget"], ctx["cpu_budget"])
        if not layout:
            continue
        cands.append({"units_hr": layout["units_hr"], "system": system,
                      "ptype": ptype, "instance": inst})
    cands.sort(key=lambda c: (-c["units_hr"], c["system"], c["instance"]))
    ctx["sc_cache"][tid] = cands
    return cands


def _pick_factory_planet(primary_systems, fallback_systems, exclude, ctx):
    """Pick the cheapest-tax spare planet to host a factory.

    Factories have no extraction needs, so POCO tax is the economic
    criterion. Planets in primary_systems (the chain's extraction systems)
    are preferred — a co-located factory adds no route cost; fallback
    systems are only considered when no primary spare exists.
    Returns (system, ptype, instance, tax_rate) or None.
    """
    taxes = ctx["planet_taxes"]
    default_rate = ctx["cfg"]["tax_rate"]

    def _best_in(systems):
        best = None
        for system in systems:
            for ptype, count in ctx["inv"].get(system, {}).items():
                for i in range(count):
                    inst = chr(ord("A") + i)
                    if (system, ptype, inst) in exclude:
                        continue
                    rate = taxes.get(f"{system}.{ptype}.{inst}", default_rate)
                    if best is None or rate < best[3]:
                        best = (system, ptype, inst, rate)
        return best

    primary = list(dict.fromkeys(primary_systems))
    pick = _best_in(primary)
    if pick:
        return pick
    return _best_in([s for s in fallback_systems if s not in set(primary)])


def _resolve_chain_selection(chain, ctx, combo_set=None, exclude=frozenset()):
    """Resolve which planet instances a chain would use.

    combo_set restricts selection to those systems (None = all systems).
    exclude is a set of (system, ptype, instance) already claimed — used
    both within a chain (two P0s never share one planet) and across
    chains (deconfliction).
    Returns (selection, fail_flags); selection is None when the chain
    cannot be placed, with fail_flags explaining why (for global tables).
    """
    tier = chain["tier"]

    if tier == "P1":
        if not chain["p0_inputs"]:
            return None, []
        p0 = chain["p0_inputs"][0]["name"]
        cand = _best_candidate(p0, ctx, combo_set, exclude)
        if not cand:
            return None, [f"NO {', '.join(sorted(P0_PLANET_MAP.get(p0, set())))}"]
        return ("p1", cand), []

    if tier == "P2":
        if len(chain["p0_inputs"]) < 2:
            return None, []
        # Self-contained preferred (1 planet vs 3)
        for c in _sc_candidates_for_chain(chain, ctx):
            if combo_set is not None and c["system"] not in combo_set:
                continue
            if (c["system"], c["ptype"], c["instance"]) in exclude:
                continue
            return ("p2sc", (c["system"], c["ptype"], c["instance"])), []
        # Factory setup: one extractor per P0 + factory planet
        p0_names = [p["name"] for p in chain["p0_inputs"]]
        exts = []
        used = set(exclude)
        for name in p0_names:
            cand = _best_candidate(name, ctx, combo_set, used)
            if not cand:
                return None, [f"NO {', '.join(sorted(P0_PLANET_MAP.get(name, set())))}"]
            exts.append((name, cand))
            used.add((cand[2], cand[3], cand[4]))
        fac = _pick_factory_planet(
            [c[1][2] for c in exts],
            sorted(combo_set) if combo_set is not None else sorted(ctx["inv"]),
            used, ctx)
        if not fac:
            return None, ["NO SPARE PLANET (factory)"]
        return ("p2f", tuple(exts), fac), []

    if tier == "P3":
        p0_names = list(chain["all_p0_names"])
        exts = []
        missing = []
        used = set(exclude)
        for name in p0_names:
            cand = _best_candidate(name, ctx, combo_set, used)
            if not cand:
                missing.append(name)
                continue
            exts.append((name, cand))
            used.add((cand[2], cand[3], cand[4]))
        if combo_set is not None and (missing or not exts):
            return None, []  # incomplete chains don't enter layouts
        fac = _pick_factory_planet(
            [c[1][2] for c in exts],
            sorted(combo_set) if combo_set is not None else sorted(ctx["inv"]),
            used, ctx)
        return ("p3", tuple(exts), fac, tuple(missing)), []

    return None, []


def _factory_planet_entry(fac, chain, factory_layout):
    system, ptype, inst, _tax = fac
    return {"system": system, "type": f"{ptype} {inst}",
            "role": f"Factory -> {chain['output_name']}",
            "layout": factory_layout, "is_factory": True}


def _not_viable(chain, flags):
    return {"chain": chain, "viable": False, "flags": flags,
            "planets_used": [], "units_hr": 0, "volume_hr": 0}


def _build_p1_vc(chain, sel, ctx):
    rate, tag, system, ptype, inst = sel[1]
    p0_name = chain["p0_inputs"][0]["name"]
    layout = compute_p1_layout(chain, rate, ctx["pg_budget"], ctx["cpu_budget"])
    if not layout:
        return _not_viable(chain, ["POWER LIMIT"])
    return {
        "chain": chain, "viable": True, "layout_type": "p1_extractor",
        "planets_used": [{"system": system, "type": f"{ptype} {inst}",
                          "role": f"Extract {p0_name} -> {chain['output_name']}",
                          "layout": layout,
                          "extract_rates": [rate],
                          "rate_detail": f"{p0_name}: {rate:.0f}/hr [{tag}]"}],
        "planet_count": 1,
        "units_hr": layout["units_hr"],
        "volume_hr": layout["volume_hr"],
        "rate_sources": [tag],
        "flags": [],
    }


def _build_p2sc_vc(chain, sel, ctx):
    system, ptype, inst = sel[1]
    p0_names = [p["name"] for p in chain["p0_inputs"]]
    inst_rates = ctx["instance_rates"].get((system, ptype, inst), {})
    pair = [inst_rates.get(n, (0, "?")) for n in p0_names]
    rates = [p[0] for p in pair]
    tags = [p[1] for p in pair]
    layout = compute_p2_selfcontained_layout(chain, rates, ctx["pg_budget"],
                                             ctx["cpu_budget"])
    if not layout:
        return _not_viable(chain, ["POWER LIMIT"])
    rate_details = []
    bottleneck_idx = rates.index(min(rates))
    for i, (p0_name, rate, tag) in enumerate(zip(p0_names, rates, tags)):
        bif_input = 6000
        headroom = (rate - bif_input) / bif_input * 100 if rate > 0 else 0
        marker = " <- BOTTLENECK" if i == bottleneck_idx and len(p0_names) > 1 else ""
        rate_details.append(f"{p0_name}: {rate:.0f}/hr [{tag}] "
                            f"({headroom:+.0f}% headroom){marker}")
    return {
        "chain": chain, "viable": True, "layout_type": "p2_selfcontained",
        "planets_used": [{"system": system, "type": f"{ptype} {inst}",
                          "role": f"Extract+Process -> {chain['output_name']}",
                          "layout": layout, "rate_details": rate_details,
                          "extract_rates": list(rates),
                          "p0_names": list(p0_names)}],
        "planet_count": 1,
        "units_hr": layout["units_hr"],
        "volume_hr": layout["volume_hr"],
        "rate_sources": tags,
        "flags": [],
    }


def _build_p2f_vc(chain, sel, ctx):
    exts, fac = sel[1], sel[2]
    pi_types = ctx["pi_types"]
    pg_budget, cpu_budget = ctx["pg_budget"], ctx["cpu_budget"]

    extraction_planets = []
    all_tags = []
    for i, (p0_name, cand) in enumerate(exts):
        rate, tag, system, ptype, inst = cand
        p1_input = chain["inputs"][i]
        p1_type = pi_types.get(p1_input["type_id"])
        if not p1_type:
            continue
        p1_chain = {"volume": p1_type["volume"], "tier": "P1"}
        p1_layout = compute_p1_layout(p1_chain, rate, pg_budget, cpu_budget)
        if not p1_layout:
            return _not_viable(chain, ["POWER LIMIT"])
        extraction_planets.append({
            "system": system, "type": f"{ptype} {inst}",
            "role": f"Extract {p0_name} -> {p1_type['name']}",
            "layout": p1_layout,
            "p1_output_hr": p1_layout["units_hr"],
            "extract_rates": [rate],
            "p1_type_id": p1_input["type_id"],
            "p1_name": p1_type["name"],
            "p1_volume": p1_type["volume"],
            "rate_detail": f"{p0_name}: {rate:.0f}/hr [{tag}]",
        })
        all_tags.append(tag)

    if not extraction_planets:
        return _not_viable(chain, ["NO P1 SUPPLY"])

    factory_layout = compute_factory_layout(chain, pg_budget, cpu_budget)
    if not factory_layout:
        return _not_viable(chain, ["POWER LIMIT"])

    aif_p1_need = 40
    min_p1_supply = min(ep["p1_output_hr"] for ep in extraction_planets)
    max_aifs_by_supply = max(1, int(min_p1_supply / aif_p1_need))
    actual_aifs = min(factory_layout["facilities"]["aif"], max_aifs_by_supply)

    units_hr = actual_aifs * 5
    volume_hr = units_hr * chain["volume"]

    planets = list(extraction_planets)
    planets.append(_factory_planet_entry(fac, chain, factory_layout))

    return {
        "chain": chain, "viable": True, "layout_type": "p2_factory",
        "planets_used": planets, "planet_count": len(planets),
        "units_hr": units_hr, "volume_hr": volume_hr,
        "rate_sources": all_tags, "flags": [],
    }


def _build_p3_vc(chain, sel, ctx):
    """Build a P3 chain vc from its selection (extractors + factory)."""
    exts, fac, missing = sel[1], sel[2], sel[3]
    pi_types = ctx["pi_types"]
    schematics = ctx["schematics"]
    pg_budget, cpu_budget = ctx["pg_budget"], ctx["cpu_budget"]
    p0_names = list(chain["all_p0_names"])
    p0_to_p1 = ctx["p0_to_p1"]

    flags = []
    for p0_name in missing:
        compatible_ptypes = P0_PLANET_MAP.get(p0_name, set())
        flags.append(f"NO {', '.join(sorted(compatible_ptypes))} (for {p0_name})")

    extraction_planets = []
    all_tags = []
    for p0_name, cand in exts:
        rate, tag, system, ptype, inst = cand
        p1_info = p0_to_p1.get(p0_name, {"name": "P1", "volume": 0.38,
                                          "type_id": 0})
        p1_chain = {"volume": p1_info["volume"], "tier": "P1"}
        p1_layout = compute_p1_layout(p1_chain, rate, pg_budget, cpu_budget)
        if not p1_layout:
            flags.append(f"POWER LIMIT (for {p0_name})")
            continue
        extraction_planets.append({
            "system": system, "type": f"{ptype} {inst}",
            "role": f"Extract {p0_name} -> {p1_info['name']}",
            "layout": p1_layout,
            "p1_output_hr": p1_layout["units_hr"],
            "p1_name": p1_info["name"],
            "p1_type_id": p1_info["type_id"],
            "p1_volume": p1_info["volume"],
            "extract_rates": [rate],
            "rate_detail": f"{p0_name}: {rate:.0f}/hr [{tag}]",
        })
        all_tags.append(tag)

    if fac is None:
        flags.append("NO SPARE PLANET (factory)")
        return _not_viable(chain, flags)

    total_needed = len(extraction_planets) + 1  # extractors + 1 combined factory

    if total_needed > 5:
        flags.append("EXCEEDS 5 PLANETS")

    # ── P3 factory: trace real P1 -> P2 -> P3 supply chain ──
    #
    # Build P1 supply map from extraction planets
    p1_supply = {}  # P1 type_id -> units/hr
    for ep in extraction_planets:
        p1_tid = ep.get("p1_type_id", 0)
        if p1_tid:
            p1_supply[p1_tid] = p1_supply.get(p1_tid, 0) + ep["p1_output_hr"]

    casc = _p3_factory_cascade(chain, p1_supply, ctx)
    flags.extend(casc["flags"])
    units_hr = casc["units_hr"]
    p3_aif_count = casc["p3_aif_count"]
    total_aifs = casc["total_aifs"]
    aif_breakdown = casc["aif_breakdown"]

    # Check PG/CPU
    facilities = {"aif": total_aifs, "launchpad": 1}
    pg_rem, cpu_rem = _planet_budget_remaining(pg_budget, cpu_budget, facilities)

    if pg_rem < 0 or cpu_rem < 0:
        flags.append("FACTORY POWER LIMIT")
        return {
            "chain": chain, "viable": False, "flags": flags,
            "planets_used": [], "units_hr": 0, "volume_hr": 0,
        }

    if units_hr <= 0:
        flags.append("NO P1 SUPPLY")
        return {
            "chain": chain, "viable": False, "flags": flags,
            "planets_used": [], "units_hr": 0, "volume_hr": 0,
        }

    volume_hr = units_hr * chain["volume"]

    factory_layout = {
        "facilities": facilities,
        "units_hr": units_hr,
        "volume_hr": volume_hr,
        "pg_used": pg_budget - pg_rem,
        "cpu_used": cpu_budget - cpu_rem,
        "role": "factory",
        "aif_breakdown": aif_breakdown,
    }

    # Build planets_used: extraction planets + factory planet
    planets = list(extraction_planets)
    planets.append(_factory_planet_entry(fac, chain, factory_layout))

    viable = ("EXCEEDS 5 PLANETS" not in flags
              and not any("NO " in f for f in flags)
              and len(extraction_planets) == len(p0_names))

    return {
        "chain": chain,
        "viable": viable,
        "layout_type": "p3_multi",
        "planets_used": planets,
        "planet_count": total_needed,
        "units_hr": units_hr,
        "volume_hr": volume_hr,
        "rate_sources": all_tags,
        "flags": flags,
    }


_VC_BUILDERS = {
    "p1": _build_p1_vc,
    "p2sc": _build_p2sc_vc,
    "p2f": _build_p2f_vc,
    "p3": _build_p3_vc,
}


def _attach_alloc_fields(vc):
    """Planet-type demand counts for the allocation knapsack.

    Factory planets count as 'Any' — any spare planet can host a factory,
    so allocation stays flexible even though a concrete cheap-tax planet
    is already pencilled in for the tax estimate.
    """
    types_needed = {}
    pc = 0
    for p in vc.get("planets_used", []):
        if p.get("is_factory"):
            base = "Any"
        else:
            t = p.get("type", "")
            parts = t.rsplit(" ", 1)
            if len(parts) == 2 and len(parts[1]) <= 2:
                base = parts[0]
            else:
                base = t or "Any"
        types_needed[base] = types_needed.get(base, 0) + 1
        pc += 1
    if pc == 0:
        pc = vc.get("planet_count", 1) or 1
        types_needed = {"Any": pc}
    vc["_alloc_types"] = types_needed
    vc["_alloc_pc"] = pc


def _types_fit(types_needed, pool, total_pool):
    """Check a chain's planet-type demands against a pooled inventory."""
    any_needed = types_needed.get("Any", 0)
    specific = 0
    for t, c in types_needed.items():
        if t == "Any":
            continue
        if pool.get(t, 0) < c:
            return False
        specific += c
    return any_needed <= total_pool - specific


def _planet_instance_key(p):
    """(system, ptype, instance) for a planets_used entry, or None."""
    t = p.get("type", "")
    system = p.get("system", "")
    if not system or not t or t == "Any":
        return None
    parts = t.rsplit(" ", 1)
    if len(parts) == 2 and len(parts[1]) <= 2:
        return (system, parts[0], parts[1])
    return (system, t, "A")


def _analyse_chain(chain, ctx, combo_set=None, exclude=frozenset()):
    """Resolve a P1/P2/P3 chain's planets and build its analysis dict.

    Memoized on (chain, selection) so re-analysis across system combos is
    cheap. Returns None when the chain can't be placed in combo mode; in
    global mode (combo_set=None) returns a non-viable stub with flags so
    the per-tier tables can show why.
    """
    sel, fail_flags = _resolve_chain_selection(chain, ctx, combo_set, exclude)
    if sel is None:
        if fail_flags and combo_set is None:
            return _not_viable(chain, fail_flags)
        return None
    key = (chain["output_type_id"], sel)
    vc = ctx["vc_memo"].get(key)
    if vc is None:
        vc = _VC_BUILDERS[sel[0]](chain, sel, ctx)
        if vc is not None:
            vc["_sel_sig"] = sel
            _attach_alloc_fields(vc)
            ctx["vc_memo"][key] = vc
    return vc


def find_viable_chains(chains, pi_types, schematics, planet_inv,
                       extraction_rates, density_data, cfg,
                       planet_taxes=None, ctx=None):
    """For each producible chain, compute layout options.

    extraction_rates / density_data use 'System.Type.Instance' keys as
    returned by load_extraction_rates() / load_planet_density().
    Returns list of analysed chain dicts (viable and non-viable, with
    flags); economics are computed separately by compute_economics().
    """
    if ctx is None:
        ctx = _build_analysis_ctx(chains, pi_types, schematics, planet_inv,
                                  extraction_rates, density_data, cfg,
                                  planet_taxes)

    # Flattened inventory for the P4 estimator
    flat_inv = {}
    total_by_type = {}
    for system, planets in ctx["inv"].items():
        for ptype, count in planets.items():
            flat_inv.setdefault(ptype, []).append((system, count))
            total_by_type[ptype] = total_by_type.get(ptype, 0) + count

    results = []
    for tid, chain in chains.items():
        tier = chain["tier"]
        if tier in ("P1", "P2", "P3"):
            result = _analyse_chain(chain, ctx)
        elif tier == "P4":
            result = _analyse_p4_chain(chain, pi_types, schematics, flat_inv,
                                       total_by_type, None,
                                       cfg["pg_budget"], cfg["cpu_budget"],
                                       cfg["max_planets"])
        else:
            continue
        if result:
            results.append(result)
    return results


def _min_extraction_planets(p0_names, flat_inv):
    """Estimate minimum extraction planets by greedily pairing P0s.

    Two P0s can share one planet if both are available on the same planet
    type in the inventory. Max 2 ECU per planet (PG budget ~14100/17000
    for 2 ECU + 2 BIF + 1 LP at CCU 4).
    """
    available = {}
    for p0_name in p0_names:
        ptypes = P0_PLANET_MAP.get(p0_name, set())
        avail = set()
        for pt in ptypes:
            if any(count > 0 for _, count in flat_inv.get(pt, [])):
                avail.add(pt)
        available[p0_name] = avail

    remaining = list(p0_names)
    planets = 0
    while remaining:
        p0 = remaining.pop(0)
        ptypes_a = available.get(p0, set())
        paired = False
        for i, other in enumerate(remaining):
            if ptypes_a & available.get(other, set()):
                remaining.pop(i)
                planets += 1
                paired = True
                break
        if not paired:
            planets += 1
    return planets


def _analyse_p4_chain(chain, pi_types, schematics, flat_inv, total_by_type,
                      rate_ctx, pg_budget, cpu_budget, max_planets):
    """Analyse a P4 chain. Full vertical integration — all P0 through P4.

    P4 products are produced in a High-Tech Industry Facility (HTIF).
    Full-chain production requires extraction planets for every P0 input
    plus factory planets for P2/P3/P4 processing.
    """
    p0_names = list(chain["all_p0_names"])
    flags = []

    # Estimate minimum planets needed
    min_extraction = _min_extraction_planets(p0_names, flat_inv)
    factory_planets = 1  # P2/P3/P4 processing combined
    total_needed = min_extraction + factory_planets

    # HTIF output: 1 unit per cycle (all P4 schematics)
    p4_cycle = chain["schematic"]["cycle_time"]
    p4_out_qty = chain["schematic"]["output"]["quantity"]
    units_hr = p4_out_qty * (3600 / p4_cycle)

    # Check if any P0 has no available planet type
    missing_p0 = []
    for p0_name in p0_names:
        ptypes = P0_PLANET_MAP.get(p0_name, set())
        if not any(total_by_type.get(pt, 0) > 0 for pt in ptypes):
            missing_p0.append(p0_name)
    if missing_p0:
        flags.append(f"MISSING PLANETS ({', '.join(missing_p0)})")

    if total_needed > max_planets:
        flags.append(f"REQUIRES {total_needed} PLANETS (have {max_planets})")

    # Check factory PG/CPU for combined P2+P3+P4 processing
    # Conservative estimate: count P2 AIFs + P3 AIFs + HTIF + LP
    # Each P3 input needs ~2 P2 AIFs + 1 P3 AIF (from P3 analysis pattern)
    p3_inputs = [inp for inp in chain["inputs"] if inp["tier"] == "P3"]
    p1_inputs = [inp for inp in chain["inputs"] if inp["tier"] == "P1"]

    # Count upstream P2 intermediaries needed for each P3
    total_p2_aifs = 0
    total_p3_aifs = 0
    for p3_inp in p3_inputs:
        p3_type = pi_types.get(p3_inp["type_id"])
        if not p3_type:
            continue
        # Each P3 needs its own P2 AIFs — estimate 2 per P3 input type
        p3_sch = None
        for sid in p3_type.get("produced_by", []):
            p3_sch = schematics.get(sid)
            if p3_sch:
                break
        if p3_sch:
            total_p2_aifs += len(p3_sch["inputs"])  # 1 P2 AIF per P2 input
            total_p3_aifs += 1

    # Factory planet facilities
    fac = {"aif": total_p2_aifs + total_p3_aifs, "htif": 1, "launchpad": 1}
    pg_rem, cpu_rem = _planet_budget_remaining(pg_budget, cpu_budget, fac)
    if pg_rem < 0 or cpu_rem < 0:
        # Try splitting across 2 factory planets
        factory_planets = 2
        total_needed = min_extraction + factory_planets
        if total_needed > max_planets:
            flags.append(f"FACTORY SPLIT NEEDS {total_needed} PLANETS")

    viable = not any("REQUIRES" in f or "MISSING" in f or "FACTORY SPLIT" in f
                     for f in flags)

    return {
        "chain": chain,
        "viable": viable,
        "layout_type": "p4_full",
        "planets_used": [],
        "planet_count": total_needed,
        "unique_p0_count": len(p0_names),
        "extraction_planets": min_extraction,
        "factory_planets": factory_planets,
        "units_hr": units_hr if viable else 0,
        "volume_hr": (units_hr * chain["volume"]) if viable else 0,
        "flags": flags,
    }


# ── Economics ─────────────────────────────────────────────────

def _build_econ_ctx(market_prices, cfg, pi_types, matrix=None, home_id=None,
                    system_ids=None, planet_taxes=None):
    """Precompute shared inputs for per-chain economics."""
    home_system_id = esi.search_system_id(cfg["home_system"])
    jita_system_id = esi.search_system_id("Jita")
    jita_jumps = 0
    if home_system_id and jita_system_id:
        # secure flag: the Jita leg carries the full cargo value, so price
        # it at the route a hauler actually flies, not the ganky shortcut
        jita_jumps = esi.get_jump_count(home_system_id, jita_system_id,
                                        avoid=_AVOID_IDS, flag="secure")
        if jita_jumps < 0:
            jita_jumps = 15  # fallback
    return {"market_prices": market_prices, "cfg": cfg, "pi_types": pi_types,
            "matrix": matrix, "home_id": home_id, "system_ids": system_ids,
            "planet_taxes": planet_taxes, "jita_jumps": jita_jumps}


def compute_economics(viable_chains, market_prices, cfg, pi_types,
                      matrix=None, home_id=None, system_ids=None,
                      planet_taxes=None, ectx=None):
    """Compute ISK/hr, tax, haul time for each viable chain.

    Primary price: sustained realised price — walks the buy order book within
    max_market_jumps and blends with VWAP for production beyond order depth.
    This models what you'd actually earn over 30 days, not just the instant
    top-of-book price.
    """
    if ectx is None:
        ectx = _build_econ_ctx(market_prices, cfg, pi_types, matrix, home_id,
                               system_ids, planet_taxes)
    for vc in viable_chains:
        _compute_economics_single(vc, ectx)


def _compute_economics_single(vc, ectx):
    """Economics for one chain. Idempotent (guarded by _econ_done) so a
    chain shared between the global ranking and several layout candidates
    is priced once and never gets duplicate flags."""
    if vc.get("_econ_done"):
        return
    vc["_econ_done"] = True

    market_prices = ectx["market_prices"]
    cfg = ectx["cfg"]
    pi_types = ectx["pi_types"]
    matrix = ectx["matrix"]
    home_id = ectx["home_id"]
    system_ids = ectx["system_ids"]
    planet_taxes = ectx["planet_taxes"]
    jita_jumps = ectx["jita_jumps"]

    chain = vc["chain"]
    tid = chain["output_type_id"]
    prices = market_prices.get(tid, {})

    local_buy = prices.get("local_buy", 0)
    local_book = prices.get("local_book", [])
    local_real_depth = prices.get("local_real_depth", 0)
    local_buyer_system = prices.get("local_buyer_system", "")
    local_buyer_jumps = prices.get("local_buyer_jumps", 0)
    local_vwap = prices.get("local_vwap", 0)
    local_avg_daily_vol = prices.get("local_avg_daily_vol", 0)
    local_active_days = prices.get("local_active_days", 0)
    local_order_count = prices.get("local_order_count", 0)
    jita_vwap = prices.get("jita_vwap", 0)
    jita_avg_daily_vol = prices.get("jita_avg_daily_vol", 0)
    jita_active_days = prices.get("jita_active_days", 0)
    jita_buy = prices.get("jita_buy", 0)

    units_hr = vc.get("units_hr", 0)
    units_per_day = units_hr * 24

    # Compute sustained realised price: walk the order book, blend with VWAP
    sustained_price, real_buy_days = _compute_sustained_price(
        local_book, units_per_day, local_vwap)

    # Store all price signals
    vc["local_buy_price"] = local_buy  # top of book (snapshot)
    vc["local_sustained"] = sustained_price  # blended over 30d
    vc["local_real_depth"] = local_real_depth
    vc["local_real_buy_days"] = real_buy_days
    vc["local_buyer_system"] = local_buyer_system
    vc["local_buyer_jumps"] = local_buyer_jumps
    vc["local_vwap"] = local_vwap
    vc["local_avg_daily_vol"] = local_avg_daily_vol
    vc["local_active_days"] = local_active_days
    vc["local_order_count"] = local_order_count
    vc["jita_vwap"] = jita_vwap
    vc["jita_buy_price"] = jita_buy
    vc["jita_avg_daily_vol"] = jita_avg_daily_vol
    vc["jita_active_days"] = jita_active_days

    # Effective sell price. Default = sustained realised local price (blended
    # buy book + VWAP). When sell_at_jita is set, value output at Jita instead —
    # but as an instant dump of one haul window's production, which walks the
    # buy book down rather than all clearing at the top order. Anything the
    # book can't absorb prices at Jita VWAP.
    sell_at_jita = bool(cfg.get("sell_at_jita"))
    haul_days = cfg.get("haul_every_days", 1) or 1
    jita_fill = _jita_fill(prices, units_per_day * haul_days, True, haul_days)
    vc["jita_fill"] = jita_fill
    if sell_at_jita:
        sell_price = jita_fill["price"]
    else:
        sell_price = sustained_price
    # Every market sell pays transaction tax — realise it in the price
    sales_tax = cfg.get("sales_tax", 0.0)
    sell_price *= (1.0 - sales_tax)
    vc["sell_at_jita"] = sell_at_jita
    vc["sales_tax_rate"] = sales_tax
    vc["sell_price"] = sell_price

    # Gross ISK/hr uses the effective sell price
    vc["gross_isk_hr"] = units_hr * sell_price

    # Tax
    tax_per_unit = _compute_chain_tax(vc, pi_types, cfg, planet_taxes)
    vc["tax_per_hr"] = tax_per_unit * units_hr

    # Net ISK/hr = gross - tax
    vc["net_isk_hr"] = vc["gross_isk_hr"] - vc["tax_per_hr"]

    # Activity-adjusted ISK/hr — penalises products that rarely trade.
    # Uses sum(order_count) over the last 30 calendar days as the signal.
    # Products need ~20 trades/month for full activity score.
    # Below that, production may sit unsold waiting for a buyer.
    # Selling at Jita bypasses this: Jita buy orders absorb PI output freely.
    if sell_at_jita:
        activity_factor = 1.0
    else:
        activity_factor = min(local_order_count / ORDERS_FOR_FULL_ACTIVITY,
                              1.0)
    vc["activity_factor"] = activity_factor
    vc["adjusted_net_isk_hr"] = vc["net_isk_hr"] * activity_factor

    # Jita ISK/hr (using Jita VWAP — you'd sell over time there)
    haul_model = cfg["haul"]
    jita_round_trip_sec = (jita_jumps * 2 * haul_model["sec_per_jump"]
                           + 2 * haul_model["sec_per_station"])
    jita_round_trip_min = jita_round_trip_sec / 60
    vc["jita_gross_isk_hr"] = units_hr * jita_vwap
    vc["jita_haul_min"] = jita_round_trip_min

    # Haul time estimate for per-chain display (est.)
    if matrix and home_id and system_ids:
        vc["haul_minutes_per_day"] = _estimate_chain_haul_minutes(
            vc, matrix, home_id, system_ids, cfg["haul"])
    else:
        vc["haul_minutes_per_day"] = _compute_haul_time(vc, cfg)

    # ── Flags ──

    # Local-market health flags only matter when we intend to sell locally.
    # Selling at Jita moots them (Jita has a live buy order or it doesn't).
    if sell_at_jita:
        if jita_buy <= 0 and jita_vwap <= 100:
            vc["flags"].append("NO JITA MARKET")
        else:
            vc["flags"].extend(_depth_flags(jita_fill, "SELL"))
    else:
        # NO LOCAL BUYER: no buy orders within jump range
        if local_buy == 0:
            if local_vwap > 100:
                vc["flags"].append("NO LOCAL BUYER")
            elif jita_vwap > 100:
                vc["flags"].append("NO LOCAL MARKET")

        # SHALLOW BUY: real buy order depth covers less than 7 days of output
        if real_buy_days < SHALLOW_BUY_THRESHOLD_DAYS and units_per_day > 0:
            if real_buy_days > 0:
                vc["flags"].append(f"SHALLOW BUY ({real_buy_days:.0f}d depth)")
            elif local_buy > MIN_REAL_ORDER_PRICE:
                vc["flags"].append("SHALLOW BUY (<1d depth)")

        # Liquidity from trade frequency
        if local_order_count < LOW_ACTIVITY_ORDER_THRESHOLD:
            vc["flags"].append(f"LOW ACTIVITY ({local_order_count} trades/30d)")

    if vc["haul_minutes_per_day"] > cfg["max_haul_minutes"]:
        vc["flags"].append("HAUL OVER BUDGET")

    # Output-buffer check: the launchpad on the planet that makes the final
    # product has to hold everything produced between two visits.
    buffer_days = min(cfg.get("on_planet_buffer_days", 1.0), haul_days)
    if units_hr > 0 and chain["volume"] > 0:
        fill_hours = FACILITY_M3["launchpad"] / (units_hr * chain["volume"])
        vc["output_fill_hours"] = fill_hours
        need_hours = buffer_days * 24
        if fill_hours < need_hours:
            extra = _storage_for(units_hr * chain["volume"] * need_hours)
            vc["flags"].append(
                f"LAUNCHPAD FILLS IN {fill_hours:.0f}H "
                f"(needs {extra} storage for {buffer_days:g}d)")

    # Jita spread: compare in-range buy vs Jita VWAP (only informative when
    # selling locally — it's the "you could do better at Jita" nudge).
    if not sell_at_jita and local_buy > 10 and jita_vwap > local_buy:
        spread_pct = (jita_vwap - local_buy) / local_buy * 100
        if spread_pct > 30:
            vc["flags"].append(f"JITA +{spread_pct:.0f}%")

    # ── Build sheet helpers ──
    vc["bottleneck"] = _identify_bottleneck(vc)
    vc["sell_recommendation"] = _sell_recommendation(vc)


def _compute_chain_tax(vc, pi_types, cfg, planet_taxes=None):
    """Compute total POCO tax per unit of output product.

    Uses the fixed PI estimated prices (PI_TAX_BASE) and correct EVE mechanics:
      Export = estimated_price × tax_rate          (1.0× multiplier)
      Import = estimated_price × 0.5 × tax_rate   (0.5× multiplier)

    Per-planet tax rates from planet_taxes where available,
    falling back to cfg["tax_rate"] as default.
    """
    if planet_taxes is None:
        planet_taxes = {}
    default_rate = cfg["tax_rate"]

    def _planet_rate(system, ptype_full):
        if not system or not ptype_full or not planet_taxes:
            return default_rate
        parts = ptype_full.rsplit(" ", 1)
        base_ptype = parts[0]
        instance = parts[1] if len(parts) > 1 and len(parts[1]) <= 2 else "A"
        return planet_taxes.get(f"{system}.{base_ptype}.{instance}", default_rate)

    layout_type = vc.get("layout_type", "")
    chain = vc["chain"]
    output_tier = chain["tier"]
    planets = vc.get("planets_used", [])

    if layout_type == "p1_extractor":
        # 1 export: P1 leaves the planet
        rate = _planet_rate(planets[0]["system"], planets[0]["type"]) if planets else default_rate
        return PI_TAX_BASE["P1"] * rate

    elif layout_type == "p2_selfcontained":
        # 1 export: P2 leaves the planet (P0->P1->P2 all on-planet, no intermediates)
        rate = _planet_rate(planets[0]["system"], planets[0]["type"]) if planets else default_rate
        return PI_TAX_BASE["P2"] * rate

    elif layout_type == "p2_factory":
        # Per P2 output unit:
        #   - Export P1 from each extractor planet (1.0×)
        #   - Import P1 to factory planet (0.5×)
        #   - Export P2 from factory planet (1.0×)
        total_tax = 0
        fac_planet = planets[-1] if planets else {}
        fac_rate = _planet_rate(fac_planet.get("system"), fac_planet.get("type"))
        output_qty = chain["schematic"]["output"]["quantity"]
        for i, inp in enumerate(chain["inputs"]):
            p1_per_p2 = inp["quantity"] / output_qty
            ext_planet = planets[i] if i < len(planets) - 1 else {}
            ext_rate = _planet_rate(ext_planet.get("system"), ext_planet.get("type"))
            total_tax += p1_per_p2 * PI_TAX_BASE["P1"] * ext_rate        # P1 export
            total_tax += p1_per_p2 * PI_TAX_BASE["P1"] * 0.5 * fac_rate  # P1 import
        total_tax += PI_TAX_BASE["P2"] * fac_rate  # P2 export
        return total_tax

    elif layout_type == "p3_multi":
        # Per P3 output unit:
        #   - Export P1 from each extractor (1.0×)
        #   - Import P1 to factory (0.5×)
        #   - P1->P2->P3 processing on factory planet (no POCO tax)
        #   - Export P3 from factory (1.0×)
        total_tax = 0
        extractors = [p for p in planets if p.get("layout", {}).get("role") == "extractor"]
        factory = [p for p in planets if p.get("layout", {}).get("role") == "factory"]
        fac_planet = factory[0] if factory else (planets[-1] if planets else {})
        fac_rate = _planet_rate(fac_planet.get("system"), fac_planet.get("type"))
        p3_units_hr = vc.get("units_hr", 0)

        # P1 transitions: use actual production rates to get P1 per P3 unit
        for ext_p in extractors:
            ext_rate = _planet_rate(ext_p.get("system"), ext_p.get("type"))
            p1_hr = (ext_p.get("layout", {}).get("units_hr", 0)
                     or ext_p.get("p1_output_hr", 0))
            p1_per_p3 = p1_hr / p3_units_hr if p3_units_hr > 0 else 0
            total_tax += p1_per_p3 * PI_TAX_BASE["P1"] * ext_rate        # P1 export
            total_tax += p1_per_p3 * PI_TAX_BASE["P1"] * 0.5 * fac_rate  # P1 import

        total_tax += PI_TAX_BASE["P3"] * fac_rate  # P3 export
        return total_tax

    elif layout_type == "p4_full":
        # Rough estimate — P4 chains don't have detailed planet assignments yet.
        # Per P4 output unit:
        #   - P1 export + import for each P0 line
        #   - P3 inputs export + import to final factory
        #   - P4 export
        total_tax = 0
        n_p0 = len(chain.get("p0_inputs", []))
        # Each P0 line produces P1 that gets exported + imported
        # Rough: ~40 P1/hr per line, P4 = 1/hr → ~40 P1 per P4 unit per line
        p1_per_p4_per_line = 40
        total_tax += n_p0 * p1_per_p4_per_line * PI_TAX_BASE["P1"] * default_rate       # P1 export
        total_tax += n_p0 * p1_per_p4_per_line * PI_TAX_BASE["P1"] * 0.5 * default_rate # P1 import
        # P3 inputs to P4 factory (export + import)
        output_qty = chain["schematic"]["output"]["quantity"]
        for inp in chain["inputs"]:
            per_unit = inp["quantity"] / output_qty
            inp_tier = inp.get("tier", "P3")
            base = PI_TAX_BASE.get(inp_tier, PI_TAX_BASE["P3"])
            total_tax += per_unit * base * default_rate        # export
            total_tax += per_unit * base * 0.5 * default_rate  # import
        total_tax += PI_TAX_BASE["P4"] * default_rate  # P4 export
        return total_tax

    return 0


def _compute_haul_time(vc, cfg):
    """Compute daily haul time in minutes for a chain's layout."""
    haul = cfg["haul"]
    planets_used = vc.get("planets_used", [])
    layout_type = vc.get("layout_type", "")
    volume_hr = vc.get("volume_hr", 0)
    hauler_m3 = cfg["hauler_m3"]

    if layout_type == "p1_extractor":
        # Simple: collect from 1 planet, deliver to station
        # 1 planet stop + 1 station stop per trip
        daily_volume = volume_hr * 24
        trips_per_day = max(1, math.ceil(daily_volume / hauler_m3))
        time_per_trip = haul["sec_per_planet"] + haul["sec_per_station"]
        total_sec = trips_per_day * time_per_trip + haul["daily_overhead"]
        return total_sec / 60

    elif layout_type in ("p2_selfcontained",):
        daily_volume = volume_hr * 24
        trips_per_day = max(1, math.ceil(daily_volume / hauler_m3))
        time_per_trip = haul["sec_per_planet"] + haul["sec_per_station"]
        total_sec = trips_per_day * time_per_trip + haul["daily_overhead"]
        return total_sec / 60

    elif layout_type == "p2_factory":
        # P1 transfer: collect from extraction planets, deliver to factory
        # Then collect P2 from factory, deliver to station
        num_extraction = len([p for p in planets_used if p.get("layout", {}).get("role") == "extractor"])
        if num_extraction == 0:
            num_extraction = len(planets_used) - 1

        # P1 transfer trips
        p1_volume_hr = 0
        for p in planets_used:
            if p.get("layout", {}).get("role") == "extractor":
                p1_volume_hr += p["layout"].get("volume_hr", 0)
        daily_p1_volume = p1_volume_hr * 24
        p1_trips = max(1, math.ceil(daily_p1_volume / hauler_m3))
        p1_time = p1_trips * (num_extraction * haul["sec_per_planet"] + haul["sec_per_planet"])

        # P2 export trips
        daily_p2_volume = volume_hr * 24
        p2_trips = max(1, math.ceil(daily_p2_volume / hauler_m3))
        p2_time = p2_trips * (haul["sec_per_planet"] + haul["sec_per_station"])

        total_sec = p1_time + p2_time + haul["daily_overhead"]
        return total_sec / 60

    elif layout_type == "p3_multi":
        # Rough estimate based on planet count
        planet_count = vc.get("planet_count", 5)
        daily_volume = volume_hr * 24
        trips = max(2, math.ceil(daily_volume / hauler_m3) + planet_count - 1)
        time_per_trip = (planet_count * haul["sec_per_planet"]
                         + haul["sec_per_station"])
        total_sec = trips * time_per_trip + haul["daily_overhead"]
        return total_sec / 60

    elif layout_type == "p4_full":
        planet_count = vc.get("planet_count", 10)
        daily_volume = volume_hr * 24
        trips = max(2, math.ceil(daily_volume / hauler_m3) + planet_count - 1)
        time_per_trip = (planet_count * haul["sec_per_planet"]
                         + haul["sec_per_station"])
        total_sec = trips * time_per_trip + haul["daily_overhead"]
        return total_sec / 60

    return 0


def _estimate_chain_haul_minutes(vc, matrix, home_id, system_ids, haul_cfg):
    """Estimate haul time for a single chain using actual jump distances.

    Rough per-chain estimate for display in ranking tables (marked 'est.').
    NOT the layout-level route time (which uses TSP).
    """
    planets = vc.get("planets_used", [])
    if not planets:
        pc = vc.get("planet_count", 1) or 1
        return (pc * haul_cfg["sec_per_planet"] + haul_cfg["daily_overhead"]) / 60

    # Collect unique systems
    systems = set()
    for p in planets:
        sys_name = p.get("system", "")
        if sys_name:
            systems.add(sys_name)

    # Simple estimate: sum round-trip jumps to each unique system
    total_jumps = 0
    for sys_name in systems:
        sid = system_ids.get(sys_name)
        if sid and sid != home_id:
            jumps = matrix.get((home_id, sid), -1)
            if jumps > 0:
                total_jumps += jumps * 2

    planet_stops = len(planets)
    time_sec = (total_jumps * haul_cfg["sec_per_jump"]
                + planet_stops * haul_cfg["sec_per_planet"]
                + haul_cfg["sec_per_station"]
                + haul_cfg["daily_overhead"])
    return time_sec / 60


def _identify_bottleneck(vc):
    """Identify the throughput bottleneck for a chain. Returns description string."""
    layout_type = vc.get("layout_type", "")
    planets = vc.get("planets_used", [])

    if layout_type == "p1_extractor" and planets:
        layout = planets[0].get("layout", {})
        fac = layout.get("facilities", {})
        p0_hr = layout.get("p0_consumed_hr", 0)
        num_bifs = fac.get("bif", 0)
        units_hr = layout.get("units_hr", 0)
        if num_bifs > 0 and p0_hr < num_bifs * 6000 * 0.95:
            pct = p0_hr / max(num_bifs * 6000, 1) * 100
            return (f"Extraction: {p0_hr:,.0f} P0/hr feeds {num_bifs} BIF "
                    f"at {pct:.0f}% -> {units_hr:.0f} P1/hr")
        return f"PG/CPU: fits {num_bifs} BIF at full capacity -> {units_hr:.0f} P1/hr"

    elif layout_type == "p2_selfcontained" and planets:
        layout = planets[0].get("layout", {})
        fac = layout.get("facilities", {})
        p0_rates = layout.get("p0_consumed_hr", [])
        num_aifs = fac.get("aif", 0)
        units_hr = layout.get("units_hr", 0)
        if isinstance(p0_rates, list) and len(p0_rates) >= 2:
            slow = min(p0_rates)
            fast = max(p0_rates)
            if slow < fast * 0.9:
                return (f"Slower P0 line at {slow:,.0f}/hr limits "
                        f"{num_aifs} AIF -> {units_hr:.1f} P2/hr")
        return f"{num_aifs} AIF on single planet -> {units_hr:.1f} P2/hr"

    elif layout_type == "p2_factory":
        extractors = [p for p in planets
                      if p.get("layout", {}).get("role") == "extractor"]
        factory = [p for p in planets
                   if p.get("layout", {}).get("role") == "factory"]
        if extractors:
            p1_rates = [p.get("p1_output_hr",
                              p.get("layout", {}).get("units_hr", 0))
                        for p in extractors]
            min_p1 = min(p1_rates) if p1_rates else 0
            if factory:
                fac_aifs = factory[0].get("layout", {}).get(
                    "facilities", {}).get("aif", 0)
                aif_need = fac_aifs * 40
                if min_p1 < aif_need:
                    return (f"Slowest extractor: {min_p1:.0f} P1/hr "
                            f"(factory {fac_aifs} AIFs need {aif_need}/hr)")
                return f"Factory: {fac_aifs} AIFs (fully supplied)"
        return "Supply chain"

    elif layout_type == "p3_multi":
        extractors = [p for p in planets
                      if p.get("layout", {}).get("role") == "extractor"]
        if extractors:
            p1_rates = [p.get("p1_output_hr", 0) for p in extractors]
            if p1_rates and min(p1_rates) > 0:
                min_rate = min(p1_rates)
                min_planet = extractors[p1_rates.index(min_rate)]
                return (f"Slowest P1 line: {min_planet.get('role', '')} "
                        f"at {min_rate:.0f}/hr")
        return "Multi-planet supply chain"

    elif layout_type == "p4_full":
        pc = vc.get("planet_count", 0)
        mp = 5  # typical max
        if pc > mp:
            return f"Planet count: {pc} needed (have {mp})"
        return "Full P4 supply chain"

    return ""


def _sell_recommendation(vc):
    """Generate a sell location recommendation string."""
    local_buy = vc.get("local_buy_price", 0)
    buyer_sys = vc.get("local_buyer_system", "")
    buyer_jumps = vc.get("local_buyer_jumps", 0)
    depth_days = vc.get("local_real_buy_days", 0)
    jita_buy = vc.get("jita_buy_price", 0)
    jita_vwap = vc.get("jita_vwap", 0)

    # Jita-dump mode: pricing is pinned to Jita, so the recommendation is
    # simply "haul to Jita and sell to buy orders".
    if vc.get("sell_at_jita"):
        if jita_buy > 0:
            return f"Haul to Jita, sell to buy orders at {jita_buy:,.0f} ISK"
        if jita_vwap > 0:
            return (f"Haul to Jita (~{jita_vwap:,.0f} ISK VWAP; "
                    f"no live buy order)")
        return "No Jita market"

    if local_buy <= 0 and jita_buy > 0:
        return f"Jita buy orders at {jita_buy:,.0f} ISK (no local buyers)"
    if local_buy <= 0:
        return "No buyers found in range"

    # Compare local vs Jita
    if jita_buy > local_buy * 1.30 and jita_buy > 100:
        pct = (jita_buy - local_buy) / local_buy * 100
        return (f"Consider Jita at {jita_buy:,.0f} ISK "
                f"(+{pct:.0f}% vs {buyer_sys} {buyer_jumps}j)")

    depth_str = ""
    if depth_days > 0:
        depth_str = f", {depth_days:.0f}d order depth"
    return f"Sell at {buyer_sys} ({buyer_jumps}j) -- {local_buy:,.0f} ISK{depth_str}"


# ── Ranking & allocation ──────────────────────────────────────

def rank_chains(viable_chains):
    """Sort chains by activity-adjusted net ISK/hr descending."""
    return sorted(viable_chains,
                  key=lambda c: c.get("adjusted_net_isk_hr", 0), reverse=True)


def _run_greedy_allocation(candidates, planet_inv, max_planets, max_haul_minutes):
    """Run one greedy allocation pass over pre-sorted candidates.

    candidates: [(sort_key, planet_count, vc), ...] already sorted descending.
    Returns: (allocated_list, total_net_isk_hr)
    """
    allocated = []
    remaining = copy.deepcopy(planet_inv)
    planets_used = 0
    total_haul = 0
    total_net = 0

    for _, pc, vc in candidates:
        if planets_used >= max_planets:
            break
        if planets_used + pc > max_planets:
            continue

        chain_haul = vc.get("haul_minutes_per_day", 0)
        if max_haul_minutes and total_haul + chain_haul > max_haul_minutes * 1.5:
            continue

        needed = {}
        for p in vc.get("planets_used", []):
            ptype_full = p.get("type", "")
            system = p.get("system")
            # Strip instance letter: "Barren A" → "Barren"
            base_ptype = ptype_full.rsplit(" ", 1)[0] if " " in ptype_full else ptype_full
            if base_ptype and base_ptype != "Any" and system:
                key = (system, base_ptype)
                needed[key] = needed.get(key, 0) + 1

        can_allocate = True
        for (sys, base_ptype), count in needed.items():
            avail = remaining.get(sys, {}).get(base_ptype, 0)
            if avail < count:
                can_allocate = False
                break

        if not can_allocate:
            continue

        for (sys, base_ptype), count in needed.items():
            remaining[sys][base_ptype] -= count

        allocated.append(vc)
        planets_used += pc
        total_haul += chain_haul
        total_net += vc.get("adjusted_net_isk_hr", 0)

    return allocated, total_net


def allocate_5_planets(ranked_chains, planet_inv, max_planets=5, max_haul_minutes=None):
    """Generate top 3 layout alternatives by varying allocation strategy.

    Returns: list of layouts, each = {"allocated": [...], "total_net": float,
             "strategy": str}. Sorted by total_net descending, deduplicated.
    """
    viable = [vc for vc in ranked_chains if vc.get("viable")]

    # Build candidate lists with different sort keys
    def _make_candidates(sort_fn):
        cands = []
        for vc in viable:
            pc = vc.get("planet_count", len(vc.get("planets_used", []))) or 1
            key = sort_fn(vc, pc)
            cands.append((key, pc, vc))
        cands.sort(key=lambda x: x[0], reverse=True)
        return cands

    strategies = [
        ("Per-slot adjusted ISK/hr",
         lambda vc, pc: vc.get("adjusted_net_isk_hr", 0) / pc),
        ("Total adjusted ISK/hr (favours factories)",
         lambda vc, pc: vc.get("adjusted_net_isk_hr", 0)),
        ("Self-contained only",
         lambda vc, pc: vc.get("adjusted_net_isk_hr", 0) / pc if pc == 1 else -1),
    ]

    layouts = []
    seen_signatures = set()

    for strategy_name, sort_fn in strategies:
        cands = _make_candidates(sort_fn)
        alloc, total = _run_greedy_allocation(cands, planet_inv,
                                               max_planets, max_haul_minutes)
        # Dedup by set of chain output names
        sig = frozenset(vc["chain"]["output_name"] for vc in alloc)
        if sig in seen_signatures:
            continue
        seen_signatures.add(sig)
        layouts.append({
            "allocated": alloc,
            "total_net": total,
            "strategy": strategy_name,
        })

    # Sort by total net descending
    layouts.sort(key=lambda l: l["total_net"], reverse=True)
    return layouts[:3]


# ── TSP solver + route cost ───────────────────────────────────

def _solve_tsp(waypoints, home_id, matrix, end_id=None, precedence=None):
    """Brute-force TSP for small waypoint sets (max ~6 = 720 perms).

    Computes home -> w1 -> ... -> wN [-> end_id] -> home, returns shortest.
    end_id (the sell hub) is pinned as the final stop before home, since
    goods must be collected before they can be sold.
    precedence: set of (before_id, after_id) pairs — e.g. an extractor
    system must be visited before the factory system its P1 feeds, since
    the hauler carries the P1 with it. Pairs whose 'before' member is not
    a free waypoint (home, or the pinned sell hub) are unenforceable and
    skipped; pairs whose 'after' member is pinned last are trivially met.
    Returns: (ordered_route: list[int], total_jumps: int)
    """
    def leg(a, b):
        return 0 if a == b else matrix.get((a, b), -1)

    pinned = [end_id] if end_id is not None and end_id != home_id else []
    waypoints = [w for w in waypoints if w not in pinned]

    if not waypoints and not pinned:
        return [], 0

    reachable = [w for w in waypoints if leg(home_id, w) >= 0]
    if not reachable and not pinned:
        return [], -1

    free = set(reachable)
    order_pairs = [(a, b) for a, b in (precedence or set())
                   if a in free and b in free and a != b]

    best_route = None
    best_dist = float('inf')

    perms = itertools.permutations(reachable) if reachable else iter([()])
    for perm in perms:
        if order_pairs:
            pos = {sid: i for i, sid in enumerate(perm)}
            if any(pos[a] > pos[b] for a, b in order_pairs):
                continue
        nodes = [home_id] + list(perm) + pinned + [home_id]
        total = 0
        valid = True
        for a, b in zip(nodes, nodes[1:]):
            d = leg(a, b)
            if d < 0:
                valid = False
                break
            total += d
        if not valid:
            continue
        if total < best_dist:
            best_dist = total
            best_route = list(perm) + pinned

    if best_route is None:
        if order_pairs:
            # Contradictory precedence (e.g. two chains feeding factories
            # in each other's systems) — fall back to pure shortest tour.
            return _solve_tsp(waypoints, home_id, matrix, end_id=end_id)
        return list(reachable) + pinned, -1
    return best_route, best_dist


def _compute_route_cost(system_set, home_id, sell_system_id, system_ids,
                        matrix, haul_cfg, planet_stops, precedence=None):
    """Compute route cost for a layout's daily circuit.

    Route: home -> extraction/factory systems (TSP) -> sell hub -> home.
    Sell system is pinned as the last stop before returning home (goods
    must be collected before they can be sold).
    precedence: set of (extractor_system, factory_system) name pairs —
    the factory stop must come after the extractors feeding it, since
    the P1 is dropped off on the same circuit it is collected.
    """
    id_to_name = {v: k for k, v in system_ids.items()}

    waypoint_ids = set()
    for sys_name in system_set:
        sid = system_ids.get(sys_name)
        if sid and sid != home_id:
            waypoint_ids.add(sid)

    end_id = None
    if sell_system_id and sell_system_id != home_id:
        end_id = sell_system_id
        # Ensure matrix has entries for sell system
        all_nodes = [home_id] + list(waypoint_ids)
        for nid in all_nodes:
            if (sell_system_id, nid) not in matrix and nid != sell_system_id:
                jumps = esi.get_jump_count(sell_system_id, nid,
                                           avoid=_AVOID_IDS)
                matrix[(sell_system_id, nid)] = jumps
                matrix[(nid, sell_system_id)] = jumps

    prec_ids = set()
    for before, after in (precedence or set()):
        a, b = system_ids.get(before), system_ids.get(after)
        if a and b and a != b:
            prec_ids.add((a, b))

    waypoints = list(waypoint_ids)
    route, total_jumps = _solve_tsp(waypoints, home_id, matrix,
                                    end_id=end_id, precedence=prec_ids)

    if total_jumps < 0:
        fallback_nodes = set(waypoints) | ({end_id} if end_id else set())
        total_jumps = sum(max(0, matrix.get((home_id, w), 5))
                          for w in fallback_nodes) * 2

    route_seconds = (total_jumps * haul_cfg["sec_per_jump"]
                     + planet_stops * haul_cfg["sec_per_planet"]
                     + haul_cfg["sec_per_station"]
                     + haul_cfg["daily_overhead"])

    # Resolve any IDs not in system_ids (e.g. sell system from market data)
    for sid in route:
        if sid not in id_to_name:
            id_to_name[sid] = esi.resolve_system_name(sid)
    if sell_system_id and sell_system_id not in id_to_name:
        id_to_name[sell_system_id] = esi.resolve_system_name(sell_system_id)

    systems_ordered = [id_to_name.get(sid, f"System {sid}") for sid in route]
    sell_name = id_to_name.get(sell_system_id, "")
    sell_jumps = matrix.get((home_id, sell_system_id), 0) if sell_system_id else 0

    return {
        "systems_ordered": systems_ordered,
        "tour_jumps": total_jumps,
        "planet_stops": planet_stops,
        "sell_system": sell_name,
        "sell_jumps": max(0, sell_jumps) if sell_jumps else 0,
        "route_seconds": route_seconds,
        "route_minutes": route_seconds / 60,
    }


# ── System-first allocator ───────────────────────────────────

def _best_chain_combo(eligible, max_planets, pool):
    """Find best combination of chains fitting in max_planets with type constraints.

    Uses branch-and-bound with suffix-sum pruning.
    """
    # Sort by adjusted ISK/hr descending for better pruning
    eligible.sort(key=lambda vc: vc.get("adjusted_net_isk_hr", 0), reverse=True)

    # Precompute suffix sums for upper-bound pruning
    n = len(eligible)
    suffix_sum = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix_sum[i] = suffix_sum[i + 1] + eligible[i].get("adjusted_net_isk_hr", 0)

    best = [0, []]  # [total_net, chains]

    def _check_types(selected):
        agg = {}
        any_count = 0
        for vc in selected:
            for t, c in vc["_alloc_types"].items():
                if t == "Any":
                    any_count += c
                else:
                    agg[t] = agg.get(t, 0) + c
        for t, c in agg.items():
            if pool.get(t, 0) < c:
                return False
        used_specific = sum(agg.values())
        total_pool = sum(pool.values())
        return any_count <= total_pool - used_specific

    def search(idx, remaining, total_net, selected):
        if total_net > best[0] and selected:
            if _check_types(selected):
                best[0] = total_net
                best[1] = list(selected)
        if remaining <= 0 or idx >= n:
            return
        if total_net + suffix_sum[idx] <= best[0]:
            return
        for i in range(idx, n):
            vc = eligible[i]
            pc = vc["_alloc_pc"]
            if pc > remaining:
                continue
            if total_net + suffix_sum[i] <= best[0]:
                break
            selected.append(vc)
            search(i + 1, remaining - pc,
                   total_net + vc.get("adjusted_net_isk_hr", 0), selected)
            selected.pop()

    search(0, max_planets, 0, [])
    return best[0], best[1]


def _determine_sell_system(allocated, market_prices, system_ids):
    """Determine sell system for a layout based on dominant product by volume."""
    best_vol = 0
    sell_sys_name = ""
    for vc in allocated:
        vol = vc.get("volume_hr", 0) * 24
        if vol > best_vol:
            best_vol = vol
            sell_sys_name = vc.get("local_buyer_system", "")
    if sell_sys_name:
        return system_ids.get(sell_sys_name) or esi.search_system_id(sell_sys_name)
    return None


def _compute_trips_per_day(allocated, hauler_m3):
    """Compute trips/day based on peak loaded leg volume."""
    peak_daily_m3 = 0
    for vc in allocated:
        input_vol = 0
        output_vol = vc.get("volume_hr", 0) * 24
        for p in vc.get("planets_used", []):
            layout = p.get("layout", {})
            if layout.get("role") == "extractor":
                input_vol += layout.get("volume_hr", 0) * 24
        peak_daily_m3 += max(input_vol, output_vol)
    if hauler_m3 <= 0:
        return 1
    return max(1, math.ceil(peak_daily_m3 / hauler_m3))


TOP_SYSTEMS_PER_P0 = 8     # allocator considers systems in the top-K of any resource
LOW_TAX_FACTORY_SYSTEMS = 5  # plus the N cheapest-POCO systems (factory hosts)
FINALIST_LAYOUTS = 10      # candidates that get instance deconfliction


def _interesting_systems(ctx, home_name):
    """Systems worth enumerating in the allocator.

    A system matters if it is in the top-K extraction candidates for any
    P0 resource, is among the cheapest-POCO systems (factory hosting), or
    is home. Everything else only adds pool filler and combinatorial cost.
    """
    inv = ctx["inv"]
    keep = set()
    if home_name in inv:
        keep.add(home_name)
    for cands in ctx["p0_candidates"].values():
        seen = []
        for _rate, _tag, system, _ptype, _inst in cands:
            if system not in seen:
                seen.append(system)
                if len(seen) >= TOP_SYSTEMS_PER_P0:
                    break
        keep.update(seen)
    taxes = ctx["planet_taxes"]
    default_rate = ctx["cfg"]["tax_rate"]
    min_tax = {}
    for system in inv:
        prefix = system + "."
        rates = [v for k, v in taxes.items() if k.startswith(prefix)]
        min_tax[system] = min(rates) if rates else default_rate
    for system in sorted(min_tax, key=lambda s: min_tax[s])[:LOW_TAX_FACTORY_SYSTEMS]:
        keep.add(system)
    return sorted(s for s in keep if s in inv)


def _cached_route(actual_systems, sell_id, planet_stops, home_id, system_ids,
                  matrix, haul_cfg, cache, precedence=None):
    """Route cost with TSP results cached by (system set, sell hub, precedence)."""
    precedence = frozenset(precedence or set())
    rkey = (frozenset(actual_systems), sell_id, precedence)
    base = cache.get(rkey)
    if base is None:
        base = _compute_route_cost(actual_systems, home_id, sell_id,
                                   system_ids, matrix, haul_cfg, 0,
                                   precedence=precedence)
        cache[rkey] = base
    route = dict(base)
    route_seconds = (route["tour_jumps"] * haul_cfg["sec_per_jump"]
                     + planet_stops * haul_cfg["sec_per_planet"]
                     + haul_cfg["sec_per_station"]
                     + haul_cfg["daily_overhead"])
    route["planet_stops"] = planet_stops
    route["route_seconds"] = route_seconds
    route["route_minutes"] = route_seconds / 60
    return route


def _layout_systems_and_stops(allocated):
    """Unique systems visited, total planet stops, and visit-order
    precedence pairs for a chain set.

    Precedence: for each chain, the factory planet's system must be
    visited after every system hosting one of that chain's extractors —
    the hauler collects P1 and drops it at the factory on the same
    circuit (picking up the previous batch's output there).
    """
    actual_systems = set()
    planet_stops = 0
    precedence = set()
    for vc in allocated:
        planets = vc.get("planets_used", [])
        planet_stops += len(planets) or vc.get("planet_count", 1) or 1
        extr_sys, fac_sys = set(), set()
        for p in planets:
            s = p.get("system", "")
            if s:
                actual_systems.add(s)
                (fac_sys if p.get("is_factory") else extr_sys).add(s)
        for f in fac_sys:
            for e in extr_sys:
                if e != f:
                    precedence.add((e, f))
    return actual_systems, planet_stops, precedence


def _finalize_layout(cand, ctx, ectx, matrix, home_id, system_ids, cfg,
                     market_prices, route_cache):
    """Deconflict planet instances across a layout's chains and build the
    final layout dict.

    Chains are re-resolved best-first with already-claimed instances
    excluded, so no two colonies land on the same physical planet; yields
    are recomputed with the planets each chain actually gets.
    """
    combo_set = cand["combo"]
    used = set()
    allocated = []
    for vc in sorted(cand["allocated"],
                     key=lambda v: -v.get("adjusted_net_isk_hr", 0)):
        if vc["chain"]["tier"] == "P4" or not vc.get("planets_used"):
            allocated.append(copy.deepcopy(vc))
            continue
        new_vc = _analyse_chain(vc["chain"], ctx, combo_set,
                                exclude=frozenset(used))
        if not new_vc or not new_vc.get("viable"):
            continue  # no conflict-free planets left for this chain
        _compute_economics_single(new_vc, ectx)
        new_vc = copy.deepcopy(new_vc)
        for p in new_vc.get("planets_used", []):
            key = _planet_instance_key(p)
            if key:
                used.add(key)
        allocated.append(new_vc)
    if not allocated:
        return None

    total_net = sum(vc.get("adjusted_net_isk_hr", 0) for vc in allocated)
    sell_id = _determine_sell_system(allocated, market_prices, system_ids)
    actual_systems, planet_stops, precedence = _layout_systems_and_stops(allocated)
    route = _cached_route(actual_systems, sell_id, planet_stops, home_id,
                          system_ids, matrix, cfg["haul"], route_cache,
                          precedence=precedence)
    trips = _compute_trips_per_day(allocated, cfg["hauler_m3"])
    daily_haul = route["route_minutes"] * trips
    route["trips_per_day"] = trips
    route["daily_haul_minutes"] = daily_haul
    route["isk_per_haul_min"] = (total_net * 24 / daily_haul
                                 if daily_haul > 0 else 0)

    label = " + ".join(sorted(actual_systems))
    if len(actual_systems) > 1:
        label += f" ({len(actual_systems)} systems)"
    return {"strategy": label, "total_net": total_net,
            "allocated": allocated, "route": route}


def allocate_system_first(ranked_chains, planet_inv, matrix, home_id,
                          system_ids, cfg, market_prices, ctx, ectx):
    """System-first allocator with combo-local planet selection.

    Enumerates subsets of 1..4 interesting systems. Within each subset,
    chains are re-analysed using only that subset's planets — so a nearby
    almost-as-good planet competes against the global best instead of the
    chain being pinned to one distant system. The best chain combination
    per subset comes from a branch-and-bound knapsack; the TSP route
    (sell hub pinned last) gives daily haul minutes; candidates within
    the haul budget are ranked by total net ISK/hr. Top candidates then
    get instance deconfliction (no two colonies on one physical planet)
    before final ranking. Returns the top 3 layouts.
    """
    max_planets = cfg["max_planets"]
    max_haul_minutes = cfg["max_haul_minutes"]
    haul_cfg = cfg["haul"]
    inv = ctx["inv"]
    home_name = cfg["home_system"]

    # P4 chains have no per-system planet assignments — pass through
    passthrough = [vc for vc in ranked_chains
                   if vc.get("viable") and vc["chain"]["tier"] == "P4"
                   and (vc.get("planet_count") or 99) <= max_planets]
    for vc in passthrough:
        _attach_alloc_fields(vc)

    p123 = [c for c in ctx["chains"].values()
            if c["tier"] in ("P1", "P2", "P3")]

    interesting = _interesting_systems(ctx, home_name)
    skipped = len(inv) - len(interesting)
    if skipped > 0:
        print(f"  PI Dossier: allocator over {len(interesting)} systems "
              f"(top {TOP_SYSTEMS_PER_P0} per resource + "
              f"{LOW_TAX_FACTORY_SYSTEMS} low-tax; {skipped} others add "
              f"nothing a kept system doesn't)")

    sec_per_jump = haul_cfg["sec_per_jump"]
    candidates = []
    knap_cache = {}
    route_cache = {}
    combos_seen = 0
    t0 = time.time()

    for subset_size in range(1, min(len(interesting), 4) + 1):
        for combo in itertools.combinations(interesting, subset_size):
            # Route lower bound: the circuit must at least round-trip to
            # the farthest member system — prune before any analysis.
            dists = []
            unreachable = False
            for s in combo:
                sid = system_ids.get(s)
                d = 0
                if sid and sid != home_id:
                    d = matrix.get((home_id, sid), -1)
                if d < 0:
                    unreachable = True
                    break
                dists.append(d)
            if unreachable:
                continue
            lb_minutes = (2 * max(dists) * sec_per_jump
                          + haul_cfg["sec_per_station"]) / 60
            if max_haul_minutes and lb_minutes > max_haul_minutes:
                continue
            combos_seen += 1

            combo_set = frozenset(combo)
            pool = {}
            for s in combo:
                for ptype, count in inv.get(s, {}).items():
                    pool[ptype] = pool.get(ptype, 0) + count
            total_pool = sum(pool.values())
            if total_pool < 1:
                continue

            eligible = []
            for chain in p123:
                vc = _analyse_chain(chain, ctx, combo_set)
                if not vc or not vc.get("viable"):
                    continue
                _compute_economics_single(vc, ectx)
                if vc["_alloc_pc"] > max_planets:
                    continue
                if not _types_fit(vc["_alloc_types"], pool, total_pool):
                    continue
                eligible.append(vc)
            for vc in passthrough:
                if _types_fit(vc["_alloc_types"], pool, total_pool):
                    eligible.append(vc)
            if not eligible:
                continue

            ksig = (frozenset(id(vc) for vc in eligible),
                    tuple(sorted(pool.items())))
            cached = knap_cache.get(ksig)
            if cached is None:
                cached = _best_chain_combo(list(eligible), max_planets, pool)
                knap_cache[ksig] = cached
            best_net, best_combo = cached
            if not best_combo:
                continue

            sell_id = _determine_sell_system(best_combo, market_prices,
                                             system_ids)
            actual_systems, planet_stops, precedence = \
                _layout_systems_and_stops(best_combo)
            route = _cached_route(actual_systems, sell_id, planet_stops,
                                  home_id, system_ids, matrix, haul_cfg,
                                  route_cache, precedence=precedence)
            trips = _compute_trips_per_day(best_combo, cfg["hauler_m3"])
            daily_haul = route["route_minutes"] * trips
            if max_haul_minutes and daily_haul > max_haul_minutes:
                continue

            candidates.append({"combo": combo_set, "net": best_net,
                               "allocated": best_combo,
                               "daily_haul": daily_haul})

    # Best-first by pre-deconflict net; finalize the top unique chain sets
    candidates.sort(key=lambda c: (-c["net"], c["daily_haul"]))
    finals = []
    seen = set()
    for cand in candidates:
        sig = frozenset(vc["chain"]["output_name"] for vc in cand["allocated"])
        if sig in seen:
            continue
        seen.add(sig)
        layout = _finalize_layout(cand, ctx, ectx, matrix, home_id,
                                  system_ids, cfg, market_prices, route_cache)
        if layout:
            finals.append(layout)
        if len(finals) >= FINALIST_LAYOUTS:
            break

    # Re-rank after deconfliction (yields may have dropped slightly)
    if max_haul_minutes:
        finals = [l for l in finals
                  if l["route"]["daily_haul_minutes"] <= max_haul_minutes * 1.001]
    finals.sort(key=lambda l: (-l["total_net"],
                               l["route"]["daily_haul_minutes"]))
    print(f"  PI Dossier: allocator evaluated {combos_seen} system subsets "
          f"({len(candidates)} candidate layouts) in {time.time()-t0:.1f}s")
    return finals[:3]


# ── System map SVG ───────────────────────────────────────────

def _generate_system_map_svg(planet_inv, matrix, system_ids, positions,
                              home_id, layouts):
    """Generate an SVG map of systems and gate connections.

    Each recommended layout's route is drawn in its own toggleable layer
    (class pi-route-layer, data-layout=N; layer 0 visible by default) so
    the web UI can switch the map between layouts on click. Each layout
    gets two groups with the same data-layout: route lines below the
    nodes, and highlight rings / sell-hub markers above them.
    """
    if not positions or len(positions) < 2:
        return ""

    xs = [p[0] for p in positions.values()]
    zs = [p[1] for p in positions.values()]
    min_x, max_x = min(xs), max(xs)
    min_z, max_z = min(zs), max(zs)
    range_x = max_x - min_x or 1
    range_z = max_z - min_z or 1

    W, H = 900, 540
    PAD = 60

    def proj(x, z):
        px = PAD + (x - min_x) / range_x * (W - 2 * PAD)
        py = PAD + (z - min_z) / range_z * (H - 2 * PAD)
        return px, py

    planet_counts = {}
    ignored_systems = set()
    for sys_name, planets in planet_inv.items():
        planet_counts[sys_name] = sum(c for pt, c in planets.items()
                                      if not pt.startswith("_"))
        if planets.get("_ignored"):
            ignored_systems.add(sys_name)

    def node_radius(name):
        return 8 + min(planet_counts.get(name, 0), 10) * 1.5

    # Projected positions, then collision relaxation: real star coordinates
    # cluster, so push overlapping circles apart (each pair shares the
    # correction) until every node has clear space, staying as close to the
    # true position as the overlaps allow.
    NODE_GAP = 12  # minimum clearance between circle edges
    pts = {}
    for name, (x, z) in positions.items():
        px, py = proj(x, z)
        pts[name] = [px, py]
    names = list(pts)
    for _ in range(200):
        moved = False
        for i, na in enumerate(names):
            ra = node_radius(na)
            for nb in names[i + 1:]:
                min_d = ra + node_radius(nb) + NODE_GAP
                dx = pts[nb][0] - pts[na][0]
                dy = pts[nb][1] - pts[na][1]
                dist = (dx * dx + dy * dy) ** 0.5
                if dist >= min_d:
                    continue
                if dist < 1e-6:
                    dx, dy, dist = 1.0, 0.5, 1.118
                push = (min_d - dist) / 2
                pts[na][0] -= dx / dist * push
                pts[na][1] -= dy / dist * push
                pts[nb][0] += dx / dist * push
                pts[nb][1] += dy / dist * push
                moved = True
        # Keep nodes on the canvas (with room for the label underneath)
        for name in names:
            r = node_radius(name) + 6
            pts[name][0] = min(max(pts[name][0], r), W - r)
            pts[name][1] = min(max(pts[name][1], r), H - r - 18)
        if not moved:
            break

    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
           f'style="width:100%;max-width:{W}px;background:#1a1a2e;'
           f'border-radius:8px;">']

    # Arrow marker for route
    svg.append('<defs><marker id="arr" markerWidth="8" markerHeight="6" '
               'refX="8" refY="3" orient="auto"><path d="M0,0 L8,3 L0,6" '
               'fill="#4af" opacity="0.8"/></marker></defs>')

    # Gate connections — only 1-jump neighbors (actual stargates)
    drawn = set()
    for (a, b), jumps in matrix.items():
        if a >= b or (a, b) in drawn or jumps != 1:
            continue
        drawn.add((a, b))
        name_a = name_b = None
        for n, sid in system_ids.items():
            if sid == a:
                name_a = n
            if sid == b:
                name_b = n
        if not name_a or not name_b:
            continue
        if name_a not in pts or name_b not in pts:
            continue
        x1, y1 = pts[name_a]
        x2, y2 = pts[name_b]
        svg.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" '
                   f'y2="{y2:.1f}" stroke="#334" stroke-width="1"/>')

    id_to_name = {v: k for k, v in system_ids.items()}
    home_name = id_to_name.get(home_id, "")

    def layer_open(idx):
        hidden = '' if idx == 0 else ' style="display:none"'
        return f'<g class="pi-route-layer" data-layout="{idx}"{hidden}>'

    # Route lines per layout — below the nodes, with arrows and jump labels
    for idx, layout in enumerate(layouts[:3]):
        svg.append(layer_open(idx))
        ordered = layout.get("route", {}).get("systems_ordered", [])
        route_names = [home_name] + ordered + [home_name] if ordered else []
        for i in range(len(route_names) - 1):
            na, nb = route_names[i], route_names[i + 1]
            if na not in pts or nb not in pts:
                continue
            x1, y1 = pts[na]
            x2, y2 = pts[nb]
            # Shorten line so arrow doesn't overlap node
            dx, dy = x2 - x1, y2 - y1
            length = (dx * dx + dy * dy) ** 0.5
            if length > 0:
                shrink = 14 / length
                x2s = x2 - dx * shrink
                y2s = y2 - dy * shrink
            else:
                x2s, y2s = x2, y2
            svg.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" '
                       f'x2="{x2s:.1f}" y2="{y2s:.1f}" '
                       f'stroke="#4af" stroke-width="2.5" opacity="0.8" '
                       f'marker-end="url(#arr)"/>')
            # Jump label on route leg
            aid = system_ids.get(na, 0)
            bid = system_ids.get(nb, 0)
            leg_jumps = matrix.get((aid, bid), -1)
            if leg_jumps > 0:
                mx = (x1 + x2) / 2
                my = (y1 + y2) / 2
                # Offset label perpendicular to line to avoid overlap
                if length > 0:
                    ox, oy = -dy / length * 12, dx / length * 12
                else:
                    ox, oy = 0, -12
                svg.append(
                    f'<text x="{mx + ox:.1f}" y="{my + oy:.1f}" '
                    f'fill="#4af" font-size="11" text-anchor="middle" '
                    f'dominant-baseline="middle" font-family="monospace" '
                    f'opacity="0.9">{leg_jumps}j</text>')
        svg.append('</g>')

    # Nodes — neutral base state; route membership is shown by the
    # per-layout highlight layers drawn on top
    for name, (px, py) in pts.items():
        sid = system_ids.get(name, 0)
        count = planet_counts.get(name, 0)
        is_home = sid == home_id
        is_ignored = name in ignored_systems
        r = node_radius(name)
        if is_home:
            fill, opacity = "#4af", "0.9"
        elif is_ignored:
            fill, opacity = "#855", "0.35"
        else:
            fill, opacity = "#556", "0.6"

        svg.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="{r:.1f}" '
                   f'fill="{fill}" opacity="{opacity}" class="sys-node" '
                   f'data-system="{name}"/>')
        if is_home:
            svg.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" '
                       f'r="{r + 4:.1f}" fill="none" stroke="#4af" '
                       f'stroke-width="2" opacity="0.6"/>')

        if is_home:
            text_fill = "#ccd"
        elif is_ignored:
            text_fill = "#866"
        else:
            text_fill = "#778"
        svg.append(f'<text x="{px:.1f}" y="{py + r + 14:.1f}" '
                   f'fill="{text_fill}" font-size="12" '
                   f'text-anchor="middle" '
                   f'font-family="monospace">{name}</text>')
        if is_ignored:
            svg.append(f'<text x="{px:.1f}" y="{py + r + 26:.1f}" '
                       f'fill="#744" font-size="9" text-anchor="middle" '
                       f'font-family="monospace">(ignored)</text>')
        if count > 0:
            svg.append(f'<text x="{px:.1f}" y="{py + 4:.1f}" fill="#fff" '
                       f'font-size="10" text-anchor="middle" '
                       f'dominant-baseline="middle" '
                       f'font-family="monospace">{count}</text>')

    # Highlight layers per layout — above the nodes: rings on member
    # systems, diamond + label on the sell hub
    for idx, layout in enumerate(layouts[:3]):
        svg.append(layer_open(idx))
        members = set()
        for a in layout.get("allocated", []):
            for p in a.get("planets_used", []):
                if p.get("system"):
                    members.add(p["system"])
        sell = layout.get("route", {}).get("sell_system", "")
        for name in sorted(members):
            if name not in pts:
                continue
            px, py = pts[name]
            svg.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" '
                       f'r="{node_radius(name) + 3:.1f}" fill="none" '
                       f'stroke="#5bc" stroke-width="2.5" opacity="0.9"/>')
        if sell and sell != home_name and sell in pts:
            px, py = pts[sell]
            r = 10
            svg.append(f'<rect x="{px - r:.1f}" y="{py - r:.1f}" '
                       f'width="{2*r}" height="{2*r}" rx="3" '
                       f'fill="#e94" opacity="0.85" '
                       f'transform="rotate(45 {px:.1f} {py:.1f})"/>')
            svg.append(f'<text x="{px:.1f}" y="{py + 4:.1f}" fill="#fff" '
                       f'font-size="10" text-anchor="middle" '
                       f'dominant-baseline="middle" '
                       f'font-family="monospace">'
                       f'{planet_counts.get(sell) or ""}</text>')
            svg.append(f'<text x="{px:.1f}" '
                       f'y="{py + node_radius(sell) + 26:.1f}" '
                       f'fill="#fb6" font-size="9" text-anchor="middle" '
                       f'font-family="monospace">(sell)</text>')
        svg.append('</g>')

    svg.append('</svg>')
    return '\n'.join(svg)


# ── Skill projections ─────────────────────────────────────────

def compute_projections(top_chains, pi_skills, cfg):
    """Compute projected ISK/hr with key skill upgrades."""
    projections = []

    # CCU projection
    if pi_skills["ccu"] < 5:
        next_level = pi_skills["ccu"] + 1
        if next_level in CCU_BUDGETS:
            new_pg, new_cpu = CCU_BUDGETS[next_level]
            # Only show if next level actually exceeds current budget
            if new_pg > cfg["pg_budget"] or new_cpu > cfg["cpu_budget"]:
                pct_pg = (new_pg - cfg["pg_budget"]) / max(cfg["pg_budget"], 1) * 100
                pct_cpu = (new_cpu - cfg["cpu_budget"]) / max(cfg["cpu_budget"], 1) * 100
                projections.append({
                    "skill": f"Command Center Upgrades {next_level}",
                    "effect": f"+{pct_pg:.0f}% PG ({new_pg:,.0f}), +{pct_cpu:.0f}% CPU ({new_cpu:,.0f})",
                    "detail": "More facilities per planet, potentially more AIFs/BIFs",
                })
            else:
                # User's config already at or above the table value — note this
                projections.append({
                    "skill": f"Command Center Upgrades {next_level}",
                    "effect": f"PG {new_pg:,.0f}, CPU {new_cpu:,.0f} (verify in-game)",
                    "detail": "Config PG/CPU already matches or exceeds; update pi_config.ini after training",
                })

    # IC 5 projection
    if pi_skills["ic"] < 5:
        next_level = pi_skills["ic"] + 1
        new_max = next_level + 1
        # Check if any P4 chains become viable at this planet count
        p4_viable_at_next = [vc for vc in top_chains
                             if vc["chain"]["tier"] == "P4"
                             and vc.get("planet_count", 99) <= new_max]
        p4_note = ""
        if p4_viable_at_next:
            names = [vc["chain"]["output_name"] for vc in p4_viable_at_next]
            p4_note = f" P4 viable: {', '.join(names)}."
        else:
            # Find closest P4 for context
            p4_all = [vc for vc in top_chains if vc["chain"]["tier"] == "P4"]
            if p4_all:
                closest = min(p4_all, key=lambda vc: vc.get("planet_count", 99))
                p4_note = (f" P4 still infeasible (closest: "
                           f"{closest['chain']['output_name']} needs "
                           f"{closest.get('planet_count', '?')} planets).")
        projections.append({
            "skill": f"Interplanetary Consolidation {next_level}",
            "effect": f"+1 planet slot ({new_max} total)",
            "detail": f"Allows one more production planet.{p4_note}",
        })

    # Planetology projection — no direct yield bonus, improves scan resolution
    if pi_skills["planetology"] < 4:
        projections.append({
            "skill": f"Planetology {pi_skills['planetology'] + 1}",
            "effect": "Higher resolution resource scanning overlay",
            "detail": "No direct yield bonus. Helps place extractors on better hotspots, "
                      "which indirectly improves yield through better positioning.",
        })

    return projections


# ── Markdown output ───────────────────────────────────────────

def _fmt_isk(value):
    """Format ISK value for display."""
    if value is None or value == 0:
        return "--"
    if abs(value) >= 1_000_000_000:
        return f"{value/1e9:.1f}B"
    if abs(value) >= 1_000_000:
        return f"{value/1e6:.1f}M"
    if abs(value) >= 1_000:
        return f"{value/1e3:.1f}K"
    return f"{value:.0f}"


def _fmt_num(value, decimals=0):
    if value is None:
        return "--"
    if decimals == 0:
        return f"{value:,.0f}"
    return f"{value:,.{decimals}f}"


def _render_layout_table(layout, cfg, lines):
    """Render one layout as a markdown table."""
    allocated = layout["allocated"]
    total_net = layout["total_net"]
    total_haul = sum(vc.get("haul_minutes_per_day", 0) for vc in allocated)
    products = [vc["chain"]["output_name"] for vc in allocated]

    route = layout.get("route", {})
    if route and route.get("tour_jumps"):
        trips = route.get("trips_per_day", 1)
        haul_min = route.get("daily_haul_minutes", total_haul)
        ipm = route.get("isk_per_haul_min", 0)
        ipm_str = f"  |  {_fmt_isk(ipm)}/haul-min" if ipm else ""
        lines.append(f"**{_fmt_isk(total_net)}/hr net** -- {', '.join(products)}  |  "
                     f"Haul: {haul_min:.0f} min/day ({trips} trip{'s' if trips > 1 else ''})"
                     f"{ipm_str}  |  *{layout['strategy']}*")
        route_parts = []
        id_to_name_local = {}  # not available here, use systems_ordered
        ordered = route.get("systems_ordered", [])
        sell = route.get("sell_system", "")
        for sn in ordered:
            label = f"{sn} (sell)" if sn == sell else sn
            route_parts.append(label)
        if route_parts:
            lines.append(f"  Route: Home -> {' -> '.join(route_parts)} -> Home  |  "
                         f"{route.get('tour_jumps', 0)} jumps | "
                         f"{route.get('planet_stops', 0)} POCO stops")
    else:
        lines.append(f"**{_fmt_isk(total_net)}/hr net** -- {', '.join(products)}  |  "
                     f"Haul: {total_haul:.0f} min/day  |  *{layout['strategy']}*")
    lines.append("")
    lines.append("| Slot | System | Type | Role | Product | ISK/hr (chain) |")
    lines.append("|------|--------|------|------|---------|----------------|")

    slot = 0
    for vc in allocated:
        chain = vc["chain"]
        net = vc.get("net_isk_hr", 0)
        planets = vc.get("planets_used", [])
        for i, p in enumerate(planets):
            slot += 1
            isk_col = _fmt_isk(net) + "/hr" if i == 0 else ""
            lines.append(
                f"| {slot} | {p.get('system','?')} | {p.get('type','?')} | "
                f"{p.get('role','?')} | {chain['output_name']} | {isk_col} |"
            )
        if not planets:
            pc = vc.get("planet_count", 1)
            slot += pc
            lines.append(
                f"| {slot} | {cfg['home_system']} | -- | "
                f"{vc.get('layout_type','')} | {chain['output_name']} | "
                f"{_fmt_isk(net)}/hr |"
            )
    lines.append("")


def render_markdown(layouts, ranked_by_tier, cfg, char_info, pi_skills,
                    projections, market_prices, pi_types,
                    ignored_systems=None, calibration=None):
    """Generate the full dossier markdown."""
    lines = []
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    lines.append(f"# PI Dossier -- {char_info['name']} @ {cfg['home_system']}")
    lines.append(f"**Generated:** {now}")
    lines.append(f"**Skills:** CCU {pi_skills['ccu']}, IC {pi_skills['ic']}, "
                 f"Planetology {pi_skills['planetology']}, "
                 f"Adv Planetology {pi_skills['adv_planetology']}")
    lines.append(f"**Tax:** {cfg['tax_rate']*100:.0f}% (default)  |  "
                 f"**Sales tax:** {cfg.get('sales_tax', 0)*100:.2f}% "
                 f"(Accounting {pi_skills.get('accounting', 0)})  |  "
                 f"**Hauler:** {_fmt_num(cfg['hauler_m3'])} m3  |  "
                 f"**Max haul:** {cfg['max_haul_minutes']:.0f} min/day")
    if calibration:
        if calibration.get("points"):
            fit = calibration.get("fit")
            fit_note = (f", power fit b={fit['b']:.2f} at {fit['weight']:.0%}"
                        if fit else "")
            lines.append(f"**Rate model:** calibrated from "
                         f"{calibration['points']} observed rate(s) "
                         f"(x{calibration['scale']:.2f} vs base table{fit_note})")
        else:
            lines.append("**Rate model:** static density bands -- no observed "
                         "rates yet; enter OBS values in the editor to "
                         "calibrate all estimates")
    if ignored_systems:
        lines.append("")
        lines.append(f"**Ignored systems** (no PI there, routes avoid them): "
                     f"{', '.join(sorted(ignored_systems))}")
    if cfg.get("avoid_systems"):
        lines.append("")
        lines.append(f"**Avoided systems** (routes go around): "
                     f"{', '.join(sorted(cfg['avoid_systems']))}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Top layouts
    if layouts:
        lines.append("## Recommended Layouts")
        lines.append("")
        for i, layout in enumerate(layouts):
            label = ["Best", "Second-best", "Third-best"][i] if i < 3 else f"#{i+1}"
            lines.append(f"### {label} Layout")
            lines.append("")
            _render_layout_table(layout, cfg, lines)

        lines.append("---")
        lines.append("")

    # All chains ranked by tier
    for tier in ["P1", "P2", "P3", "P4"]:
        tier_chains = [vc for vc in ranked_by_tier if vc["chain"]["tier"] == tier]
        if not tier_chains:
            continue

        tier_label = {"P1": "P1 (Self-contained extraction)",
                      "P2": "P2 (Refined Commodities)",
                      "P3": "P3 (Specialized Commodities)",
                      "P4": "P4 (Advanced Commodities — full chain)"}
        lines.append(f"### {tier_label.get(tier, tier)}")
        lines.append("")
        lines.append("| Rank | Product | Setup | Units/hr | Sustained | Net ISK/hr | Adj ISK/hr | Trades | Haul (est.) | Flags |")
        lines.append("|------|---------|-------|----------|-----------|------------|------------|--------|-------------|-------|")

        for rank, vc in enumerate(tier_chains, 1):
            chain = vc["chain"]
            setup = vc.get("layout_type", "?")
            pc = vc.get("planet_count", len(vc.get("planets_used", [])))
            if setup == "p1_extractor":
                setup_str = f"1 planet"
            elif setup == "p2_selfcontained":
                setup_str = f"1 planet (self-contained)"
            elif setup == "p2_factory":
                setup_str = f"{pc} planets (factory)"
            elif setup == "p3_multi":
                setup_str = f"{pc} planets"
            elif setup == "p4_full":
                up0 = vc.get("unique_p0_count", "?")
                setup_str = f"{pc}p ({up0} P0s)"
            else:
                setup_str = setup

            flags_str = ", ".join(vc.get("flags", [])) if vc.get("flags") else "--"
            if not vc.get("viable"):
                flags_str = ", ".join(vc.get("flags", ["NOT VIABLE"]))

            net = vc.get("net_isk_hr", 0)
            adj = vc.get("adjusted_net_isk_hr", 0)
            # Only show adjusted if different from net (activity < 1.0)
            adj_str = f"{_fmt_isk(adj)}/hr" if abs(adj - net) > 1 else "="

            oc = vc.get("local_order_count", 0)
            lines.append(
                f"| {rank} | {chain['output_name']} | {setup_str} | "
                f"{vc.get('units_hr',0):.0f} | {_fmt_isk(vc.get('local_sustained',0))} | "
                f"{_fmt_isk(net)}/hr | {adj_str} | "
                f"{oc}/30d | "
                f"{vc.get('haul_minutes_per_day',0):.0f} min | {flags_str} |"
            )

        lines.append("")

    # Market notes
    lines.append("## Market Notes")
    lines.append("")
    strong_local = []
    thin_local = []
    jita_arb = []

    for vc in ranked_by_tier:
        if not vc.get("viable"):
            continue
        chain = vc["chain"]
        name = chain["output_name"]
        local_depth = vc.get("local_depth", 0)
        units_hr = vc.get("units_hr", 0)
        local_buy = vc.get("local_buy_price", 0)
        jita_buy = vc.get("jita_buy_price", 0)

        if local_depth > 0 and units_hr > 0 and local_depth / units_hr >= 24:
            strong_local.append(name)
        elif local_depth > 0:
            thin_local.append(name)

        if local_buy > 10 and jita_buy > local_buy:
            spread = (jita_buy - local_buy) / local_buy * 100
            if spread > 30:
                jita_arb.append(f"{name} (+{spread:.0f}%)")

    if strong_local:
        lines.append(f"- Local liquidity strong for: {', '.join(strong_local)}")
    if thin_local:
        lines.append(f"- Thin local market for: {', '.join(thin_local)}")
    if jita_arb:
        lines.append(f"- Jita arbitrage opportunities (>30% spread): {', '.join(jita_arb)}")
    lines.append("")

    # Skill projections
    if projections:
        lines.append("## Skill Upgrade Projections")
        lines.append("")
        for proj in projections:
            lines.append(f"- **{proj['skill']}**: {proj['effect']}")
            lines.append(f"  {proj['detail']}")
        lines.append("")

    # Flag definitions
    lines.append("## Flag Definitions")
    lines.append("")
    lines.append(f"- **Sustained**: blended price over 30d — walks buy book within {cfg['max_market_jumps']}j, VWAP for remainder")
    lines.append("- **Net ISK/hr**: revenue when selling (sustained price x units/hr - tax)")
    lines.append("- **Adj ISK/hr**: net x activity factor (trades/20, capped at 1.0). Penalises thin markets where production sits unsold. '=' means no penalty. Rankings use this.")
    lines.append("- **SHALLOW BUY**: real buy orders (>2 ISK) cover <7 days of production")
    lines.append("- **NO LOCAL BUYER**: no buy orders within jump range")
    lines.append("- **NO LOCAL MARKET**: no meaningful trade activity in region")
    lines.append(f"- **LOW ACTIVITY**: fewer than {LOW_ACTIVITY_ORDER_THRESHOLD} trades in last 30 calendar days (region-wide)")
    lines.append("- **HAUL OVER BUDGET**: daily haul exceeds max_haul_minutes_per_day")
    lines.append("- **POWER LIMIT**: layout pushes against PG or CPU ceiling")
    lines.append("- **NO [PLANET TYPE]**: chain needs a planet type not in your inventory")
    lines.append("- **LAUNCHPAD FILLS IN XH**: output outruns the 10k m3 launchpad before the next visit — add Storage Facilities")
    lines.append("- **SELL SLIPPAGE X%**: dumping one haul walks the Jita buy book down this far below the top order")
    lines.append("- **SELL IS X% OF 30d VOLUME**: one haul is this share of Jita's normal throughput — the book may not refill between runs")
    lines.append("- **EXCEEDS 5 PLANETS**: chain needs more planet slots than available")
    lines.append("- **JITA +X%**: Jita VWAP significantly higher than local buy")
    lines.append("")

    return "\n".join(lines)


# ── Build-sheet JSON + manual-override recompute ──────────────
#
# The web Build Sheet lets you hand-edit BIF counts per extractor planet
# (link power means each planet really fits a slightly different number than
# the optimizer assumed). The factory's AIFs are then re-derived from the
# resulting P1 supply. _LAST_RUN keeps the in-memory run so one chain can be
# recomputed without re-running the whole multi-minute optimizer.

_LAST_RUN = {"state": None}


def _planet_detail_json(p, pg_budget, cpu_budget):
    """JSON for one planet's build-sheet row (+ editability flags)."""
    pl = p.get("layout", {})
    fac = pl.get("facilities", {})
    p0_consumed = pl.get("p0_consumed_hr", 0)
    if isinstance(p0_consumed, (int, float)):
        p0_consumed = [p0_consumed] if p0_consumed > 0 else []
    return {
        "system": p.get("system", ""),
        "type": p.get("type", ""),
        "role": p.get("role", ""),
        "facilities": fac,
        "ecu_heads": (pl.get("ecu_heads", DEFAULT_ECU_HEADS)
                      if fac.get("ecu", 0) > 0 else 0),
        "p0_consumed_hr": p0_consumed,
        "units_hr": pl.get("units_hr", 0),
        "volume_hr": pl.get("volume_hr", 0),
        "pg_used": pl.get("pg_used", 0),
        "cpu_used": pl.get("cpu_used", 0),
        "pg_budget": pg_budget,
        "cpu_budget": cpu_budget,
        "over_budget": bool(pl.get("over_budget", False)),
        "rate_detail": p.get("rate_detail", ""),
        "rate_details": p.get("rate_details", []),
        "aif_breakdown": pl.get("aif_breakdown", []),
        "is_factory": bool(p.get("is_factory")),
        "bif_editable": bool(fac.get("bif")),
        "aif_editable": bool(fac.get("aif")),
    }


def _chain_entry_json(vc, pg_budget, cpu_budget):
    """JSON for one chain's build sheet + market block (used by both the
    initial dossier render and the single-chain recalc endpoint)."""
    chain = vc["chain"]
    planets_detail = [_planet_detail_json(p, pg_budget, cpu_budget)
                      for p in vc.get("planets_used", [])]
    return {
        "output_name": chain["output_name"],
        "tier": chain["tier"],
        "layout_type": vc.get("layout_type", ""),
        "units_hr": vc.get("units_hr", 0),
        "net_isk_hr": vc.get("net_isk_hr", 0),
        "gross_isk_hr": vc.get("gross_isk_hr", 0),
        "tax_per_hr": vc.get("tax_per_hr", 0),
        "haul_minutes_per_day": vc.get("haul_minutes_per_day", 0),
        "planets_used": planets_detail,
        "bottleneck": vc.get("bottleneck", ""),
        "flags": vc.get("flags", []),
        "market": {
            "local_buy": vc.get("local_buy_price", 0),
            "local_sustained": vc.get("local_sustained", 0),
            "buyer_system": vc.get("local_buyer_system", ""),
            "buyer_jumps": vc.get("local_buyer_jumps", 0),
            "depth_days": vc.get("local_real_buy_days", 0),
            "depth_units": vc.get("local_real_depth", 0),
            "avg_daily_vol": vc.get("local_avg_daily_vol", 0),
            "active_days": vc.get("local_active_days", 0),
            "order_count": vc.get("local_order_count", 0),
            "jita_buy": vc.get("jita_buy_price", 0),
            "jita_vwap": vc.get("jita_vwap", 0),
            "jita_daily_vol": vc.get("jita_avg_daily_vol", 0),
            "sell_recommendation": vc.get("sell_recommendation", ""),
            "sell_at_jita": vc.get("sell_at_jita", False),
            "sell_price": vc.get("sell_price", vc.get("local_sustained", 0)),
        },
    }


def _split_bifs(total, rates):
    """Distribute `total` BIFs across P0 lines proportional to extraction rate.

    Each active line gets at least 1; rounding remainder goes to the faster
    line(s). Returns a list of ints summing to `total` (or fewer when
    total < number of lines, filling the fastest lines first)."""
    n = len(rates)
    if n == 0 or total <= 0:
        return [0] * n
    if total <= n:
        order = sorted(range(n), key=lambda i: -rates[i])
        split = [0] * n
        for k in range(total):
            split[order[k]] = 1
        return split
    rate_sum = sum(rates) or 1
    raw = [r / rate_sum * total for r in rates]
    split = [max(1, int(x)) for x in raw]
    diff = total - sum(split)
    frac_order = sorted(range(n), key=lambda i: -(raw[i] - int(raw[i])))
    k = 0
    while diff > 0:
        split[frac_order[k % n]] += 1
        diff -= 1
        k += 1
    small_order = sorted(range(n), key=lambda i: split[i])
    k = 0
    guard = 0
    while diff < 0 and guard < 10000:
        idx = small_order[k % n]
        if split[idx] > 1:
            split[idx] -= 1
            diff += 1
        k += 1
        guard += 1
    return split


def _recompute_chain(vc, ctx, aif_overrides=None):
    """Recompute a chain's throughput from its planets' current (possibly
    user-edited) facility counts + stored raw extraction rates. Mutates vc.

    aif_overrides: {planet_index: int} optional factory-AIF target. Below the
    supply-supported max it caps output; above it, output stays capped by P1
    supply (extra AIFs would just starve)."""
    aif_overrides = aif_overrides or {}
    lt = vc.get("layout_type", "")
    planets = vc.get("planets_used", [])
    chain = vc["chain"]
    pgb, cpub = ctx["pg_budget"], ctx["cpu_budget"]
    BIF_FULL = 6000
    AIF_P1_NEED = 40  # P1/hr per input per P2 AIF

    def refresh_power(p):
        lay = p.get("layout") or {}
        fac = lay.get("facilities", {})
        pg_rem, cpu_rem = _planet_budget_remaining(
            pgb, cpub, fac, lay.get("ecu_heads", DEFAULT_ECU_HEADS))
        lay["pg_used"] = pgb - pg_rem
        lay["cpu_used"] = cpub - cpu_rem
        lay["over_budget"] = (pg_rem < 0) or (cpu_rem < 0)

    def fac_int(lay, key):
        try:
            return max(0, int(lay.get("facilities", {}).get(key, 0)))
        except (TypeError, ValueError):
            return 0

    if not planets:
        return

    if lt == "p1_extractor":
        p = planets[0]
        lay = p["layout"]
        rate = (p.get("extract_rates") or [lay.get("p0_consumed_hr", 0)])[0]
        bif = fac_int(lay, "bif")
        units = _bif_p1_output(rate, bif)
        lay["units_hr"] = units
        lay["volume_hr"] = units * chain["volume"]
        lay["p0_consumed_hr"] = min(rate, bif * BIF_FULL)
        refresh_power(p)
        vc["units_hr"] = units
        vc["volume_hr"] = lay["volume_hr"]

    elif lt == "p2_selfcontained":
        p = planets[0]
        lay = p["layout"]
        rates = p.get("extract_rates") or []
        total_bif = fac_int(lay, "bif")
        split = _split_bifs(total_bif, rates)
        lay["facilities"]["bif"] = sum(split)
        lay["bif_split"] = split
        p1_outs = [_bif_p1_output(r, b) for r, b in zip(rates, split)]
        min_p1 = min(p1_outs) if p1_outs else 0
        max_aif = min_p1 / AIF_P1_NEED
        if 0 in aif_overrides:
            aif = aif_overrides[0]
            eff_aif = min(aif, max_aif)  # AIFs past supply just starve
        else:
            aif = int(max_aif)  # auto-derive whole AIFs the supply sustains
            eff_aif = aif
        lay["facilities"]["aif"] = aif
        units = eff_aif * 5
        lay["units_hr"] = units
        lay["volume_hr"] = units * chain["volume"]
        lay["p0_consumed_hr"] = [min(r, b * BIF_FULL)
                                 for r, b in zip(rates, split)]
        refresh_power(p)
        vc["units_hr"] = units
        vc["volume_hr"] = units * chain["volume"]

    elif lt == "p2_factory":
        supply_by_input = {}
        fac_idx = None
        for i, p in enumerate(planets):
            if p.get("is_factory"):
                fac_idx = i
                continue
            lay = p["layout"]
            rate = (p.get("extract_rates") or [0])[0]
            bif = fac_int(lay, "bif")
            po = _bif_p1_output(rate, bif)
            lay["units_hr"] = po
            lay["volume_hr"] = po * (p.get("p1_volume", 0) or 0)
            lay["p0_consumed_hr"] = min(rate, bif * BIF_FULL)
            p["p1_output_hr"] = po
            refresh_power(p)
            key = p.get("p1_type_id") or p.get("p1_name") or i
            supply_by_input[key] = supply_by_input.get(key, 0) + po
        min_p1 = min(supply_by_input.values()) if supply_by_input else 0
        max_aif = min_p1 / AIF_P1_NEED
        if fac_idx is not None:
            flay = planets[fac_idx]["layout"]
            if fac_idx in aif_overrides:
                aif = aif_overrides[fac_idx]
                eff_aif = min(aif, max_aif)  # AIFs past supply just starve
            else:
                aif = int(max_aif)  # auto-derive whole AIFs supply sustains
                eff_aif = aif
            units = eff_aif * 5
            flay["facilities"]["aif"] = aif
            flay["units_hr"] = units
            flay["volume_hr"] = units * chain["volume"]
            refresh_power(planets[fac_idx])
            vc["units_hr"] = units
            vc["volume_hr"] = units * chain["volume"]

    elif lt == "p3_multi":
        p1_supply = {}
        fac_idx = None
        for i, p in enumerate(planets):
            if p.get("is_factory"):
                fac_idx = i
                continue
            lay = p["layout"]
            rate = (p.get("extract_rates") or [0])[0]
            bif = fac_int(lay, "bif")
            po = _bif_p1_output(rate, bif)
            lay["units_hr"] = po
            lay["volume_hr"] = po * (p.get("p1_volume", 0) or 0)
            lay["p0_consumed_hr"] = min(rate, bif * BIF_FULL)
            p["p1_output_hr"] = po
            refresh_power(p)
            tid = p.get("p1_type_id")
            if tid:
                p1_supply[tid] = p1_supply.get(tid, 0) + po
        casc = _p3_factory_cascade(chain, p1_supply, ctx)
        units = casc["units_hr"]
        total_aifs = casc["total_aifs"]
        breakdown = list(casc["aif_breakdown"])
        if fac_idx is not None:
            flay = planets[fac_idx]["layout"]
            override = aif_overrides.get(fac_idx)
            if (override is not None and total_aifs > 0
                    and override < total_aifs):
                scale = override / total_aifs
                units *= scale
                total_aifs = override
                breakdown.append(f"(scaled to {override} AIF cap)")
            flay["facilities"]["aif"] = total_aifs
            flay["units_hr"] = units
            flay["volume_hr"] = units * chain["volume"]
            flay["aif_breakdown"] = breakdown
            refresh_power(planets[fac_idx])
        vc["units_hr"] = units
        vc["volume_hr"] = units * chain["volume"]


def recalc_chain_build(layout_index, chain_index, bif_overrides=None,
                       aif_overrides=None):
    """Recompute a single chain's build + economics from manual facility
    overrides, reusing the last in-memory run. Returns the updated chain
    entry + the layout's new total, or {"error": ...} if no run is cached."""
    state = _LAST_RUN.get("state")
    if not state:
        return {"error": "No PI run in memory — generate the dossier first."}
    layouts = state["layouts"]
    try:
        layout = layouts[layout_index]
        vc = layout["allocated"][chain_index]
    except (IndexError, KeyError, TypeError):
        return {"error": "Chain not found — please regenerate the dossier."}

    ctx, ectx, cfg = state["ctx"], state["ectx"], state["cfg"]
    pgb, cpub = state["pg_budget"], state["cpu_budget"]
    planets = vc.get("planets_used", [])

    for k, v in (bif_overrides or {}).items():
        try:
            idx, cnt = int(k), max(0, int(v))
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(planets):
            lay = planets[idx].get("layout")
            if lay and "bif" in lay.get("facilities", {}):
                lay["facilities"]["bif"] = cnt

    aif_ov = {}
    for k, v in (aif_overrides or {}).items():
        try:
            aif_ov[int(k)] = max(0, int(v))
        except (TypeError, ValueError):
            continue

    _recompute_chain(vc, ctx, aif_ov)

    # Re-price this chain (clear the idempotency guard first)
    vc["_econ_done"] = False
    _compute_economics_single(vc, ectx)

    # Layout total + route economics (output volume changed -> trip count)
    total_net = sum(c.get("adjusted_net_isk_hr", 0)
                    for c in layout["allocated"])
    layout["total_net"] = total_net
    route = layout.get("route", {})
    rm = route.get("route_minutes", 0)
    if rm:
        trips = _compute_trips_per_day(layout["allocated"], cfg["hauler_m3"])
        daily = rm * trips
        route["trips_per_day"] = trips
        route["daily_haul_minutes"] = daily
        route["isk_per_haul_min"] = (total_net * 24 / daily) if daily > 0 else 0

    return {
        "layout_index": layout_index,
        "chain_index": chain_index,
        "entry": _chain_entry_json(vc, pgb, cpub),
        "total_net": total_net,
        "route": route,
    }


# ── Single-product scaling (dedicate more planets to one product) ──────
#
# The optimizer sizes every product to a *minimal* footprint (one extractor
# per P0 + one factory) so the other planets stay free for other products.
# The Target-a-Product what-if asks the opposite: "if I commit my colony to
# THIS product, what does each extra planet buy me?" These helpers re-resolve
# the same chain across more extractor planets (balanced by P1 supply) and
# report output + the per-planet layout at every planet count up to
# max_planets. Tax-per-unit is recipe-determined, so net at a guaranteed
# price scales linearly with throughput — the work is all in units_hr.


def _scaled_ext_alloc(ctx, n_ext, p0_names, exclude=frozenset()):
    """Greedily place n_ext extractor planets balanced across p0_names.

    Each new planet goes to the P0 type with the lowest running P1 supply,
    drawing the best unused candidate (global rank) for it. Output is gated
    by the *slowest* input line, so equalising supply maximises throughput.
    Returns (alloc, used) where alloc is a list of (p0_name, candidate) and
    used is the set of claimed (system, ptype, instance) instances.
    """
    pgb, cpub = ctx["pg_budget"], ctx["cpu_budget"]
    pools = {n: ctx["p0_candidates"].get(n, []) for n in dict.fromkeys(p0_names)}
    used = set(exclude)
    supply = {n: 0.0 for n in pools}
    alloc = []

    def _p1_out(rate):
        lay = compute_p1_layout({"volume": 0.38, "tier": "P1"}, rate, pgb, cpub)
        return lay["units_hr"] if lay else 0  # P1 units_hr is volume-independent

    for _ in range(n_ext):
        pick = None
        for name in sorted(supply, key=lambda n: supply[n]):
            for c in pools[name]:
                if (c[2], c[3], c[4]) not in used:
                    pick = (name, c)
                    break
            if pick:
                break
        if not pick:
            break  # inventory exhausted — curve plateaus here
        name, c = pick
        used.add((c[2], c[3], c[4]))
        supply[name] += _p1_out(c[0])
        alloc.append((name, c))
    return alloc, used


def _scaled_extractor_planet(p0_name, cand, p1_info, ctx):
    """One extractor planet entry producing p1_info from p0_name at cand's rate."""
    rate, tag, system, ptype, inst = cand
    vol = p1_info.get("volume", 0.38)
    p1_layout = compute_p1_layout({"volume": vol, "tier": "P1"}, rate,
                                  ctx["pg_budget"], ctx["cpu_budget"])
    if not p1_layout:
        return None
    return {
        "system": system, "type": f"{ptype} {inst}",
        "role": f"Extract {p0_name} -> {p1_info.get('name', 'P1')}",
        "layout": p1_layout,
        "p1_output_hr": p1_layout["units_hr"],
        "p1_type_id": p1_info.get("type_id", 0),
        "p1_name": p1_info.get("name", "P1"),
        "p1_volume": vol,
        "extract_rates": [rate],
        "rate_detail": f"{p0_name}: {rate:.0f}/hr [{tag}]",
    }


def _build_scaled_vc(base_vc, ctx, total_planets):
    """Re-resolve base_vc's product to occupy `total_planets` planets.

    Returns a vc-shaped dict (planets_used / units_hr / planet_count) or None
    when the product can't be placed at that size (e.g. inventory ran out of
    candidate planets, or no spare planet for the factory). Economics are
    attached by the caller. P4 chains have no concrete layout and are skipped.
    """
    chain = base_vc["chain"]
    lt = base_vc.get("layout_type", "")
    pgb, cpub = ctx["pg_budget"], ctx["cpu_budget"]
    p0_to_p1 = ctx["p0_to_p1"]

    def vc_dict(layout_type, planets, units, tags, flags=None, bottleneck=None):
        return {
            "chain": chain, "viable": True, "layout_type": layout_type,
            "planets_used": planets, "planet_count": len(planets),
            "units_hr": units, "volume_hr": units * chain.get("volume", 0),
            "rate_sources": tags, "flags": flags or [],
            "bottleneck": bottleneck or "",
        }

    if lt == "p1_extractor":
        # Independent extractor planets; output is the simple sum.
        p0 = chain["p0_inputs"][0]["name"] if chain.get("p0_inputs") else None
        p1_info = {"name": chain["output_name"],
                   "volume": chain.get("volume", 0.38),
                   "type_id": chain["output_type_id"]}
        planets, tags = [], []
        for cand in ctx["p0_candidates"].get(p0, []):
            if len(planets) >= total_planets:
                break
            ent = _scaled_extractor_planet(p0, cand, p1_info, ctx)
            if ent:
                ent["role"] = f"Extract {p0} -> {chain['output_name']}"
                planets.append(ent)
                tags.append(cand[1])
        if not planets:
            return None
        units = sum(p["layout"]["units_hr"] for p in planets)
        return vc_dict("p1_extractor", planets, units, tags)

    if lt == "p2_selfcontained":
        # Independent self-contained planets; output is the sum.
        p0_names = [p["name"] for p in chain["p0_inputs"]]
        planets, tags = [], []
        for c in _sc_candidates_for_chain(chain, ctx)[:total_planets]:
            inst_rates = ctx["instance_rates"].get(
                (c["system"], c["ptype"], c["instance"]), {})
            pair = [inst_rates.get(n, (0, "?")) for n in p0_names]
            rates = [p[0] for p in pair]
            ptags = [p[1] for p in pair]
            layout = compute_p2_selfcontained_layout(chain, rates, pgb, cpub)
            if not layout:
                continue
            planets.append({
                "system": c["system"], "type": f"{c['ptype']} {c['instance']}",
                "role": f"Extract+Process -> {chain['output_name']}",
                "layout": layout,
                "rate_details": [f"{n}: {r:.0f}/hr [{t}]"
                                 for n, r, t in zip(p0_names, rates, ptags)],
                "extract_rates": list(rates), "p0_names": list(p0_names),
            })
            tags.extend(ptags)
        if not planets:
            return None
        units = sum(p["layout"]["units_hr"] for p in planets)
        return vc_dict("p2_selfcontained", planets, units, tags)

    if lt in ("p2_factory", "p3_multi"):
        if lt == "p2_factory":
            p0_names = [p["name"] for p in chain["p0_inputs"]]
        else:
            p0_names = list(chain["all_p0_names"])
        n_ext = max(1, total_planets - 1)  # reserve 1 planet for the factory
        alloc, used = _scaled_ext_alloc(ctx, n_ext, p0_names)
        if not alloc:
            return None
        extraction_planets, tags = [], []
        supply_by_p1 = {}
        for name, cand in alloc:
            p1_info = p0_to_p1.get(name, {"name": "P1", "volume": 0.38,
                                          "type_id": 0})
            ent = _scaled_extractor_planet(name, cand, p1_info, ctx)
            if not ent:
                continue
            extraction_planets.append(ent)
            tags.append(cand[1])
            tid = p1_info.get("type_id", 0)
            supply_by_p1[tid] = supply_by_p1.get(tid, 0) + ent["p1_output_hr"]
        if not extraction_planets:
            return None
        fac = _pick_factory_planet(
            [p["system"] for p in extraction_planets],
            sorted(ctx["inv"]), used, ctx)
        if not fac:
            return None  # no spare planet to host the factory

        if lt == "p2_factory":
            cap_layout = compute_factory_layout(chain, pgb, cpub)
            if not cap_layout:
                return None
            fac_cap = cap_layout["facilities"]["aif"]
            min_supply = min((supply_by_p1.get(inp["type_id"], 0)
                              for inp in chain["inputs"]), default=0)
            aifs = max(0, min(fac_cap, int(min_supply / 40)))
            units = aifs * 5
            # PG/CPU must reflect the AIFs actually placed, not the cap.
            facilities = {"aif": aifs, "launchpad": 1}
            pg_rem, cpu_rem = _planet_budget_remaining(pgb, cpub, facilities)
            factory_layout = {
                "facilities": facilities, "units_hr": units,
                "volume_hr": units * chain["volume"],
                "pg_used": pgb - pg_rem, "cpu_used": cpub - cpu_rem,
                "role": "factory",
            }
            if aifs >= fac_cap:
                bottleneck = (f"Factory AIF capacity (PG/CPU): {aifs} AIF "
                              f"-> {units:.0f} P2/hr")
            else:
                bottleneck = (f"P1 supply: {min_supply:.0f}/hr each input "
                              f"-> {aifs} AIF -> {units:.0f} P2/hr")
            planets = list(extraction_planets)
            planets.append(_factory_planet_entry(fac, chain, factory_layout))
            return vc_dict("p2_factory", planets, units, tags,
                           bottleneck=bottleneck)

        # p3_multi: trace the real P1 -> P2 -> P3 cascade for this supply.
        casc = _p3_factory_cascade(chain, supply_by_p1, ctx)
        units = casc["units_hr"]
        facilities = {"aif": casc["total_aifs"], "launchpad": 1}
        pg_rem, cpu_rem = _planet_budget_remaining(pgb, cpub, facilities)
        flags = list(casc.get("flags", []))
        if pg_rem < 0 or cpu_rem < 0:
            flags.append("FACTORY POWER LIMIT")
        factory_layout = {
            "facilities": facilities, "units_hr": units,
            "volume_hr": units * chain["volume"],
            "pg_used": pgb - pg_rem, "cpu_used": cpub - cpu_rem,
            "role": "factory", "aif_breakdown": casc["aif_breakdown"],
        }
        planets = list(extraction_planets)
        planets.append(_factory_planet_entry(fac, chain, factory_layout))
        return vc_dict("p3_multi", planets, units, tags, flags)

    return None  # p4_full and unknown layouts don't scale


def product_scaling(output_name, price_override=None):
    """Scaling curve for one product: output + net + per-planet layout at
    each planet count from its minimal footprint up to max_planets.

    Reuses the last in-memory run. Tax-per-unit is taken from the optimizer's
    minimal build (recipe-determined, planet-count-independent), so net at a
    guaranteed price is just units_hr × (price − tax_per_unit). Rows that buy
    no extra output over a smaller build are flagged (a 2-input product only
    gains output when extractors are added in balanced pairs).
    """
    state = _LAST_RUN.get("state")
    if not state:
        return {"error": "No PI run in memory — generate the dossier first."}
    ctx, cfg = state["ctx"], state["cfg"]
    pgb, cpub = state["pg_budget"], state["cpu_budget"]
    ranked = state.get("ranked") or []
    base = next((v for v in ranked
                 if v["chain"]["output_name"] == output_name), None)
    if base is None:
        return {"error": f"Product '{output_name}' not found — regenerate the dossier."}

    base_units = base.get("units_hr", 0)
    tax_per_unit = (base.get("tax_per_hr", 0) / base_units
                    if base_units > 0 else 0)
    # Price scaled builds at the same effective sell price the optimizer used
    # (Jita buy when sell_at_jita is on, else the sustained local price).
    sustained = base.get("sell_price", base.get("local_sustained", 0))
    max_planets = cfg["max_planets"]
    base_count = base.get("planet_count", len(base.get("planets_used", [])))

    # Display fields copied onto each scaled vc so _chain_entry_json's market
    # block renders (prices don't change with scale; only volumes do).
    # bottleneck is intentionally NOT copied — each scaled build computes its
    # own (the base build's bottleneck is wrong at other planet counts).
    market_keys = ("local_buy_price", "local_sustained", "local_buyer_system",
                   "local_buyer_jumps", "local_real_buy_days",
                   "local_real_depth", "local_avg_daily_vol",
                   "local_active_days", "local_order_count", "jita_buy_price",
                   "jita_vwap", "jita_avg_daily_vol", "sell_recommendation")

    scales = []
    seen = set()
    prev_units = -1.0
    for total in range(base_count, max_planets + 1):
        svc = _build_scaled_vc(base, ctx, total)
        if not svc:
            continue
        pc = svc["planet_count"]
        u = round(svc["units_hr"], 3)
        if (pc, u) in seen:
            continue
        seen.add((pc, u))
        for k in market_keys:
            svc.setdefault(k, base.get(k))
        if not svc.get("bottleneck"):
            svc["bottleneck"] = _identify_bottleneck(svc)
        svc["tax_per_hr"] = tax_per_unit * svc["units_hr"]
        svc["gross_isk_hr"] = sustained * svc["units_hr"]
        svc["net_isk_hr"] = svc["gross_isk_hr"] - svc["tax_per_hr"]
        if 0 <= prev_units >= svc["units_hr"] and pc > base_count:
            svc.setdefault("flags", []).insert(
                0, "NO EXTRA OUTPUT (needs a balanced pair)")
        prev_units = svc["units_hr"]

        sp = {
            "planet_count": pc,
            "units_hr": svc["units_hr"],
            "volume_hr": svc["volume_hr"],
            "tax_per_hr": svc["tax_per_hr"],
            "market_gross_isk_hr": svc["gross_isk_hr"],
            "market_net_isk_hr": svc["net_isk_hr"],
            "is_base": pc == base_count,
            "entry": _chain_entry_json(svc, pgb, cpub),
            "flags": svc.get("flags", []),
        }
        if price_override is not None and price_override > 0:
            gross = svc["units_hr"] * price_override
            sp["custom_price"] = price_override
            sp["custom_gross_isk_hr"] = gross
            sp["custom_net_isk_hr"] = (gross * (1.0 - cfg.get("sales_tax", 0.0))
                                       - svc["tax_per_hr"])
        scales.append(sp)

    return {
        "output_name": output_name,
        "tier": base["chain"]["tier"],
        "base_planet_count": base_count,
        "max_planets": max_planets,
        "tax_per_unit": tax_per_unit,
        "market_price": sustained,
        "scalable": len(scales) > 1,
        "scales": scales,
    }


def product_at_price(output_name, price_override=None):
    """Full build sheet + economics for one named product, optionally
    re-priced at a user-supplied *guaranteed* sell price.

    The optimizer prices every product off the local buy-order book (blended
    with VWAP). This lets the user pick any product and assert "I'd sell it at
    X" — gross = units/hr × X, minus POCO tax, with NO liquidity/activity
    penalty (the price is taken as guaranteed, so depth doesn't cap it).

    Returns the product's build sheet (same shape the layout cards use), its
    market-based net for reference, the at-price net, and the current best
    viable product for side-by-side comparison. Reuses the last in-memory run.
    """
    state = _LAST_RUN.get("state")
    if not state:
        return {"error": "No PI run in memory — generate the dossier first."}
    ranked = state.get("ranked") or []
    pgb, cpub = state["pg_budget"], state["cpu_budget"]

    vc = next((v for v in ranked
               if v["chain"]["output_name"] == output_name), None)
    if vc is None:
        return {"error": f"Product '{output_name}' not found — regenerate the dossier."}

    entry = _chain_entry_json(vc, pgb, cpub)
    units_hr = vc.get("units_hr", 0)
    tax_per_hr = vc.get("tax_per_hr", 0)

    out = {
        "entry": entry,
        "units_hr": units_hr,
        "tax_per_hr": tax_per_hr,
        "market_price": vc.get("sell_price", vc.get("local_sustained", 0)),
        "sell_at_jita": vc.get("sell_at_jita", False),
        "market_gross_isk_hr": vc.get("gross_isk_hr", 0),
        "market_net_isk_hr": vc.get("net_isk_hr", 0),
        "haul_minutes_per_day": vc.get("haul_minutes_per_day", 0),
        "viable": vc.get("viable", False),
        "flags": vc.get("flags", []),
    }

    if price_override is not None and price_override > 0:
        gross = units_hr * price_override
        _st = state.get("cfg", {}).get("sales_tax", 0.0)
        out["custom_price"] = price_override
        out["custom_gross_isk_hr"] = gross
        out["custom_net_isk_hr"] = gross * (1.0 - _st) - tax_per_hr

    # Best = highest activity-adjusted net among viable products (the headline
    # "this is the best thing" recommendation). Falls back to top of ranked.
    best = next((v for v in ranked if v.get("viable")),
                ranked[0] if ranked else None)
    if best is not None:
        out["best"] = {
            "output_name": best["chain"]["output_name"],
            "tier": best["chain"]["tier"],
            "net_isk_hr": best.get("net_isk_hr", 0),
            "adjusted_net_isk_hr": best.get("adjusted_net_isk_hr", 0),
            "market_price": best.get("local_sustained", 0),
            "units_hr": best.get("units_hr", 0),
            "layout_type": best.get("layout_type", ""),
            "planet_count": best.get("planet_count",
                                     len(best.get("planets_used", []))),
            "is_target": best["chain"]["output_name"] == output_name,
        }

    # Scaling curve: what each extra dedicated planet buys for this product.
    scaling = product_scaling(output_name, price_override)
    if "error" not in scaling:
        out["scaling"] = scaling
    return out


# ── Import-and-upgrade analysis ───────────────────────────────
# Alternative to full vertical integration: BUY lower-tier PI commodities at
# Jita (sell orders), haul them to a factory planet, process up to a higher
# tier, and dump the finished product at Jita (buy orders). No extraction — the
# planet is pure refinery. Compares "which product is worth making this way,
# and from which buy-in tier" purely on Jita spread minus POCO tax.

_TIER_LEVEL = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4}
_TIER_NAMES = {v: k for k, v in _TIER_LEVEL.items()}


def _first_schematic(type_id, pi_types, schematics):
    """The production schematic for a type (first listed), or None."""
    t = pi_types.get(type_id)
    if not t:
        return None
    for sid in t.get("produced_by", []):
        sch = schematics.get(sid)
        if sch:
            return sch
    return None


def _bom_at_tier(type_id, qty, buy_level, pi_types, schematics, acc, depth=0):
    """Accumulate into acc {type_id: units} the tier<=buy_level commodities
    required to make `qty` units of type_id, expanding higher tiers via their
    schematics. Fractional quantities are expected (per 1 unit of a target)."""
    t = pi_types.get(type_id)
    if not t or depth > 12:
        return
    if _TIER_LEVEL.get(t["tier"], 0) <= buy_level:
        acc[type_id] = acc.get(type_id, 0.0) + qty
        return
    sch = _first_schematic(type_id, pi_types, schematics)
    if not sch:
        acc[type_id] = acc.get(type_id, 0.0) + qty  # unexpandable -> treat as buy
        return
    out_qty = sch["output"].get("quantity", 1) or 1
    runs = qty / out_qty
    for inp in sch["inputs"]:
        _bom_at_tier(inp["type_id"], inp["quantity"] * runs, buy_level,
                     pi_types, schematics, acc, depth + 1)


def _facility_bundle(type_id, rate_hr, buy_level, pi_types, schematics,
                     acc, depth=0):
    """Accumulate fractional facility counts into acc ({"aif","htif"}) needed
    to sustain rate_hr units/hr of type_id, processing every tier above
    buy_level on-planet (tiers <= buy_level are bought, so no facility)."""
    t = pi_types.get(type_id)
    if not t or depth > 12:
        return
    tier = t["tier"]
    if _TIER_LEVEL.get(tier, 0) <= buy_level:
        return
    sch = _first_schematic(type_id, pi_types, schematics)
    if not sch:
        return
    out_qty = sch["output"].get("quantity", 1) or 1
    cycle = sch.get("cycle_time", 3600) or 3600
    per_fac_hr = out_qty * 3600.0 / cycle
    if per_fac_hr > 0:
        key = "htif" if tier == "P4" else "aif"
        acc[key] = acc.get(key, 0.0) + rate_hr / per_fac_hr
    cycles_hr = rate_hr / out_qty
    for inp in sch["inputs"]:
        _facility_bundle(inp["type_id"], inp["quantity"] * cycles_hr,
                         buy_level, pi_types, schematics, acc, depth + 1)


def _storage_for(m3_needed):
    """Storage Facilities required beyond the one launchpad, to hold m3_needed."""
    over = m3_needed - FACILITY_M3["launchpad"]
    return max(0, math.ceil(over / FACILITY_M3["storage"])) if over > 0 else 0


def _factory_units_per_planet(bundle_at_1, pg_budget, cpu_budget,
                              m3_per_unit=0.0, buffer_days=0.0):
    """Max target units/hr one factory planet sustains for a bundle sized to
    make 1 unit/hr, after reserving one launchpad.

    Limited by PG and CPU, and — when buffer_days is set — by what the planet
    can physically hold. `m3_per_unit` is the input + output volume per
    finished unit; the planet must stage `buffer_days` of that flow on-planet
    between visits. Storage Facilities buy capacity out of the same PG/CPU
    budget the factories want, so every storage count is tried and the best
    resulting throughput wins.

    Returns (units_hr, pg_per_unit, cpu_per_unit, storage_count).
    """
    c = FACILITY_COSTS
    pg_per = (bundle_at_1.get("aif", 0) * c["aif"]["pg"]
              + bundle_at_1.get("htif", 0) * c["htif"]["pg"])
    cpu_per = (bundle_at_1.get("aif", 0) * c["aif"]["cpu"]
               + bundle_at_1.get("htif", 0) * c["htif"]["cpu"])
    if pg_per <= 0 or cpu_per <= 0:
        return 0.0, 0.0, 0.0, 0
    flow_per_unit_hr = m3_per_unit * 24 * buffer_days  # m3 held per unit/hr
    best_units, best_storage = 0.0, 0
    n = 0
    while True:
        pg_avail = pg_budget - c["launchpad"]["pg"] - n * c["storage"]["pg"]
        cpu_avail = cpu_budget - c["launchpad"]["cpu"] - n * c["storage"]["cpu"]
        if pg_avail < pg_per or cpu_avail < cpu_per:
            break
        units = min(pg_avail / pg_per, cpu_avail / cpu_per)
        if flow_per_unit_hr > 0:
            hold = FACILITY_M3["launchpad"] + n * FACILITY_M3["storage"]
            units = min(units, hold / flow_per_unit_hr)
        if units > best_units:
            best_units, best_storage = units, n
        n += 1
    # Report only the storage the winning throughput actually needs — the
    # search may have peaked at a count with spare capacity.
    if best_units > 0 and flow_per_unit_hr > 0:
        best_storage = _storage_for(best_units * flow_per_unit_hr)
    return best_units, pg_per, cpu_per, best_storage


def _snap_facilities(bundle, units_hr, pg_budget, cpu_budget, storage_count):
    """Whole factories to actually build, and the throughput they support.

    You cannot place 9.72 High-Tech Production Plants. Round each facility type
    up so nothing starves; if the rounded-up build no longer fits the command
    centre, round down instead and accept the lower ceiling.

    Returns (counts, achievable_units_hr, pg_used, cpu_used).
    """
    c = FACILITY_COSTS
    base_pg = c["launchpad"]["pg"] + storage_count * c["storage"]["pg"]
    base_cpu = c["launchpad"]["cpu"] + storage_count * c["storage"]["cpu"]
    wanted = {k: v for k, v in bundle.items() if v > 0}
    if not wanted or units_hr <= 0:
        return {}, 0.0, base_pg, base_cpu
    for rounder in (math.ceil, math.floor):
        counts = {k: int(rounder(v * units_hr)) for k, v in wanted.items()}
        if not all(counts.values()):
            continue
        pg = base_pg + sum(n * c[k]["pg"] for k, n in counts.items())
        cpu = base_cpu + sum(n * c[k]["cpu"] for k, n in counts.items())
        if pg <= pg_budget and cpu <= cpu_budget:
            achieved = min(counts[k] / wanted[k] for k in counts)
            return counts, achieved, pg, cpu
    return {}, 0.0, base_pg, base_cpu


def _factory_tax_rate(state):
    """POCO rate for a hypothetical import factory: the cheapest scouted
    rate among active-inventory planets (a factory needs no extraction, so
    tax is the only siting criterion). Falls back to cfg default when no
    planet is scouted. Returns (rate, planet_key_or_None)."""
    ctx = state.get("ctx") or {}
    cfg = state.get("cfg") or {}
    taxes = ctx.get("planet_taxes") or {}
    best_rate, best_key = None, None
    for system, planets in (ctx.get("inv") or {}).items():
        for ptype, count in planets.items():
            for i in range(int(count)):
                key = f"{system}.{ptype}.{chr(ord('A') + i)}"
                rate = taxes.get(key)
                if rate is not None and (best_rate is None or rate < best_rate):
                    best_rate, best_key = rate, key
    if best_rate is None:
        return cfg.get("tax_rate", 0.15), None
    return best_rate, best_key


DEFAULT_CADENCE = {"haul_days": 1.0, "poco_m3": 35000.0, "buffer_days": 1.0}


def _cadence_from_cfg(cfg):
    """Haul-cadence knobs an import planet has to live within."""
    haul_days = cfg.get("haul_every_days", 1) or 1
    return {
        "haul_days": haul_days,
        "poco_m3": cfg.get("poco_buffer_m3", 35000),
        # How long the planet runs on its own stock between visits. With a
        # daily login you top the planet up from the POCO every day even
        # though the hauler only comes every haul_days.
        "buffer_days": min(cfg.get("on_planet_buffer_days", 1.0), haul_days),
    }


def _import_option(chain, buy_level, market, pi_types, schematics,
                   pg_budget, cpu_budget, tax_rate, sales_tax=0.0,
                   broker_fee=None, cadence=None, n_planets=1):
    """Economics of making one target chain by importing tier<=buy_level inputs.

    Everything is per one unit of finished output, then scaled to a per-planet
    hourly figure via factory throughput. Tax = POCO import on bought inputs +
    export on the finished unit (single-factory-planet assumption), plus market
    sales tax on the Jita dump.

    Throughput is settled BEFORE pricing, because price depends on how much you
    move per haul but throughput doesn't depend on price:

      1. PG/CPU, minus a launchpad and enough Storage Facilities to stage
         `buffer_days` of input+output flow on-planet.
      2. The customs office — each haul-window leg has to fit in 35k m3.
      3. Only then are both Jita legs priced by walking the actual order books
         for one window's quantity (x n_planets, so a portfolio's later planets
         correctly see the price its earlier planets already moved).

    When broker_fee is given, also reports a buy-order-sourced input basis:
    acquiring inputs with your own buy orders filled at ~the Jita top buy
    plus the broker fee (a hub buy order must roughly match Jita's to
    attract sellers; local sellers may fill for less). Upside, not
    guaranteed — the headline net stays on the instant sell-order basis."""
    tid = chain["output_type_id"]
    out_tier = chain["tier"]
    cad = cadence or DEFAULT_CADENCE
    haul_days = cad["haul_days"]
    out_vol = chain.get("volume", 0)

    # Bill of materials at the buy-in tier (per 1 finished unit)
    bom = {}
    _bom_at_tier(tid, 1.0, buy_level, pi_types, schematics, bom)

    flags = []
    input_vol = 0.0
    # POCO tax is linear in the rate, so accumulate it at rate 1.0 and scale.
    # That lets a caller re-price the same build on a different planet's
    # customs office without redoing any of the work.
    tax_unit_per_rate = PI_TAX_BASE.get(out_tier, 70000)  # export leg
    for in_tid, qty in bom.items():
        it = pi_types.get(in_tid, {})
        input_vol += qty * it.get("volume", 0)
        tax_unit_per_rate += qty * PI_TAX_BASE.get(it.get("tier", "P1"), 500) * 0.5

    # ── 1. Throughput: PG/CPU after a launchpad + the storage the buffer needs
    bundle = {}
    _facility_bundle(tid, 1.0, buy_level, pi_types, schematics, bundle)
    units_hr, pg_per, cpu_per, storage_count = _factory_units_per_planet(
        bundle, pg_budget, cpu_budget, input_vol + out_vol, cad["buffer_days"])

    # ── 2. Customs office: each window leg has to fit through the POCO
    poco_scale = 1.0
    legs = [v * units_hr * 24 * haul_days for v in (input_vol, out_vol) if v > 0]
    if units_hr > 0 and legs:
        poco_scale = min([1.0] + [cad["poco_m3"] / v for v in legs if v > 0])
    if poco_scale < 1.0:
        units_hr *= poco_scale
        flags.append(f"POCO BUFFER CAPS AT {poco_scale * 100:.0f}%")
    if cad["buffer_days"] > 0 and units_hr > 0:
        storage_count = _storage_for(
            (input_vol + out_vol) * units_hr * 24 * cad["buffer_days"])
    else:
        storage_count = 0

    # ── 2b. Snap to whole facilities. Rounding up leaves them idling a few
    # percent (duty cycle); if it no longer fits, the build rounds down and
    # throughput drops to what those facilities really make.
    fac_counts, achieved_hr, pg_used, cpu_used = _snap_facilities(
        bundle, units_hr, pg_budget, cpu_budget, storage_count)
    if achieved_hr <= 0:
        units_hr = 0.0
    elif achieved_hr < units_hr:
        units_hr = achieved_hr
    duty_cycle = (units_hr / achieved_hr) if achieved_hr > 0 else 0.0
    # Rounding down moves less cargo, which can free a Storage Facility the
    # build no longer needs — report what it actually takes, not the estimate.
    if units_hr <= 0:
        storage_count = 0
    elif cad["buffer_days"] > 0:
        shrunk = _storage_for(
            (input_vol + out_vol) * units_hr * 24 * cad["buffer_days"])
        if shrunk < storage_count:
            freed = storage_count - shrunk
            pg_used -= freed * FACILITY_COSTS["storage"]["pg"]
            cpu_used -= freed * FACILITY_COSTS["storage"]["cpu"]
            storage_count = shrunk

    # ── 3. Price both legs by walking the book for one haul window
    window_out = units_hr * 24 * haul_days * max(n_planets, 1)

    input_cost = 0.0
    input_cost_buy = 0.0  # buy-order sourcing basis; None if any input lacks a buy
    input_lines = []
    for in_tid, qty in sorted(bom.items(), key=lambda kv: -kv[1]):
        it = pi_types.get(in_tid, {})
        prices = market.get(in_tid, {})
        fill = _jita_fill(prices, qty * window_out, False, haul_days)
        jsell = fill["price"]
        jbuy = prices.get("jita_buy", 0) or 0
        if fill["top"] <= 0:
            flags.append(f"NO JITA SELL ({it.get('name', in_tid)})")
        flags.extend(_depth_flags(fill, f"BUY {it.get('name', in_tid)}"))
        input_cost += qty * jsell
        if input_cost_buy is not None and jbuy > 0:
            input_cost_buy += qty * jbuy
        else:
            input_cost_buy = None
        input_lines.append({
            "name": it.get("name", str(in_tid)),
            "type_id": in_tid,
            "tier": it.get("tier", ""),
            "volume": it.get("volume", 0),
            "qty_per_unit": qty,
            "qty_per_window": qty * window_out,
            "jita_sell": jsell,
            "jita_sell_top": fill["top"],
            "jita_buy": jbuy,
            "slippage": fill["slippage"],
            "book_share": fill["book_share"],
            "cost_per_unit": qty * jsell,
        })

    out_prices = market.get(tid, {})
    sell_fill = _jita_fill(out_prices, window_out, True, haul_days)
    revenue = sell_fill["price"]
    if sell_fill["top"] <= 0:
        rev_basis = "jita_vwap" if revenue > 0 else "none"
        flags.append("NO JITA BUYER (VWAP est.)" if revenue > 0 else "NO JITA MARKET")
    elif sell_fill["filled_frac"] >= 1.0:
        rev_basis = "jita_buy"
    else:
        rev_basis = "jita_buy+vwap"
    flags.extend(_depth_flags(sell_fill, "SELL"))

    tax_per_unit = tax_unit_per_rate * tax_rate
    sales_tax_per_unit = revenue * sales_tax

    pretax_net_per_unit = revenue - input_cost - sales_tax_per_unit
    net_per_unit = pretax_net_per_unit - tax_per_unit
    roi = (net_per_unit / input_cost) if input_cost > 0 else 0.0

    # Buy-order sourcing upside (see docstring)
    buy_sourced_cost = None
    net_buy_sourced = None
    if broker_fee is not None and input_cost_buy is not None:
        buy_sourced_cost = input_cost_buy * (1.0 + broker_fee)
        net_buy_sourced = (revenue - buy_sourced_cost - tax_per_unit
                           - sales_tax_per_unit)

    net_isk_hr = net_per_unit * units_hr
    net_isk_hr_buy_sourced = (net_buy_sourced * units_hr
                              if net_buy_sourced is not None else None)
    daily_import_vol = input_vol * units_hr * 24
    daily_export_vol = out_vol * units_hr * 24
    on_planet_m3 = (input_vol + out_vol) * units_hr * 24 * cad["buffer_days"]

    return {
        "buy_tier": _TIER_NAMES.get(buy_level, "P?"),
        "buy_level": buy_level,
        "input_cost_per_unit": input_cost,
        "input_cost_buy_sourced": buy_sourced_cost,
        "net_per_unit_buy_sourced": net_buy_sourced,
        "tax_per_unit": tax_per_unit,
        "sales_tax_per_unit": sales_tax_per_unit,
        "revenue_per_unit": revenue,
        "revenue_top_of_book": sell_fill["top"],
        "revenue_slippage": sell_fill["slippage"],
        "sell_book_share": sell_fill["book_share"],
        "sell_filled_frac": sell_fill["filled_frac"],
        "revenue_basis": rev_basis,
        "net_per_unit": net_per_unit,
        "pretax_net_per_unit": pretax_net_per_unit,
        "tax_unit_per_rate": tax_unit_per_rate,
        "roi": roi,
        "units_hr_per_planet": units_hr,
        "net_isk_hr_per_planet": net_isk_hr,
        "net_isk_hr_buy_sourced": net_isk_hr_buy_sourced,
        # Whole facilities to actually place on the planet
        "aif_count": fac_counts.get("aif", 0),
        "htif_count": fac_counts.get("htif", 0),
        "storage_count": storage_count,
        "launchpad_count": 1,
        "duty_cycle": duty_cycle,
        "pg_used": pg_used,
        "cpu_used": cpu_used,
        "on_planet_m3": on_planet_m3,
        "on_planet_capacity_m3": (FACILITY_M3["launchpad"]
                                  + storage_count * FACILITY_M3["storage"]),
        "buffer_days": cad["buffer_days"],
        "poco_scale": poco_scale,
        "input_vol_per_unit": input_vol,
        "output_vol_per_unit": out_vol,
        "daily_import_m3": daily_import_vol,
        "daily_export_m3": daily_export_vol,
        "window_units": units_hr * 24 * haul_days,
        "input_lines": input_lines,
        "flags": flags,
        "viable": units_hr > 0 and net_per_unit > 0 and not any(
            f.startswith("NO JITA SELL") or f == "NO JITA MARKET"
            for f in flags),
    }


def import_upgrade_options(min_tier="P3"):
    """Rank products you could make by buying lower-tier PI at Jita, hauling to
    a factory planet, and upgrading. For each target (P3/P4) it evaluates every
    valid buy-in tier and reports the best.

    Reuses the last in-memory run for pi_types/schematics/market/budgets.
    """
    state = _LAST_RUN.get("state")
    if not state:
        return {"error": "No PI run in memory — generate the dossier first."}
    ctx = state["ctx"]
    ectx = state["ectx"]
    cfg = state["cfg"]
    market = ectx["market_prices"]
    pi_types = ctx["pi_types"]
    schematics = ctx["schematics"]
    chains = ctx["chains"]
    pg_budget = state["pg_budget"]
    cpu_budget = state["cpu_budget"]
    tax_rate, tax_planet = _factory_tax_rate(state)
    sales_tax = cfg.get("sales_tax", 0.0)
    broker_fee = cfg.get("broker_fee")
    cad = _cadence_from_cfg(cfg)
    haul_days, poco_m3 = cad["haul_days"], cad["poco_m3"]

    min_level = _TIER_LEVEL.get(min_tier, 3)
    # Full-integration net (per that product's optimizer layout) for reference
    ranked = state.get("ranked") or []
    vi_by_name = {v["chain"]["output_name"]: v for v in ranked}

    products = []
    for tid, chain in chains.items():
        out_level = _TIER_LEVEL.get(chain["tier"], 0)
        if out_level < min_level:
            continue
        options = []
        for buy_level in range(1, out_level):  # P1..(target-1)
            opt = _import_option(chain, buy_level, market, pi_types, schematics,
                                 pg_budget, cpu_budget, tax_rate, sales_tax,
                                 broker_fee, cad)
            options.append(opt)
        if not options:
            continue
        # Best = highest per-planet net ISK/hr among viable; else best net/unit
        viable_opts = [o for o in options if o["viable"]]
        pool = viable_opts or options
        best = max(pool, key=lambda o: o["net_isk_hr_per_planet"])
        vi = vi_by_name.get(chain["output_name"])
        products.append({
            "output_name": chain["output_name"],
            "output_type_id": tid,
            "tier": chain["tier"],
            "volume": chain.get("volume", 0),
            "best": best,
            "options": options,
            "full_vi_net_isk_hr": (vi.get("net_isk_hr", 0) if vi else 0),
            "full_vi_planet_count": (
                vi.get("planet_count", len(vi.get("planets_used", [])))
                if vi else 0),
            "full_vi_viable": bool(vi.get("viable")) if vi else False,
        })

    # Rank products by their best per-planet net ISK/hr
    products.sort(key=lambda p: p["best"]["net_isk_hr_per_planet"], reverse=True)
    return {
        "min_tier": min_tier,
        "tax_rate": tax_rate,
        "factory_tax_planet": tax_planet,
        "sales_tax": sales_tax,
        "broker_fee": broker_fee,
        "haul_every_days": haul_days,
        "poco_buffer_m3": poco_m3,
        "on_planet_buffer_days": cad["buffer_days"],
        "pg_budget": pg_budget,
        "cpu_budget": cpu_budget,
        "sell_basis": "jita_buy",
        "buy_basis": "jita_sell",
        "products": products,
    }


DEFAULT_ISK_PER_HAUL_MIN = 100000.0  # opportunity value of a minute of hauling


def best_pi_plays(isk_per_haul_min=None, min_tier="P1"):
    """Head-to-head: for every product, put the extract-yourself path and the
    import-and-upgrade path on ONE consistent basis and rank them together.

    Both strategies sell output at the Jita buy order, and both have their
    hauling priced into net: daily haul minutes (extraction's local gather
    circuit + delivery to Jita; import's Jita round-trips) times a configurable
    ISK-per-haul-minute opportunity cost. This answers "what is the single best
    thing to do with a PI planet, extract or import?".

    Reuses the last in-memory run. Ranked by total net ISK/hr after haul.
    """
    state = _LAST_RUN.get("state")
    if not state:
        return {"error": "No PI run in memory — generate the dossier first."}
    ctx = state["ctx"]
    ectx = state["ectx"]
    cfg = state["cfg"]
    market = ectx["market_prices"]
    pi_types = ctx["pi_types"]
    schematics = ctx["schematics"]
    chains = ctx["chains"]
    pg_budget = state["pg_budget"]
    cpu_budget = state["cpu_budget"]
    tax_rate, tax_planet = _factory_tax_rate(state)
    sales_tax = cfg.get("sales_tax", 0.0)
    broker_fee = cfg.get("broker_fee")
    hauler_m3 = cfg.get("hauler_m3", 9000) or 9000
    cad = _cadence_from_cfg(cfg)
    haul_days, poco_m3 = cad["haul_days"], cad["poco_m3"]
    jita_jumps = ectx.get("jita_jumps", 15)
    haul = cfg.get("haul", {"sec_per_jump": 45, "sec_per_station": 180})
    if isk_per_haul_min is None:
        isk_per_haul_min = DEFAULT_ISK_PER_HAUL_MIN
    jita_rt_min = (jita_jumps * 2 * haul.get("sec_per_jump", 45)
                   + 2 * haul.get("sec_per_station", 180)) / 60.0

    def _trips(m3):
        return math.ceil(m3 / hauler_m3) if (m3 > 0 and hauler_m3 > 0) else 0

    min_level = _TIER_LEVEL.get(min_tier, 1)
    ranked = state.get("ranked") or []
    vi_by_name = {v["chain"]["output_name"]: v for v in ranked}

    plays = []
    for tid, chain in chains.items():
        out_level = _TIER_LEVEL.get(chain["tier"], 0)
        if out_level < min_level:
            continue
        name = chain["output_name"]
        tier = chain["tier"]
        vol = chain.get("volume", 0)
        out_prices = market.get(tid, {})

        options = []

        # ── Extraction (full P0→ path), re-priced at Jita buy ──
        vi = vi_by_name.get(name)
        if vi is not None:
            units = vi.get("units_hr", 0)
            tax_hr = vi.get("tax_per_hr", 0)
            planets = vi.get("planet_count", len(vi.get("planets_used", []))) or 1
            # One haul dumps a whole window's production at once — walk the
            # book for that quantity instead of trusting the top order.
            fill = _jita_fill(out_prices, units * 24 * haul_days, True, haul_days)
            rev = fill["price"]
            basis = ("jita_buy" if fill["top"] > 0 and fill["filled_frac"] >= 1.0
                     else "jita_buy+vwap" if fill["top"] > 0 else "jita_vwap")
            gross_hr = units * rev
            net_hr = gross_hr * (1.0 - sales_tax) - tax_hr
            # One run every haul_days: local circuit + delivery amortised
            local_min = vi.get("haul_minutes_per_day", 0) / haul_days
            deliver_min = (_trips(units * 24 * haul_days * vol)
                           * jita_rt_min / haul_days)
            haul_min = local_min + deliver_min
            haul_cost_hr = haul_min * isk_per_haul_min / 24.0
            net_after = net_hr - haul_cost_hr
            ex_flags = list(vi.get("flags", []))
            ex_flags.extend(_depth_flags(fill, "SELL"))
            if units * 24 * haul_days * vol / planets > poco_m3:
                ex_flags.append(f"{haul_days:g}d OUTPUT EXCEEDS POCO BUFFER")
            options.append({
                "strategy": "extract", "label": "Extract (P0→)",
                "buy_tier": "—", "planets": planets, "units_hr": units,
                "gross_isk_hr": gross_hr, "tax_per_hr": tax_hr,
                "input_cost_isk_hr": 0.0, "net_isk_hr": net_hr,
                "haul_min_per_day": haul_min, "haul_cost_isk_hr": haul_cost_hr,
                "net_after_haul_isk_hr": net_after,
                "net_after_haul_per_planet": net_after / planets if planets else net_after,
                "revenue_basis": basis, "roi": None,
                "daily_import_m3": 0.0, "daily_export_m3": units * 24 * vol,
                "viable": bool(vi.get("viable")) and units > 0,
                "flags": ex_flags,
            })

        # ── Import-and-upgrade (best buy-in tier after haul) ──
        if out_level >= 2:
            best_imp = None
            for buy_level in range(1, out_level):
                opt = _import_option(chain, buy_level, market, pi_types,
                                     schematics, pg_budget, cpu_budget,
                                     tax_rate, sales_tax, broker_fee, cad)
                imp_trips = max(_trips(opt["daily_import_m3"] * haul_days),
                                _trips(opt["daily_export_m3"] * haul_days))
                imp_min = imp_trips * jita_rt_min / haul_days
                haul_cost_hr = imp_min * isk_per_haul_min / 24.0
                net_after = opt["net_isk_hr_per_planet"] - haul_cost_hr
                opt["_haul_min"] = imp_min
                opt["_haul_cost_hr"] = haul_cost_hr
                opt["_net_after"] = net_after
                if best_imp is None or net_after > best_imp["_net_after"]:
                    best_imp = opt
            if best_imp is not None:
                u = best_imp["units_hr_per_planet"]
                options.append({
                    "strategy": "import",
                    "label": f"Import {best_imp['buy_tier']}→{tier}",
                    "buy_tier": best_imp["buy_tier"], "planets": 1, "units_hr": u,
                    "gross_isk_hr": best_imp["revenue_per_unit"] * u,
                    "tax_per_hr": best_imp["tax_per_unit"] * u,
                    "input_cost_isk_hr": best_imp["input_cost_per_unit"] * u,
                    "net_isk_hr": best_imp["net_isk_hr_per_planet"],
                    "net_isk_hr_buy_sourced": best_imp.get("net_isk_hr_buy_sourced"),
                    "haul_min_per_day": best_imp["_haul_min"],
                    "haul_cost_isk_hr": best_imp["_haul_cost_hr"],
                    "net_after_haul_isk_hr": best_imp["_net_after"],
                    "net_after_haul_per_planet": best_imp["_net_after"],
                    "revenue_basis": best_imp["revenue_basis"],
                    "roi": best_imp["roi"],
                    "daily_import_m3": best_imp["daily_import_m3"],
                    "daily_export_m3": best_imp["daily_export_m3"],
                    "storage_count": best_imp["storage_count"],
                    "aif_count": best_imp["aif_count"],
                    "htif_count": best_imp["htif_count"],
                    "on_planet_m3": best_imp["on_planet_m3"],
                    "on_planet_capacity_m3": best_imp["on_planet_capacity_m3"],
                    "sell_book_share": best_imp["sell_book_share"],
                    "revenue_slippage": best_imp["revenue_slippage"],
                    "buy_level": best_imp["buy_level"],
                    "viable": best_imp["viable"], "flags": best_imp["flags"],
                })

        if not options:
            continue
        winner = max(options, key=lambda o: o["net_after_haul_isk_hr"])
        plays.append({
            "output_name": name, "output_type_id": tid, "tier": tier,
            "volume": vol, "options": options,
            "winner_strategy": winner["strategy"],
            "best_net_after_haul_isk_hr": winner["net_after_haul_isk_hr"],
            "best_net_after_haul_per_planet": winner["net_after_haul_per_planet"],
        })

    # Rank by total net ISK/hr after haul (per user's chosen headline metric)
    plays.sort(key=lambda p: p["best_net_after_haul_isk_hr"], reverse=True)
    return {
        "min_tier": min_tier, "tax_rate": tax_rate,
        "factory_tax_planet": tax_planet, "sales_tax": sales_tax,
        "hauler_m3": hauler_m3, "haul_every_days": haul_days,
        "poco_buffer_m3": poco_m3,
        "on_planet_buffer_days": cad["buffer_days"],
        "isk_per_haul_min": isk_per_haul_min, "jita_jumps": jita_jumps,
        "jita_round_trip_min": jita_rt_min, "sell_basis": "jita_buy",
        "plays": plays,
    }


def _factory_sites(state, limit):
    """Candidate POCOs for factory planets, cheapest first.

    A factory extracts nothing, so tax is the only siting criterion — but P4
    needs a High-Tech Production Plant, which only exists on Barren and
    Temperate planets. The pool therefore carries the cheapest `limit` planets
    overall AND the cheapest `limit` that can host P4, because the cheapest
    planet in the system may well be a Gas one that no P4 can use.
    """
    ctx = state.get("ctx") or {}
    cfg = state.get("cfg") or {}
    taxes = ctx.get("planet_taxes") or {}
    sites = []
    for system, planets in (ctx.get("inv") or {}).items():
        for ptype, count in planets.items():
            for i in range(int(count)):
                key = f"{system}.{ptype}.{chr(ord('A') + i)}"
                rate = taxes.get(key)
                if rate is not None:
                    sites.append({"key": key, "system": system, "type": ptype,
                                  "tax_rate": rate,
                                  "htif_ok": ptype in HTIF_PLANET_TYPES})
    sites.sort(key=lambda s: s["tax_rate"])
    pool = sites[:limit] + [s for s in sites if s["htif_ok"]][:limit]
    seen, out = set(), []
    for s in pool:
        if s["key"] not in seen:
            seen.add(s["key"])
            out.append(s)
    # Nothing scouted (or too few) — fall back to the configured default rate.
    # Assume such a planet can host P4 so the search is not blocked by a gap
    # in scouting; the rate is the pessimistic part.
    while len(out) < limit:
        out.append({"key": None, "system": "", "type": "",
                    "tax_rate": cfg.get("tax_rate", 0.15), "htif_ok": True})
    return sorted(out, key=lambda s: s["tax_rate"])


def _assign_sites(planets, sites):
    """Cheapest total POCO tax for these planets, and the site each one gets.

    planets: [(tax_isk_hr_per_unit_of_rate, needs_htif)]
    Restricted (P4) planets can only take Barren/Temperate sites, so this is a
    small constrained assignment, not a plain sort — the cheapest office may be
    a planet type the build cannot use. Exact: at most a handful of splits, and
    within each group cheap rates pair with big tax bills (rearrangement).

    Returns (total_tax_isk_hr, {planet_index: site}) or None if it cannot fit.
    """
    capable = sorted([s for s in sites if s["htif_ok"]],
                     key=lambda s: s["tax_rate"])
    other = sorted([s for s in sites if not s["htif_ok"]],
                   key=lambda s: s["tax_rate"])
    restricted = sorted([(sens, i) for i, (sens, need) in enumerate(planets)
                         if need], reverse=True)
    free = sorted([(sens, i) for i, (sens, need) in enumerate(planets)
                   if not need], reverse=True)
    if len(restricted) > len(capable):
        return None
    if len(planets) > len(capable) + len(other):
        return None

    best = None
    max_extra = min(len(free), len(capable) - len(restricted))
    for j in range(max_extra + 1):
        if len(free) - j > len(other):
            continue  # not enough non-capable sites for the rest
        cap_used = capable[:len(restricted) + j]
        oth_used = other[:len(free) - j]
        for combo in itertools.combinations(range(len(cap_used)),
                                            len(restricted)):
            taken = set(combo)
            r_sites = sorted((cap_used[i] for i in taken),
                             key=lambda s: s["tax_rate"])
            u_sites = sorted([cap_used[i] for i in range(len(cap_used))
                              if i not in taken] + list(oth_used),
                             key=lambda s: s["tax_rate"])
            cost = 0.0
            picked = {}
            for (sens, idx), site in zip(restricted, r_sites):
                cost += sens * site["tax_rate"]
                picked[idx] = site
            for (sens, idx), site in zip(free, u_sites):
                cost += sens * site["tax_rate"]
                picked[idx] = site
            if best is None or cost < best[0]:
                best = (cost, picked)
    return best


# Candidate lines kept per ranking criterion (net, net per ISK, net per m3).
# The union is what the allocation search runs over.
RUN_PLAN_MAX_LINES = 8


def pi_run_plan(isk_per_haul_min=None, max_planets=None,
                capital=None, max_trips=None):
    """The do-this-tonight sheet: what to build, buy, drop, and bring back.

    Fills your planet slots greedily, one at a time. Each candidate is re-priced
    for the portfolio size being considered, so the second planet on a product
    sees the Jita book its first planet already ate through — a thin product
    stops winning slots and the runner-up takes over. That's what makes the
    result a mix rather than five copies of whatever ranked first.

    Everything is quoted per haul run (haul_every_days) because that's the unit
    of work you actually do.

    # ponytail: allocates import/factory planets only. Extraction layouts need
    # specific planets from the allocator, so the best extraction play is
    # reported alongside for comparison rather than competing for slots.
    """
    state = _LAST_RUN.get("state")
    if not state:
        return {"error": "No PI run in memory — generate the dossier first."}
    ctx, ectx, cfg = state["ctx"], state["ectx"], state["cfg"]
    market = ectx["market_prices"]
    pi_types = ctx["pi_types"]
    schematics = ctx["schematics"]
    chains = ctx["chains"]
    pg_budget, cpu_budget = state["pg_budget"], state["cpu_budget"]
    tax_rate, tax_planet = _factory_tax_rate(state)
    sales_tax = cfg.get("sales_tax", 0.0)
    broker_fee = cfg.get("broker_fee")
    cad = _cadence_from_cfg(cfg)
    haul_days = cad["haul_days"]
    hauler_m3 = cfg.get("hauler_m3", 9000) or 9000
    slots = int(max_planets or cfg.get("max_planets", 5))
    jita_jumps = ectx.get("jita_jumps", 15)
    haul = cfg.get("haul", {"sec_per_jump": 45, "sec_per_station": 180})
    if isk_per_haul_min is None:
        isk_per_haul_min = DEFAULT_ISK_PER_HAUL_MIN
    jita_rt_min = (jita_jumps * 2 * haul.get("sec_per_jump", 45)
                   + 2 * haul.get("sec_per_station", 180)) / 60.0

    sites = _factory_sites(state, slots)
    site_rates = sorted(s["tax_rate"] for s in sites)
    ref_rate = site_rates[0] if site_rates else tax_rate

    def _trips(m3):
        return math.ceil(m3 / hauler_m3) if (m3 > 0 and hauler_m3 > 0) else 0

    def _opt(chain, lvl, n):
        return _import_option(chain, lvl, market, pi_types, schematics,
                              pg_budget, cpu_budget, ref_rate, sales_tax,
                              broker_fee, cad, n_planets=n)

    # ── Candidate lines: one per (product, buy-in tier).
    # The ceilings are deliberately NOT applied here. Pruning the candidate
    # set by a ceiling makes the set change as the ceiling moves, and then a
    # tighter budget can hand back a *better* plan than a loose one. Ceilings
    # belong in _score, where they only ever shrink the feasible region.
    lines = []
    for tid, chain in chains.items():
        if _TIER_LEVEL.get(chain["tier"], 0) < 2:
            continue
        for lvl in range(1, _TIER_LEVEL.get(chain["tier"], 0)):
            o = _opt(chain, lvl, 1)
            if not o["viable"]:
                continue
            lines.append({
                "tid": tid, "chain": chain, "buy_level": lvl, "opts": {1: o},
                "net1": o["net_isk_hr_per_planet"],
                "spend1": o["input_cost_per_unit"] * o["window_units"],
                "cargo1": max(o["daily_import_m3"],
                              o["daily_export_m3"]) * haul_days,
            })

    # Which line is worth a slot depends on which ceiling bites: planet slots,
    # ISK, or cargo space. Keep the leaders under each, because ranking on net
    # alone starves the search exactly when capital is the binding constraint.
    def _per(attr):
        return lambda l: -(l["net1"] / l[attr]) if l[attr] > 0 else 0.0
    keep = {}
    for key in (lambda l: -l["net1"], _per("spend1"), _per("cargo1")):
        for line in sorted(lines, key=key)[:RUN_PLAN_MAX_LINES]:
            keep[(line["tid"], line["buy_level"])] = line
    lines = sorted(keep.values(), key=lambda l: -l["net1"])

    for line in lines:  # depth-aware pricing at each portfolio size
        for n in range(2, slots + 1):
            line["opts"][n] = _opt(line["chain"], line["buy_level"], n)

    def _score(alloc):
        """Exact net ISK/hr after haul for an allocation [(line, count), ...].

        Returns None when the allocation cannot be built at all — over budget,
        over the trip limit, or needing more P4-capable planets than exist.
        """
        spend = imp_m3 = exp_m3 = pretax = 0.0
        demand = []  # (tax per unit of POCO rate, needs a Barren/Temperate)
        for line, n in alloc:
            o = line["opts"][n]
            spend += o["input_cost_per_unit"] * o["window_units"] * n
            imp_m3 += o["daily_import_m3"] * haul_days * n
            exp_m3 += o["daily_export_m3"] * haul_days * n
            pretax += o["pretax_net_per_unit"] * o["units_hr_per_planet"] * n
            demand += [(o["tax_unit_per_rate"] * o["units_hr_per_planet"],
                        o["htif_count"] > 0)] * n
        if capital is not None and spend > capital:
            return None
        trips = max(_trips(imp_m3), _trips(exp_m3))
        if max_trips is not None and trips > max_trips:
            return None
        placed = _assign_sites(demand, sites)
        if placed is None:
            return None  # not enough Barren/Temperate planets for the P4s
        tax_hr, picked = placed
        haul_cost = trips * jita_rt_min / haul_days * isk_per_haul_min / 24.0
        return {"net": pretax - tax_hr - haul_cost, "spend": spend,
                "import_m3": imp_m3, "export_m3": exp_m3, "trips": trips,
                "tax_isk_hr": tax_hr, "haul_cost_isk_hr": haul_cost,
                "placed": picked, "demand": demand}

    # ── Exhaustive search over allocations. Spend and cargo only ever grow as
    # planets are added, so an infeasible prefix prunes its whole subtree —
    # under a real budget that kills almost everything immediately.
    best_alloc, best_score = [], None

    def _search(start, slots_left, alloc):
        nonlocal best_alloc, best_score
        if alloc:
            sc = _score(alloc)
            if sc is None:
                return  # infeasible, and adding planets cannot fix it
            if best_score is None or sc["net"] > best_score["net"]:
                best_alloc, best_score = list(alloc), sc
        if slots_left == 0:
            return
        used = {line["tid"] for line, _ in alloc}
        for i in range(start, len(lines)):
            if lines[i]["tid"] in used:
                continue  # one buy-in tier per product, or depth double-counts
            for n in range(1, slots_left + 1):
                alloc.append((lines[i], n))
                _search(i + 1, slots_left - n, alloc)
                alloc.pop()

    _search(0, slots, [])

    counts = {line["tid"]: n for line, n in best_alloc}
    chosen = {line["tid"]: line["opts"][n] for line, n in best_alloc}

    # Real planets, in the same order _score built its demand list, so the
    # reported siting is exactly the one the winning score was computed from.
    sites_for = {}
    if best_score:
        order = [line["tid"] for line, n in best_alloc for _ in range(n)]
        for idx, tid in enumerate(order):
            site = best_score["placed"].get(idx)
            if site:
                sites_for.setdefault(tid, []).append(site)
        for tid in sites_for:
            sites_for[tid].sort(key=lambda s: s["tax_rate"])

    # ── Build the per-planet + shopping output ──
    shopping = {}
    planets, warnings = [], []
    import_m3 = export_m3 = 0.0
    spend_per_run = revenue_per_run = 0.0
    total_net_isk_hr = 0.0

    def _product_net(tid):
        """Net ISK/hr for a product, taxed at the customs offices it got."""
        o = chosen[tid]
        n = counts[tid]
        gross = o["pretax_net_per_unit"] * o["units_hr_per_planet"] * n
        tax = sum(o["tax_unit_per_rate"] * o["units_hr_per_planet"]
                  * s["tax_rate"] for s in sites_for.get(tid, []))
        return gross - tax, tax

    for tid in sorted(chosen, key=lambda t: -_product_net(t)[0]):
        opt = chosen[tid]
        chain = chains[tid]
        n = counts[tid]
        net_hr, tax_hr = _product_net(tid)
        per_planet_units = opt["window_units"]          # units per haul run
        run_units = per_planet_units * n

        inputs = []
        for line in opt["input_lines"]:
            it_units = line["qty_per_unit"] * per_planet_units
            vol_each = line["volume"]
            inputs.append({
                "name": line["name"],
                "tier": line["tier"],
                "units_per_planet_per_run": it_units,
                "units_per_planet_per_day": it_units / haul_days,
                "m3_per_planet_per_run": it_units * vol_each,
                "isk_per_planet_per_run": it_units * line["jita_sell"],
                "jita_sell": line["jita_sell"],
                "slippage": line["slippage"],
            })
            agg = shopping.setdefault(line["name"], {
                "name": line["name"], "tier": line["tier"], "units": 0.0,
                "m3": 0.0, "isk": 0.0, "jita_sell": line["jita_sell"],
                "slippage": line["slippage"], "book_share": line["book_share"],
            })
            agg["units"] += it_units * n
            agg["m3"] += it_units * vol_each * n
            agg["isk"] += it_units * line["jita_sell"] * n

        p_import_m3 = opt["daily_import_m3"] * haul_days * n
        p_export_m3 = opt["daily_export_m3"] * haul_days * n
        import_m3 += p_import_m3
        export_m3 += p_export_m3
        spend_per_run += opt["input_cost_per_unit"] * run_units
        revenue_per_run += opt["revenue_per_unit"] * run_units
        total_net_isk_hr += net_hr

        if opt["on_planet_m3"] > opt["on_planet_capacity_m3"] + 1e-6:
            warnings.append(f"{chain['output_name']}: on-planet buffer "
                            f"{opt['on_planet_m3']:.0f} m3 exceeds built capacity")
        for f in opt["flags"]:
            warnings.append(f"{chain['output_name']}: {f}")

        planets.append({
            "output_name": chain["output_name"],
            "output_type_id": tid,
            "tier": chain["tier"],
            "buy_tier": opt["buy_tier"],
            "planet_count": n,
            "sites": [{"planet": s["key"], "system": s["system"],
                       "type": s["type"], "tax_rate": s["tax_rate"]}
                      for s in sites_for.get(tid, [])],
            "facilities": {
                "aif": opt["aif_count"], "htif": opt["htif_count"],
                "launchpad": 1, "storage": opt["storage_count"],
            },
            "duty_cycle": opt["duty_cycle"],
            "pg_used": opt["pg_used"], "pg_budget": pg_budget,
            "cpu_used": opt["cpu_used"], "cpu_budget": cpu_budget,
            "units_hr_per_planet": opt["units_hr_per_planet"],
            "units_per_planet_per_run": per_planet_units,
            "units_per_run": run_units,
            "inputs": inputs,
            "on_planet_m3": opt["on_planet_m3"],
            "on_planet_capacity_m3": opt["on_planet_capacity_m3"],
            "import_m3_per_run": p_import_m3,
            "export_m3_per_run": p_export_m3,
            "spend_per_run": opt["input_cost_per_unit"] * run_units,
            "net_isk_hr_per_planet": net_hr / n if n else 0.0,
            "total_net_isk_hr": net_hr,
            "tax_isk_hr": tax_hr,
            "net_per_unit": opt["net_per_unit"],
            "roi": opt["roi"],
            "revenue_per_unit": opt["revenue_per_unit"],
            "revenue_top_of_book": opt["revenue_top_of_book"],
            "revenue_slippage": opt["revenue_slippage"],
            "sell_book_share": opt["sell_book_share"],
            "poco_scale": opt["poco_scale"],
            "flags": opt["flags"],
        })

    trips = max(_trips(import_m3), _trips(export_m3))
    haul_cost_isk_hr = trips * jita_rt_min / haul_days * isk_per_haul_min / 24.0
    total_net_isk_hr -= haul_cost_isk_hr
    if trips > 1:
        warnings.append(f"One run needs {trips} hauler trips "
                        f"({max(import_m3, export_m3):,.0f} m3 vs "
                        f"{hauler_m3:,.0f} m3 hold)")
    if not planets:
        warnings.append(
            "Nothing fits the ceilings. Raise the budget or the trip limit, "
            "or drop to a lower-tier product.")
    if capital is not None and planets:
        warnings.append(f"Uses {spend_per_run / capital:.0%} of the "
                        f"{capital:,.0f} ISK budget "
                        f"({capital - spend_per_run:,.0f} idle).")

    # Reference: the best extraction play, for the extract-vs-import sanity check
    bp = best_pi_plays(isk_per_haul_min=isk_per_haul_min, min_tier="P1")
    extraction_alt = None
    for play in bp.get("plays", []):
        for o in play["options"]:
            if o["strategy"] == "extract" and o["viable"]:
                extraction_alt = {
                    "output_name": play["output_name"], "tier": play["tier"],
                    "planets": o["planets"],
                    "net_after_haul_isk_hr": o["net_after_haul_isk_hr"],
                    "net_after_haul_per_planet": o["net_after_haul_per_planet"],
                }
                break
        if extraction_alt:
            break

    return {
        "haul_every_days": haul_days,
        "on_planet_buffer_days": cad["buffer_days"],
        "poco_buffer_m3": cad["poco_m3"],
        "hauler_m3": hauler_m3,
        "jita_jumps": jita_jumps,
        "jita_round_trip_min": jita_rt_min,
        "isk_per_haul_min": isk_per_haul_min,
        "capital": capital,
        "max_trips": max_trips,
        "tax_rate": ref_rate,
        "factory_tax_planet": tax_planet,
        "site_tax_rates": site_rates,
        "sales_tax": sales_tax,
        "pg_budget": pg_budget,
        "cpu_budget": cpu_budget,
        "slots": slots,
        "slots_used": sum(counts.values()),
        "planets": planets,
        "shopping_list": sorted(shopping.values(), key=lambda s: -s["isk"]),
        "spend_per_run": spend_per_run,
        "revenue_per_run": revenue_per_run,
        "net_per_run": revenue_per_run - spend_per_run,
        "total_net_isk_hr": total_net_isk_hr,
        "haul_cost_isk_hr": haul_cost_isk_hr,
        "import_m3_per_run": import_m3,
        "export_m3_per_run": export_m3,
        "trips_per_run": trips,
        "extraction_alternative": extraction_alt,
        "warnings": warnings,
    }


# ── Web API entry point ───────────────────────────────────────

def generate_pi_dossier_data(overrides=None):
    """Run the full PI dossier pipeline and return JSON-serializable data.

    overrides: optional dict to override pi_config values
               (tax_rate, hauler_m3, max_haul_minutes, max_market_jumps)
    """
    # Load configs
    char_info, pi_skills = load_skills()
    cfg = load_pi_config()
    planet_inv = load_planet_inventory()
    extraction_rates = load_extraction_rates()
    density_data = load_planet_density()

    # Apply overrides
    if overrides:
        for k in ("tax_rate", "hauler_m3", "max_haul_minutes", "max_market_jumps"):
            if k in overrides:
                cfg[k] = overrides[k]
        if "sell_at_jita" in overrides:
            cfg["sell_at_jita"] = bool(overrides["sell_at_jita"])

    # Resolve home system
    home_system_id = esi.search_system_id(cfg["home_system"])
    if not home_system_id:
        return {"error": f"Home system '{cfg['home_system']}' not found in ESI."}

    # Systems to route around: ignored inventory systems + cfg avoid list.
    # Affects every jump/route lookup (market range, jita haul, layout TSP).
    avoid_names = {s for s, p in planet_inv.items() if p.get("_ignored")}
    avoid_names.update(cfg["avoid_systems"])
    avoid_names.discard(cfg["home_system"])
    _AVOID_IDS.clear()
    for name in avoid_names:
        sid = esi.search_system_id(name)
        if sid and sid != home_system_id:
            _AVOID_IDS.add(sid)
    if avoid_names:
        print(f"  PI Dossier: routing around {', '.join(sorted(avoid_names))}")

    local_region_key = None
    local_region_id = None
    for rk, r in esi.REGIONS.items():
        if rk == "verge":
            local_region_key = rk
            local_region_id = r["id"]
            break
    if not local_region_id:
        local_region_id = esi.REGIONS["verge"]["id"]
        local_region_key = "verge"

    print("  PI Dossier: fetching PI type data...")
    pi_types, by_name = fetch_pi_types(progress=True)
    if not pi_types:
        return {"error": "Failed to fetch PI type data from EVE Ref."}

    print("  PI Dossier: fetching schematics...")
    schematics = fetch_schematics(pi_types, progress=True)
    if not schematics:
        return {"error": "Failed to fetch PI schematic data from EVE Ref."}

    print("  PI Dossier: building chain graph...")
    chains = build_chain_graph(pi_types, schematics)

    print("  PI Dossier: fetching market data...")
    market_prices = fetch_pi_market(pi_types, local_region_id, home_system_id,
                                    cfg["max_market_jumps"], progress=True)

    # Build jump matrix and system positions
    print("  PI Dossier: building jump matrix...")
    all_systems = list(set(list(planet_inv.keys()) + [cfg["home_system"]]))
    system_ids, matrix = esi.build_jump_matrix(all_systems, avoid=_AVOID_IDS)
    system_positions = esi.get_system_positions(system_ids)
    home_id = system_ids.get(cfg["home_system"])
    planet_taxes = load_planet_taxes()

    print("  PI Dossier: computing layouts and economics...")
    calc_inv = active_inventory(planet_inv)
    ctx = _build_analysis_ctx(chains, pi_types, schematics, calc_inv,
                              extraction_rates, density_data, cfg,
                              planet_taxes)
    calibration = ctx["calibration"]
    if calibration["points"]:
        fit_note = (f", power fit at {calibration['fit']['weight']:.0%} weight"
                    if calibration["fit"] else "")
        print(f"  PI Dossier: density model calibrated from "
              f"{calibration['points']} observed rate(s) "
              f"(x{calibration['scale']:.2f} vs base table{fit_note})")
    ectx = _build_econ_ctx(market_prices, cfg, pi_types, matrix, home_id,
                           system_ids, planet_taxes)
    viable = find_viable_chains(chains, pi_types, schematics,
                                calc_inv, extraction_rates, density_data,
                                cfg, planet_taxes, ctx=ctx)
    compute_economics(viable, market_prices, cfg, pi_types,
                      matrix, home_id, system_ids, planet_taxes, ectx=ectx)

    ranked = rank_chains(viable)
    layouts = allocate_system_first(ranked, calc_inv, matrix, home_id,
                                    system_ids, cfg, market_prices, ctx, ectx)

    # Add sell systems to map so route is fully visible
    for layout in layouts:
        sell_name = layout.get("route", {}).get("sell_system", "")
        if sell_name and sell_name not in system_ids:
            sell_id = esi.search_system_id(sell_name)
            if sell_id:
                system_ids[sell_name] = sell_id
                # Add to matrix — distances to existing systems
                for other_id in list({v for v in system_ids.values()
                                      if v != sell_id}):
                    if (sell_id, other_id) not in matrix:
                        j = esi.get_jump_count(sell_id, other_id,
                                               avoid=_AVOID_IDS)
                        matrix[(sell_id, other_id)] = j
                        matrix[(other_id, sell_id)] = j
                matrix[(sell_id, sell_id)] = 0
    system_positions = esi.get_system_positions(system_ids)

    # Generate system map SVG
    map_svg = _generate_system_map_svg(planet_inv, matrix, system_ids,
                                        system_positions, home_id, layouts)

    projections = compute_projections(ranked, pi_skills, cfg)

    markdown = render_markdown(layouts, ranked, cfg, char_info, pi_skills,
                               projections, market_prices, pi_types,
                               ignored_systems=sorted(set(planet_inv) - set(calc_inv)),
                               calibration=calibration)

    # Build JSON response
    chains_json = []
    for vc in ranked:
        chain = vc["chain"]
        chains_json.append({
            "output_type_id": chain["output_type_id"],
            "output_name": chain["output_name"],
            "tier": chain["tier"],
            "layout_type": vc.get("layout_type", ""),
            "planet_count": vc.get("planet_count", len(vc.get("planets_used", []))),
            "unique_p0_count": vc.get("unique_p0_count", 0),
            "units_hr": vc.get("units_hr", 0),
            "volume_hr": vc.get("volume_hr", 0),
            "local_sustained": vc.get("local_sustained", 0),
            "local_buy_price": vc.get("local_buy_price", 0),
            "local_real_depth": vc.get("local_real_depth", 0),
            "local_real_buy_days": vc.get("local_real_buy_days", 0),
            "local_buyer_system": vc.get("local_buyer_system", ""),
            "local_buyer_jumps": vc.get("local_buyer_jumps", 0),
            "local_vwap": vc.get("local_vwap", 0),
            "local_avg_daily_vol": vc.get("local_avg_daily_vol", 0),
            "local_active_days": vc.get("local_active_days", 0),
            "local_order_count": vc.get("local_order_count", 0),
            "jita_vwap": vc.get("jita_vwap", 0),
            "jita_buy_price": vc.get("jita_buy_price", 0),
            "jita_avg_daily_vol": vc.get("jita_avg_daily_vol", 0),
            "gross_isk_hr": vc.get("gross_isk_hr", 0),
            "tax_per_hr": vc.get("tax_per_hr", 0),
            "net_isk_hr": vc.get("net_isk_hr", 0),
            "activity_factor": vc.get("activity_factor", 1.0),
            "adjusted_net_isk_hr": vc.get("adjusted_net_isk_hr", 0),
            "haul_minutes_per_day": vc.get("haul_minutes_per_day", 0),
            "viable": vc.get("viable", False),
            "rate_sources": vc.get("rate_sources", []),
            "flags": vc.get("flags", []),
            "planets_used": [
                {"system": p.get("system", ""), "type": p.get("type", ""),
                 "role": p.get("role", ""),
                 "rate_details": p.get("rate_details", p.get("rate_detail", ""))}
                for p in vc.get("planets_used", [])
            ],
        })

    pg_budget = cfg["pg_budget"]
    cpu_budget = cfg["cpu_budget"]

    layouts_json = []
    for layout in layouts:
        layout_entries = [_chain_entry_json(vc, pg_budget, cpu_budget)
                          for vc in layout["allocated"]]
        layouts_json.append({
            "strategy": layout["strategy"],
            "total_net": layout["total_net"],
            "allocated": layout_entries,
            "route": layout.get("route", {}),
        })

    # Stash the in-memory run so the web UI can recompute a single chain's
    # build (manual BIF/AIF overrides) without re-running the whole optimizer.
    _LAST_RUN["state"] = {
        "ctx": ctx, "ectx": ectx, "cfg": cfg, "layouts": layouts,
        "ranked": ranked,
        "pg_budget": pg_budget, "cpu_budget": cpu_budget,
    }

    return {
        "char_info": char_info,
        "pi_skills": pi_skills,
        "config": {
            "home_system": cfg["home_system"],
            "tax_rate": cfg["tax_rate"],
            "sales_tax": cfg["sales_tax"],
            "hauler_m3": cfg["hauler_m3"],
            "max_haul_minutes": cfg["max_haul_minutes"],
            "max_market_jumps": cfg["max_market_jumps"],
            "pg_budget": cfg["pg_budget"],
            "cpu_budget": cfg["cpu_budget"],
            "max_planets": cfg["max_planets"],
            "sell_at_jita": cfg.get("sell_at_jita", False),
        },
        "planet_inventory": planet_inv,
        "extraction_rates": extraction_rates,
        "density_data": density_data,
        "calibration": calibration,
        "chains": chains_json,
        "layouts": layouts_json,
        "system_map_svg": map_svg,
        "projections": projections,
        "markdown": markdown,
    }


# ── Config save helpers (for web UI) ─────────────────────────

def save_planet_inventory(data):
    """Save planet inventory dict to planet_inventory.ini."""
    cp = configparser.ConfigParser()
    cp.optionxform = str
    for system, planets in sorted(data.items()):
        cp.add_section(system)
        for ptype, count in sorted(planets.items()):
            if ptype == "_ignored":
                if count:
                    cp.set(system, "_ignored", "1")
                continue
            if count > 0:
                cp.set(system, ptype, str(count))
    with open(_ini_path("planet_inventory.ini"), "w", encoding="utf-8") as f:
        cp.write(f)


def save_extraction_rates(data):
    """Save extraction rates dict to planet_extraction.ini (v1.1 format).

    data: {"System.PlanetType": {"Resource Name": rate}} (from web UI JSON)
    """
    cp = configparser.ConfigParser()
    cp.optionxform = str
    for section_key, resources in sorted(data.items()):
        cp.add_section(section_key)
        for resource, rate in sorted(resources.items()):
            if isinstance(rate, (int, float)) and rate > 0:
                cp.set(section_key, _name_to_underscore(resource), str(int(rate)))
    path = _ini_path("planet_extraction.ini")
    with open(path, "w", encoding="utf-8") as f:
        f.write("; Observed P0/hr per resource per planet-type per system.\n")
        f.write("; Format: [System.PlanetType] Resource_Name = p0_per_hour\n\n")
        cp.write(f)


def save_planet_density(data):
    """Save planet density dict to planet_density.ini.

    data: {"System.PlanetType": {"Resource Name": density_pct}} (from web UI JSON)
    """
    cp = configparser.ConfigParser()
    cp.optionxform = str
    for section_key, resources in sorted(data.items()):
        cp.add_section(section_key)
        for resource, pct in sorted(resources.items()):
            if isinstance(pct, (int, float)) and pct > 0:
                pct_str = str(int(pct)) if float(pct).is_integer() else str(pct)
                cp.set(section_key, _name_to_underscore(resource), pct_str)
    path = _ini_path("planet_density.ini")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# planet_density.ini\n")
        f.write("# Per-resource density % from in-game scan\n\n")
        cp.write(f)


def save_planet_taxes(data):
    """Save per-planet tax rates to planet_taxes.ini.

    data: {"System.PlanetType.Instance": tax_rate, ...}
    """
    cp = configparser.ConfigParser()
    cp.optionxform = str
    cp.add_section("taxes")
    for key, rate in sorted(data.items()):
        if isinstance(rate, (int, float)) and rate >= 0:
            cp.set("taxes", key, str(rate))
    path = _ini_path("planet_taxes.ini")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# planet_taxes.ini\n")
        f.write("# Per-planet POCO tax rates\n")
        f.write("# Format: System.PlanetType.Instance = rate\n\n")
        cp.write(f)


# ── Self-test ─────────────────────────────────────────────────

def self_test():
    """Run verification checks."""
    errors = []

    def check(label, condition, detail=""):
        if not condition:
            msg = f"FAIL: {label}"
            if detail:
                msg += f" -- {detail}"
            errors.append(msg)
            print(f"  [FAIL] {label} {detail}")
        else:
            print(f"  [ OK ] {label}")

    print("Running PI Dossier self-test...\n")

    # 1. Config loading
    print("Config loading:")
    char_info, pi_skills = load_skills()
    check("skills.ini loads", char_info["name"] != "Unknown")
    check("PI skills present", pi_skills["ccu"] > 0,
          f"CCU={pi_skills['ccu']}")

    cfg = load_pi_config()
    check("pi_config.ini loads", cfg["home_system"] == "Jufvitte")

    planet_inv = load_planet_inventory()
    check("planet_inventory.ini loads", len(planet_inv) > 0,
          f"{len(planet_inv)} systems")

    extraction_rates = load_extraction_rates()
    check("planet_extraction.ini loads", True)

    density_data = load_planet_density()

    # 2. EVE Ref type fetching
    print("\nEVE Ref data:")
    pi_types, by_name = fetch_pi_types(progress=True)
    check("P0 types fetched", sum(1 for t in pi_types.values() if t["tier"] == "P0") == 15,
          f"got {sum(1 for t in pi_types.values() if t['tier'] == 'P0')}")
    check("P1 types fetched", sum(1 for t in pi_types.values() if t["tier"] == "P1") == 15,
          f"got {sum(1 for t in pi_types.values() if t['tier'] == 'P1')}")
    check("P2 types fetched", sum(1 for t in pi_types.values() if t["tier"] == "P2") >= 20,
          f"got {sum(1 for t in pi_types.values() if t['tier'] == 'P2')}")
    check("P3 types fetched", sum(1 for t in pi_types.values() if t["tier"] == "P3") >= 15,
          f"got {sum(1 for t in pi_types.values() if t['tier'] == 'P3')}")

    # 3. Verify specific type data
    bacteria = pi_types.get(2393)
    check("Bacteria (P1) found", bacteria is not None)
    if bacteria:
        check("Bacteria volume from EVE Ref", bacteria["volume"] > 0,
              f"volume={bacteria['volume']}")

    # 4. Schematic fetching
    print("\nSchematics:")
    schematics = fetch_schematics(pi_types, progress=True)
    check("Schematics fetched", len(schematics) > 50,
          f"got {len(schematics)}")

    # Verify Bacteria schematic (131)
    sch131 = schematics.get(131)
    check("Schematic 131 (Bacteria)", sch131 is not None)
    if sch131:
        check("Bacteria cycle time = 1800s", sch131["cycle_time"] == 1800,
              f"got {sch131['cycle_time']}")
        check("Bacteria output = 20 units", sch131["output"]["quantity"] == 20,
              f"got {sch131['output']['quantity']}")
        check("Bacteria input = 3000 P0", sch131["inputs"][0]["quantity"] == 3000,
              f"got {sch131['inputs'][0]['quantity']}")

    # Verify Nanites schematic (78) - P2
    sch78 = schematics.get(78)
    check("Schematic 78 (Nanites)", sch78 is not None)
    if sch78:
        check("Nanites cycle time = 3600s", sch78["cycle_time"] == 3600,
              f"got {sch78['cycle_time']}")
        check("Nanites output = 5 units", sch78["output"]["quantity"] == 5,
              f"got {sch78['output']['quantity']}")
        check("Nanites has 2 inputs", len(sch78["inputs"]) == 2,
              f"got {len(sch78['inputs'])}")

    # 5. Chain graph
    print("\nChain graph:")
    chains = build_chain_graph(pi_types, schematics)
    p1_chains = [c for c in chains.values() if c["tier"] == "P1"]
    p2_chains = [c for c in chains.values() if c["tier"] == "P2"]
    p3_chains = [c for c in chains.values() if c["tier"] == "P3"]
    check("P1 chains built", len(p1_chains) == 15, f"got {len(p1_chains)}")
    check("P2 chains built", len(p2_chains) >= 20, f"got {len(p2_chains)}")
    check("P3 chains built", len(p3_chains) >= 15, f"got {len(p3_chains)}")

    # Verify a P2 chain traces back to P0
    for c in p2_chains:
        if c["output_name"] == "Nanites":
            check("Nanites traces to P0", len(c["p0_inputs"]) >= 2,
                  f"p0_inputs={[p['name'] for p in c['p0_inputs']]}")
            break

    # 6. Planet P0 mapping sanity
    print("\nPlanet mapping:")
    check("PLANET_P0_MAP has 8 types", len(PLANET_P0_MAP) == 8)
    check("P0_PLANET_MAP has 15 resources", len(P0_PLANET_MAP) == 15,
          f"got {len(P0_PLANET_MAP)}")
    check("Gas has Reactive Gas", "Reactive Gas" in PLANET_P0_MAP.get("Gas", []))
    check("Ice has Aqueous Liquids", "Aqueous Liquids" in PLANET_P0_MAP.get("Ice", []))

    # 7. Layout computation
    print("\nLayout computation:")
    test_chain_p1 = {"volume": 0.19, "tier": "P1"}
    layout = compute_p1_layout(test_chain_p1, 14000, 17000, 21315)
    check("P1 layout computes", layout is not None)
    if layout:
        check("P1 layout has units_hr > 0", layout["units_hr"] > 0,
              f"units_hr={layout['units_hr']}")
        check("P1 layout fits PG budget", layout["pg_used"] <= 17000,
              f"pg_used={layout['pg_used']}")

    test_chain_p2 = {"volume": 0.75, "tier": "P2"}
    layout2 = compute_p2_selfcontained_layout(test_chain_p2, [14000, 14000], 17000, 21315)
    check("P2 self-contained layout computes", layout2 is not None)
    if layout2:
        check("P2 self-contained has units_hr > 0", layout2["units_hr"] > 0,
              f"units_hr={layout2['units_hr']}")
        # Two 10-head ECUs alone are 16,200 PG — it only fits on fewer heads.
        check("P2 self-contained drops heads to fit two ECUs",
              layout2["ecu_heads"] < DEFAULT_ECU_HEADS,
              f"heads={layout2['ecu_heads']}")

    # ECU cost and head-count search
    check("ECU cost matches SDE (2600 PG + 550/head, 400 CPU + 110/head)",
          _ecu_pg_cpu(10) == (2600 + 5500, 400 + 1100)
          and _ecu_pg_cpu(1) == (3150, 510), _ecu_pg_cpu(10))
    check("CCU budgets are the real per-level command centres",
          CCU_BUDGETS[1] == (9000, 7057) and CCU_BUDGETS[4] == (17000, 21315)
          and CCU_BUDGETS[5] == (19000, 25415))
    _tight = compute_p1_layout(test_chain_p1, 14000, 9000, 7057)
    check("Tight command centre trades heads for BIFs",
          _tight is not None and _tight["ecu_heads"] < DEFAULT_ECU_HEADS
          and _tight["pg_used"] <= 9000 and _tight["cpu_used"] <= 7057,
          _tight)
    check("Roomy command centre keeps all 10 heads",
          compute_p1_layout(test_chain_p1, 14000, 19000,
                            25415)["ecu_heads"] == DEFAULT_ECU_HEADS)
    check("Head count scales the rate that reaches the BIFs",
          compute_p1_layout(test_chain_p1, 14000, 9000, 7057)["units_hr"]
          < compute_p1_layout(test_chain_p1, 14000, 19000, 25415)["units_hr"])

    factory = compute_factory_layout(test_chain_p2, 17000, 21315)
    check("P2 factory layout computes", factory is not None)
    if factory:
        check("P2 factory has AIFs", factory["facilities"]["aif"] > 0,
              f"aifs={factory['facilities']['aif']}")

    # P4 chain checks
    p4_chains = {tid: c for tid, c in chains.items() if c["tier"] == "P4"}
    check("P4 chains built", len(p4_chains) == 8, f"got {len(p4_chains)}")

    if p4_chains:
        sample_p4 = list(p4_chains.values())[0]
        check("P4 has P0 trace", len(sample_p4["all_p0_names"]) >= 4,
              f"p0s={sample_p4['all_p0_names']}")
        check("P4 has 3 inputs", len(sample_p4["inputs"]) == 3,
              f"inputs={[i['name'] for i in sample_p4['inputs']]}")
        check("P4 cycle time 3600s", sample_p4["schematic"]["cycle_time"] == 3600,
              f"cycle={sample_p4['schematic']['cycle_time']}")
        check("P4 output qty 1", sample_p4["schematic"]["output"]["quantity"] == 1,
              f"qty={sample_p4['schematic']['output']['quantity']}")

        # Verify HTIF in FACILITY_COSTS
        check("HTIF in FACILITY_COSTS", "htif" in FACILITY_COSTS,
              f"keys={list(FACILITY_COSTS.keys())}")
        check("HTIF PG=400", FACILITY_COSTS["htif"]["pg"] == 400, "")
        check("HTIF CPU=1100", FACILITY_COSTS["htif"]["cpu"] == 1100, "")

        # All P4 chains should require >5 planets for full vertical integration
        viable = find_viable_chains(chains, pi_types, schematics,
                                    planet_inv, extraction_rates,
                                    density_data, cfg)
        p4_viable = [vc for vc in viable if vc["chain"]["tier"] == "P4"]
        check("P4 chains analysed", len(p4_viable) == 8,
              f"got {len(p4_viable)}")
        min_planets = min(vc.get("planet_count", 99) for vc in p4_viable) if p4_viable else 0
        check("P4 min planets >= 6", min_planets >= 6,
              f"min_planets={min_planets}")

    # ── TSP solver tests ──
    print("\nTSP solver:")
    mock_matrix = {
        (1, 1): 0, (2, 2): 0, (3, 3): 0, (4, 4): 0,
        (1, 2): 3, (2, 1): 3,
        (1, 3): 5, (3, 1): 5,
        (1, 4): 7, (4, 1): 7,
        (2, 3): 2, (3, 2): 2,
        (2, 4): 4, (4, 2): 4,
        (3, 4): 1, (4, 3): 1,
    }
    tsp_route, tsp_total = _solve_tsp([2, 3, 4], 1, mock_matrix)
    # Optimal: 1->2->3->4->1 = 3+2+1+7=13 or 1->4->3->2->1 = 7+1+2+3=13
    check("TSP 3-node solves", tsp_total == 13, f"got {tsp_total}")
    check("TSP route has 3 waypoints", len(tsp_route) == 3, f"got {len(tsp_route)}")

    tsp_empty, tsp_zero = _solve_tsp([], 1, mock_matrix)
    check("TSP empty returns 0 jumps", tsp_zero == 0)

    tsp_one, tsp_one_d = _solve_tsp([2], 1, mock_matrix)
    check("TSP single waypoint = round trip", tsp_one_d == 6, f"got {tsp_one_d}")

    # Sell hub pinned as last stop before home
    tsp_sell, tsp_sell_d = _solve_tsp([2, 3], 1, mock_matrix, end_id=4)
    check("TSP sell pinned last", tsp_sell and tsp_sell[-1] == 4,
          f"route={tsp_sell}")
    # Best: 1->2->3->4->1 = 3+2+1+7 = 13
    check("TSP sell-last distance", tsp_sell_d == 13, f"got {tsp_sell_d}")

    # Sell system that is also an extraction system stays last, not duplicated
    tsp_dup, tsp_dup_d = _solve_tsp([2, 3], 1, mock_matrix, end_id=3)
    check("TSP sell==waypoint dedup", tsp_dup == [2, 3],
          f"route={tsp_dup}")
    check("TSP sell==waypoint distance", tsp_dup_d == 10, f"got {tsp_dup_d}")

    # Factory system pinned after its extractor systems: without the
    # constraint 1->4->3->2->1 (13) ties 1->2->3->4->1; require 4 (factory)
    # after both 2 and 3 (extractors) — only ascending order is valid.
    tsp_prec, tsp_prec_d = _solve_tsp([2, 3, 4], 1, mock_matrix,
                                      precedence={(2, 4), (3, 4)})
    check("TSP precedence: factory last", tsp_prec == [2, 3, 4],
          f"route={tsp_prec}")
    check("TSP precedence distance", tsp_prec_d == 13, f"got {tsp_prec_d}")

    # A single pair flips which of two equal-length tours is chosen
    tsp_rev, tsp_rev_d = _solve_tsp([2, 4], 1, mock_matrix,
                                    precedence={(4, 2)})
    check("TSP precedence: reversed order honoured", tsp_rev == [4, 2],
          f"route={tsp_rev}")
    check("TSP precedence reversed distance", tsp_rev_d == 14,
          f"got {tsp_rev_d}")

    # Contradictory precedence falls back to pure shortest tour
    tsp_cyc, tsp_cyc_d = _solve_tsp([2, 3], 1, mock_matrix,
                                    precedence={(2, 3), (3, 2)})
    check("TSP precedence cycle falls back", tsp_cyc_d == 10,
          f"route={tsp_cyc}, d={tsp_cyc_d}")

    # _layout_systems_and_stops emits (extractor_sys, factory_sys) pairs
    mock_alloc = [{"planets_used": [
        {"system": "SysA", "type": "Oceanic 1", "role": "Extract"},
        {"system": "SysB", "type": "Barren 1", "role": "Factory",
         "is_factory": True}]}]
    _sys, _stops, _prec = _layout_systems_and_stops(mock_alloc)
    check("Layout precedence pairs", _prec == {("SysA", "SysB")},
          f"got {_prec}")
    check("Layout stops counted", _stops == 2, f"got {_stops}")

    # ── Ignored systems ──
    print("\nIgnored systems:")
    test_inv = {"SysA": {"Gas": 2, "Barren": 1},
                "SysB": {"Gas": 1, "_ignored": True}}
    act = active_inventory(test_inv)
    check("Ignored system excluded", "SysB" not in act, f"got {list(act)}")
    check("Active system kept intact", act.get("SysA") == {"Gas": 2, "Barren": 1},
          f"got {act.get('SysA')}")

    # ── Route avoidance (live ESI) ──
    print("\nRoute avoidance:")
    juf_id = esi.search_system_id("Jufvitte")
    cos_id = esi.search_system_id("Costolle")
    oue_id = esi.search_system_id("Ouelletta")
    if juf_id and cos_id and oue_id:
        direct = esi.get_jump_count(juf_id, cos_id)
        detour = esi.get_jump_count(juf_id, cos_id, avoid={oue_id})
        check("Direct route found", direct > 0, f"got {direct}")
        check("Avoid forces longer route", detour > direct,
              f"direct={direct}, avoiding Ouelletta={detour}")
    else:
        check("Route avoidance systems resolve", False,
              f"juf={juf_id}, cos={cos_id}, oue={oue_id}")

    # ── Route cost test ──
    print("\nRoute cost:")
    mock_haul = {"sec_per_jump": 45, "sec_per_planet": 180,
                 "sec_per_station": 180, "daily_overhead": 300}
    mock_sids = {"SysA": 2, "SysB": 3, "Home": 1}
    rc = _compute_route_cost({"SysA", "SysB"}, 1, None, mock_sids,
                              mock_matrix, mock_haul, 3)
    check("Route cost has tour_jumps >= 0", rc["tour_jumps"] >= 0,
          f"jumps={rc['tour_jumps']}")
    check("Route cost has route_minutes > 0", rc["route_minutes"] > 0,
          f"min={rc['route_minutes']:.1f}")
    expected_sec = (rc["tour_jumps"] * 45 + 3 * 180 + 180 + 300)
    check("Route cost formula consistent", abs(rc["route_seconds"] - expected_sec) < 1,
          f"expected={expected_sec}, got={rc['route_seconds']}")

    # ── Per-planet tax test ──
    print("\nPer-planet tax:")
    test_vc = {
        "chain": {"base_price": 1000, "inputs": [], "p0_inputs": [],
                  "tier": "P1",
                  "schematic": {"output": {"quantity": 1}}},
        "layout_type": "p1_extractor",
        "planets_used": [{"system": "Jufvitte", "type": "Gas A",
                          "layout": {}}],
    }
    test_taxes = {"Jufvitte.Gas.A": 0.05}
    tax_with = _compute_chain_tax(test_vc, {}, {"tax_rate": 0.15}, test_taxes)
    tax_without = _compute_chain_tax(test_vc, {}, {"tax_rate": 0.15}, {})
    # P1 export: PI_TAX_BASE["P1"] × rate = 500 × rate
    check("Per-planet tax uses specific rate",
          abs(tax_with - 500 * 0.05) < 0.01, f"got {tax_with}")
    check("Per-planet tax falls back to default",
          abs(tax_without - 500 * 0.15) < 0.01, f"got {tax_without}")

    # P2 self-contained: export only, 9000 × rate
    test_vc_p2 = {
        "chain": {"tier": "P2", "inputs": [], "p0_inputs": [],
                  "schematic": {"output": {"quantity": 1}}},
        "layout_type": "p2_selfcontained",
        "planets_used": [{"system": "Jufvitte", "type": "Barren A", "layout": {}}],
    }
    tax_p2 = _compute_chain_tax(test_vc_p2, {}, {"tax_rate": 0.10}, {})
    check("P2 self-contained export tax = 9000 * 0.10",
          abs(tax_p2 - 900) < 0.01, f"got {tax_p2}")

    # ── Estimate chain haul test ──
    print("\nChain haul estimate:")
    test_haul_vc = {
        "planets_used": [
            {"system": "SysA", "layout": {"role": "extractor", "volume_hr": 5}},
        ],
    }
    est = _estimate_chain_haul_minutes(test_haul_vc, mock_matrix, 1,
                                        mock_sids, mock_haul)
    check("Estimate haul > 0", est > 0, f"got {est:.1f} min")

    # ── Instance rate table & candidates ──
    print("\nInstance rates & candidates:")
    t_inv = {"Sys1": {"Gas": 2}, "Sys2": {"Gas": 1}}
    t_ext = {"Sys1.Gas.A": {"Aqueous Liquids": 9000}}
    t_dens = {"Sys1.Gas.A": {"Aqueous Liquids": 22, "Ionic Solutions": 30},
              "Sys1.Gas.B": {"Ionic Solutions": 40}}
    ir = _build_instance_rates(t_inv, t_ext, t_dens)
    check("OBS beats EST on same instance",
          ir[("Sys1", "Gas", "A")]["Aqueous Liquids"] == (9000, "OBS"),
          f"got {ir[('Sys1','Gas','A')].get('Aqueous Liquids')}")
    check("EST from density band",
          ir[("Sys1", "Gas", "A")]["Ionic Solutions"] == (19000, "EST"),
          f"got {ir[('Sys1','Gas','A')].get('Ionic Solutions')}")
    check("Scanned planet lacking a resource yields nothing",
          "Aqueous Liquids" not in ir[("Sys1", "Gas", "B")],
          f"got {ir[('Sys1','Gas','B')]}")
    check("Unscanned planet gets DFL",
          ir[("Sys2", "Gas", "A")]["Reactive Gas"] == (DEFAULT_EXTRACTION_RATE, "DFL"),
          f"got {ir[('Sys2','Gas','A')].get('Reactive Gas')}")
    t_cands, t_by_sys = _build_p0_candidates(ir)
    top_is = t_cands["Ionic Solutions"][0]
    check("Candidates ranked by rate",
          top_is[0] == 22000 and top_is[2] == "Sys1" and top_is[4] == "B",
          f"got {top_is}")

    # ── Combo-local selection & deconfliction ──
    print("\nCombo-local selection & deconfliction:")
    t_chain = {"output_type_id": 90001, "output_name": "TestP1",
               "tier": "P1", "volume": 0.38,
               "p0_inputs": [{"name": "Ionic Solutions"}],
               "inputs": [],
               "schematic": {"output": {"quantity": 1}, "cycle_time": 1800}}
    t_cfg = {"tax_rate": 0.15, "pg_budget": 17000, "cpu_budget": 21315,
             "max_planets": 5}
    t_ctx = _build_analysis_ctx({90001: t_chain}, {}, {}, t_inv, t_ext,
                                t_dens, t_cfg, {"Sys1.Gas.B": 0.05})
    sel_all, _f = _resolve_chain_selection(t_chain, t_ctx)
    check("Global pick is best instance",
          sel_all and sel_all[1][2:5] == ("Sys1", "Gas", "B"),
          f"got {sel_all}")
    sel_combo, _f = _resolve_chain_selection(t_chain, t_ctx,
                                             combo_set=frozenset({"Sys2"}))
    check("Combo-local pick stays in combo",
          sel_combo and sel_combo[1][2] == "Sys2", f"got {sel_combo}")
    sel_excl, _f = _resolve_chain_selection(
        t_chain, t_ctx, exclude=frozenset({("Sys1", "Gas", "B")}))
    check("Excluded instance falls to next best",
          sel_excl and sel_excl[1][2:5] == ("Sys1", "Gas", "A"),
          f"got {sel_excl}")
    vc1 = _analyse_chain(t_chain, t_ctx)
    k1 = _planet_instance_key(vc1["planets_used"][0])
    vc2 = _analyse_chain(t_chain, t_ctx, exclude=frozenset({k1}))
    check("Second colony lands on a different planet",
          _planet_instance_key(vc2["planets_used"][0]) != k1,
          f"both on {k1}")

    # ── Self-contained P2 uses ONE physical planet ──
    print("\nSelf-contained P2 instance integrity:")
    t_p2 = {"output_type_id": 90002, "output_name": "TestP2", "tier": "P2",
            "volume": 0.75,
            "p0_inputs": [{"name": "Aqueous Liquids"},
                          {"name": "Ionic Solutions"}],
            "inputs": [],
            "schematic": {"output": {"quantity": 5}, "cycle_time": 3600}}
    scs = _sc_candidates_for_chain(t_p2, t_ctx)
    check("SC candidates exist", len(scs) >= 1, f"got {len(scs)}")
    if scs:
        best_sc = scs[0]
        # Sys1 Gas B lacks Aqueous Liquids, so it must NOT be combined with
        # Gas A's rates; valid single planets: Sys1 Gas A, Sys2 Gas A (DFL)
        check("SC best is a single valid planet",
              (best_sc["system"], best_sc["instance"]) in
              [("Sys1", "A"), ("Sys2", "A")], f"got {best_sc}")
        sc_vc = _analyse_chain(t_p2, t_ctx)
        check("SC vc viable from one instance", sc_vc.get("viable"),
              f"got {sc_vc.get('flags')}")

    # ── Factory placement & tax ──
    print("\nFactory placement:")
    fac = _pick_factory_planet(["Sys1"], ["Sys2"], set(), t_ctx)
    check("Factory picks cheapest-tax spare planet",
          fac is not None and fac[:3] == ("Sys1", "Gas", "B")
          and abs(fac[3] - 0.05) < 1e-9, f"got {fac}")
    fac2 = _pick_factory_planet(["Sys1"], ["Sys2"],
                                {("Sys1", "Gas", "B")}, t_ctx)
    check("Factory respects claimed instances",
          fac2 is not None and fac2[:3] == ("Sys1", "Gas", "A"),
          f"got {fac2}")
    fac3 = _pick_factory_planet(["Sys1"], ["Sys2"],
                                {("Sys1", "Gas", "A"), ("Sys1", "Gas", "B")},
                                t_ctx)
    check("Factory falls back to other systems",
          fac3 is not None and fac3[0] == "Sys2", f"got {fac3}")

    # ── Density model auto-calibration ──
    print("\nDensity model calibration:")
    est0, info0 = build_density_estimator({}, {})
    check("No observations -> static table",
          info0["points"] == 0 and est0(30) == 19000, f"got {est0(30)}")
    # Single point: obs 9000 at 22% density (static says 17000)
    est1, info1 = build_density_estimator(
        {"S.Gas.A": {"Aqueous Liquids": 9000}},
        {"S.Gas.A": {"Aqueous Liquids": 22}})
    check("Single point scales the table",
          info1["points"] == 1 and abs(est1(22) - 9000) < 1
          and info1["fit"] is None,
          f"est(22)={est1(22):.0f}, info={info1}")
    check("Scale applies across all densities",
          abs(est1(30) - 19000 * info1["scale"]) < 1, f"got {est1(30):.0f}")
    # Six points on a clean rate = 1000 * density line -> power fit b ~ 1
    cal_ext = {}
    cal_dens = {}
    for i, d in enumerate([5, 10, 20, 30, 40, 50]):
        key = f"S{i}.Gas.A"
        cal_ext[key] = {"Base Metals": 1000 * d}
        cal_dens[key] = {"Base Metals": d}
    est6, info6 = build_density_estimator(cal_ext, cal_dens)
    check("Power fit engages at 6 points",
          info6["fit"] is not None and info6["fit"]["weight"] > 0,
          f"got {info6}")
    if info6["fit"]:
        check("Power fit recovers linear trend",
              abs(info6["fit"]["b"] - 1.0) < 0.05,
              f"b={info6['fit']['b']:.3f}")
        check("Calibrated estimate tracks observations",
              20000 < est6(25) < 30000, f"est(25)={est6(25):.0f}")
    check("Zero density still yields zero", est6(0) == 0 and est1(0) == 0)
    # Obs without density scan contributes no calibration point
    _est, info_nd = build_density_estimator(
        {"S.Gas.A": {"Aqueous Liquids": 9000}}, {})
    check("Obs without density scan is not a calibration point",
          info_nd["points"] == 0, f"got {info_nd}")

    # ── Allocator helpers ──
    print("\nAllocator helpers:")
    check("_types_fit respects pool counts",
          _types_fit({"Gas": 2, "Any": 1}, {"Gas": 2, "Barren": 1}, 3)
          and not _types_fit({"Gas": 3}, {"Gas": 2}, 2))
    t_vc = {"planets_used": [
        {"system": "Sys1", "type": "Gas A"},
        {"system": "Sys1", "type": "Gas B", "is_factory": True}]}
    _attach_alloc_fields(t_vc)
    check("Factory counts as Any in allocation",
          t_vc["_alloc_types"] == {"Gas": 1, "Any": 1}
          and t_vc["_alloc_pc"] == 2, f"got {t_vc['_alloc_types']}")
    inter = _interesting_systems(t_ctx, "Sys1")
    check("Interesting systems include extraction hosts",
          set(inter) == {"Sys1", "Sys2"}, f"got {inter}")

    # ── Manual build overrides (BIF/AIF recompute) ──
    print("\nManual build overrides:")
    sb = _split_bifs(5, [12000, 6000])
    check("_split_bifs sums to total", sum(sb) == 5, f"got {sb}")
    check("_split_bifs gives each line >=1", all(b >= 1 for b in sb),
          f"got {sb}")
    check("_split_bifs favours faster line", sb[0] >= sb[1], f"got {sb}")

    # P1 extractor: BIF count drives P1 output
    p1_vc = {"layout_type": "p1_extractor", "chain": {"volume": 0.19},
             "planets_used": [{"extract_rates": [12000], "layout": {
                 "facilities": {"ecu": 1, "bif": 2, "launchpad": 1}}}]}
    _recompute_chain(p1_vc, t_ctx)
    check("P1 recompute: 2 BIF @ 12000 -> 80/hr", p1_vc["units_hr"] == 80,
          f"got {p1_vc['units_hr']}")
    p1_vc["planets_used"][0]["layout"]["facilities"]["bif"] = 1
    _recompute_chain(p1_vc, t_ctx)
    check("P1 recompute: 1 BIF @ 12000 -> 40/hr", p1_vc["units_hr"] == 40,
          f"got {p1_vc['units_hr']}")

    # P2 factory: AIFs auto-derive from the slowest P1 input
    def _mk_p2f():
        return {"layout_type": "p2_factory",
                "chain": {"volume": 0.75, "output_name": "TestP2"},
                "planets_used": [
                    {"is_factory": False, "extract_rates": [12000],
                     "p1_type_id": 1, "p1_volume": 0.38,
                     "layout": {"facilities": {"ecu": 1, "bif": 2,
                                               "launchpad": 1}}},
                    {"is_factory": False, "extract_rates": [12000],
                     "p1_type_id": 2, "p1_volume": 0.38,
                     "layout": {"facilities": {"ecu": 1, "bif": 1,
                                               "launchpad": 1}}},
                    {"is_factory": True,
                     "layout": {"facilities": {"aif": 1, "launchpad": 1}}}]}
    p2f = _mk_p2f()
    _recompute_chain(p2f, t_ctx)
    check("P2f recompute: min supply 40 -> 1 AIF, 5/hr",
          p2f["units_hr"] == 5
          and p2f["planets_used"][2]["layout"]["facilities"]["aif"] == 1,
          f"units={p2f['units_hr']}, "
          f"aif={p2f['planets_used'][2]['layout']['facilities']['aif']}")
    # One extra BIF on the slow input lifts supply -> one more AIF -> more P2
    p2f["planets_used"][1]["layout"]["facilities"]["bif"] = 2
    _recompute_chain(p2f, t_ctx)
    check("P2f recompute: extra BIF -> 2 AIF, 10/hr",
          p2f["units_hr"] == 10
          and p2f["planets_used"][2]["layout"]["facilities"]["aif"] == 2,
          f"units={p2f['units_hr']}, "
          f"aif={p2f['planets_used'][2]['layout']['facilities']['aif']}")
    # Manual AIF override below supply caps the output
    p2f2 = _mk_p2f()
    p2f2["planets_used"][1]["layout"]["facilities"]["bif"] = 2
    _recompute_chain(p2f2, t_ctx, aif_overrides={2: 1})
    check("P2f AIF override caps output", p2f2["units_hr"] == 5,
          f"got {p2f2['units_hr']}")
    # Over-budget facilities flagged but still recomputed
    p1_vc["planets_used"][0]["layout"]["facilities"]["bif"] = 20
    _recompute_chain(p1_vc, t_ctx)
    check("Over-budget facilities flagged",
          p1_vc["planets_used"][0]["layout"].get("over_budget") is True,
          "expected over_budget True")
    check("recalc_chain_build errors with no run",
          "error" in recalc_chain_build(0, 0))

    # ── Single-product scaling (more planets -> more output) ──
    print("\nSingle-product scaling:")
    scale_chain = {
        "output_type_id": 90030, "output_name": "TestRocketFuel", "tier": "P2",
        "volume": 0.75,
        "p0_inputs": [{"name": "Suspended Plasma"}, {"name": "Ionic Solutions"}],
        "inputs": [{"type_id": 2, "name": "Plasmoids", "tier": "P1",
                    "quantity": 40},
                   {"type_id": 1, "name": "Electrolytes", "tier": "P1",
                    "quantity": 40}],
        "all_p0_names": {"Suspended Plasma", "Ionic Solutions"},
        "schematic": {"output": {"quantity": 5}, "cycle_time": 3600,
                      "inputs": []},
    }
    scale_ctx = {
        "pg_budget": 17000, "cpu_budget": 21315,
        "p0_candidates": {
            "Suspended Plasma": [(8000, "DFL", "S1", "Lava", "A"),
                                 (8000, "DFL", "S2", "Lava", "A"),
                                 (8000, "DFL", "S3", "Lava", "A")],
            "Ionic Solutions": [(8000, "DFL", "S4", "Gas", "A"),
                                (8000, "DFL", "S5", "Gas", "A"),
                                (8000, "DFL", "S6", "Gas", "A")],
        },
        "p0_to_p1": {
            "Suspended Plasma": {"name": "Plasmoids", "volume": 0.38,
                                 "type_id": 2},
            "Ionic Solutions": {"name": "Electrolytes", "volume": 0.38,
                                "type_id": 1},
        },
        "inv": {"S1": {"Lava": 1}, "S2": {"Lava": 1}, "S3": {"Lava": 1},
                "S4": {"Gas": 1}, "S5": {"Gas": 1}, "S6": {"Gas": 1},
                "S7": {"Barren": 1}},
        "instance_rates": {}, "planet_taxes": {}, "cfg": {"tax_rate": 0.15},
        "sc_cache": {},
    }
    scale_base = {"chain": scale_chain, "layout_type": "p2_factory"}
    s3 = _build_scaled_vc(scale_base, scale_ctx, 3)
    s4 = _build_scaled_vc(scale_base, scale_ctx, 4)
    s5 = _build_scaled_vc(scale_base, scale_ctx, 5)
    check("Scale 3 planets: 2 ext + 1 factory, 5/hr",
          s3["planet_count"] == 3 and s3["units_hr"] == 5,
          f"pc={s3['planet_count']}, units={s3['units_hr']}")
    check("Scale 4 planets (2+1 ext): no extra output (5/hr)",
          s4["planet_count"] == 4 and s4["units_hr"] == 5,
          f"pc={s4['planet_count']}, units={s4['units_hr']}")
    check("Scale 5 planets (2+2 ext): output doubles to 10/hr",
          s5["planet_count"] == 5 and s5["units_hr"] == 10,
          f"pc={s5['planet_count']}, units={s5['units_hr']}")
    check("Scaled build has exactly one factory",
          sum(1 for p in s5["planets_used"] if p.get("is_factory")) == 1,
          f"factories={sum(1 for p in s5['planets_used'] if p.get('is_factory'))}")
    check("Scaling is monotonic non-decreasing",
          s3["units_hr"] <= s4["units_hr"] <= s5["units_hr"])
    check("product_scaling errors with no run",
          "error" in product_scaling("Anything"))

    # ── Sell-at-Jita pricing mode ──
    print("\nSell-at-Jita pricing:")
    _sj_chain = {"output_type_id": 99001, "output_name": "SJ Test", "tier": "P1",
                 "volume": 0.38, "inputs": [], "p0_inputs": [],
                 "schematic": {"cycle_time": 3600, "output": {"quantity": 20},
                               "inputs": []}}
    _sj_prices = {99001: {"local_buy": 300, "local_book": [], "local_real_depth": 0,
                          "local_buyer_system": "Home", "local_buyer_jumps": 0,
                          "local_vwap": 320, "local_avg_daily_vol": 2,
                          "local_active_days": 3, "local_order_count": 2,
                          "jita_vwap": 500, "jita_avg_daily_vol": 5000,
                          "jita_active_days": 30, "jita_buy": 480,
                          "jita_buy_book": [(480, 10 ** 9)],
                          "jita_sell_book": [(520, 10 ** 9)]}}
    def _sj_run(flag):
        _cfg = load_pi_config()
        _cfg["sell_at_jita"] = flag
        _ectx = {"market_prices": _sj_prices, "cfg": _cfg, "pi_types": {},
                 "matrix": None, "home_id": None, "system_ids": None,
                 "planet_taxes": {}, "jita_jumps": 15}
        _vc = {"chain": _sj_chain, "units_hr": 40.0, "volume_hr": 40.0 * 0.38,
               "layout_type": "p1_extractor", "planets_used": [], "flags": []}
        _compute_economics_single(_vc, _ectx)
        return _vc
    _sj_local, _sj_jita = _sj_run(False), _sj_run(True)
    _sj_st = load_pi_config()["sales_tax"]
    check("Sales tax derived from Accounting skill",
          abs(_sj_st - SALES_TAX_BASE
              * (1 - SALES_TAX_REDUCTION * load_skills()[1]["accounting"]))
          < 1e-9, f"got {_sj_st}")
    check("Jita mode prices at jita_buy net of sales tax",
          abs(_sj_jita["sell_price"] - 480 * (1 - _sj_st)) < 1e-6,
          f"got {_sj_jita['sell_price']}")
    check("Jita mode gross = units x net jita_buy",
          abs(_sj_jita["gross_isk_hr"] - 40 * 480 * (1 - _sj_st)) < 1e-6)
    check("Jita mode: no activity penalty",
          _sj_jita["activity_factor"] == 1.0
          and abs(_sj_jita["adjusted_net_isk_hr"] - _sj_jita["net_isk_hr"]) < 1e-6)
    check("Jita mode suppresses local-market flags",
          not any("LOW ACTIVITY" in f or "SHALLOW" in f or "NO LOCAL" in f
                  for f in _sj_jita["flags"]),
          f"flags={_sj_jita['flags']}")
    check("Local mode still flags low activity",
          any("LOW ACTIVITY" in f for f in _sj_local["flags"]))
    check("Modes yield different sell prices",
          _sj_local["sell_price"] != _sj_jita["sell_price"])

    # ── Import-and-upgrade analysis ──
    print("\nImport-and-upgrade:")
    _iu_types = {
        1:  {"name": "W",  "tier": "P1", "volume": 0.38, "produced_by": []},
        2:  {"name": "B",  "tier": "P1", "volume": 0.38, "produced_by": []},
        10: {"name": "C",  "tier": "P2", "volume": 1.5,  "produced_by": [110]},
        20: {"name": "S",  "tier": "P3", "volume": 6.0,  "produced_by": [120]},
        30: {"name": "WM", "tier": "P4", "volume": 100.0, "produced_by": [130]},
    }
    _iu_sch = {
        110: {"inputs": [{"type_id": 1, "quantity": 40}, {"type_id": 2, "quantity": 40}],
              "output": {"type_id": 10, "quantity": 5}, "cycle_time": 3600},
        120: {"inputs": [{"type_id": 10, "quantity": 10}],
              "output": {"type_id": 20, "quantity": 3}, "cycle_time": 3600},
        130: {"inputs": [{"type_id": 20, "quantity": 6}],
              "output": {"type_id": 30, "quantity": 1}, "cycle_time": 3600},
    }
    _iu_chains = {
        20: {"output_type_id": 20, "output_name": "S", "tier": "P3", "volume": 6.0,
             "schematic": _iu_sch[120], "inputs": [{"type_id": 10, "tier": "P2", "quantity": 10}]},
        30: {"output_type_id": 30, "output_name": "WM", "tier": "P4", "volume": 100.0,
             "schematic": _iu_sch[130], "inputs": [{"type_id": 20, "tier": "P3", "quantity": 6}]},
    }
    def _deep(sell, buy):
        """Market entry with books deep enough that walking them never slips."""
        return {"jita_sell": sell, "jita_buy": buy, "jita_vwap": buy,
                "jita_avg_daily_vol": 0,
                "jita_sell_book": [(sell, 10 ** 9)] if sell else [],
                "jita_buy_book": [(buy, 10 ** 9)] if buy else []}
    _iu_market = {
        1: _deep(100, 90), 2: _deep(120, 100),
        10: _deep(12000, 11000), 20: _deep(90000, 85000),
        30: _deep(0, 1500000),
    }
    _saved_run = _LAST_RUN.get("state")
    _LAST_RUN["state"] = {
        "ctx": {"pi_types": _iu_types, "schematics": _iu_sch, "chains": _iu_chains},
        "ectx": {"market_prices": _iu_market},
        "cfg": {"tax_rate": 0.10, "pg_budget": 17000, "cpu_budget": 21315},
        "ranked": [], "pg_budget": 17000, "cpu_budget": 21315,
    }
    _ft_state = {"ctx": {"inv": {"S1": {"Gas": 2}, "S2": {"Barren": 1}},
                         "planet_taxes": {"S1.Gas.A": 0.05, "S1.Gas.B": 0.15,
                                          "S2.Barren.A": 0.01}},
                 "cfg": {"tax_rate": 0.15}}
    check("Factory tax = cheapest scouted POCO in inventory",
          _factory_tax_rate(_ft_state) == (0.01, "S2.Barren.A"),
          f"got {_factory_tax_rate(_ft_state)}")
    check("Factory tax falls back to cfg default when unscouted",
          _factory_tax_rate({"ctx": {"inv": {"S1": {"Gas": 1}}},
                             "cfg": {"tax_rate": 0.15}}) == (0.15, None))
    _iu = import_upgrade_options(min_tier="P3")
    _wm = next((p for p in _iu["products"] if p["output_name"] == "WM"), None)
    _s = next((p for p in _iu["products"] if p["output_name"] == "S"), None)
    check("Import options built for P3 and P4", _wm is not None and _s is not None)
    if _wm:
        _p3 = next(o for o in _wm["options"] if o["buy_tier"] == "P3")
        check("BOM P4<-P3 costs 6x P3 sell",
              abs(_p3["input_cost_per_unit"] - 6 * 90000) < 1, _p3["input_cost_per_unit"])
        _exp_tax = 6 * PI_TAX_BASE["P3"] * 0.5 * 0.10 + PI_TAX_BASE["P4"] * 0.10
        check("Import tax = import(inputs)+export(P4)",
              abs(_p3["tax_per_unit"] - _exp_tax) < 1, _p3["tax_per_unit"])
        check("Revenue = Jita buy of P4",
              abs(_p3["revenue_per_unit"] - 1500000) < 1)
        # Storage + launchpad leave 21315-3600-2000 CPU for HTIFs at 1100 each,
        # a 14.29/hr ceiling. 15 whole HTIFs do not fit that CPU, so the build
        # rounds down to 14 and the planet makes exactly 14/hr. Storage — not
        # the customs office — is what binds at a 1-day buffer.
        check("P4<-P3 sized by whole HTIFs under the storage-adjusted ceiling",
              _p3["htif_count"] == 14
              and abs(_p3["units_hr_per_planet"] - 14.0) < 1e-9
              and _p3["storage_count"] == 3
              and _p3["cpu_used"] <= 21315 and _p3["pg_used"] <= 17000
              and not any("POCO BUFFER" in f for f in _p3["flags"]),
              f"{_p3['units_hr_per_planet']} htif={_p3['htif_count']} "
              f"storage={_p3['storage_count']} cpu={_p3['cpu_used']}")
        check("On-planet buffer fits the storage that was built",
              _p3["on_planet_m3"] <= _p3["on_planet_capacity_m3"] + 1e-6,
              f"{_p3['on_planet_m3']:.0f} > {_p3['on_planet_capacity_m3']}")
        _p1 = next(o for o in _wm["options"] if o["buy_tier"] == "P1")
        check("BOM P4<-P1 expands to 160+160 P1",
              abs(_p1["input_cost_per_unit"] - (160 * 100 + 160 * 120)) < 1,
              _p1["input_cost_per_unit"])
        _p3_st = _import_option(_iu_chains[30], 3, _iu_market, _iu_types,
                                _iu_sch, 17000, 21315, 0.10, sales_tax=0.02)
        check("Import sales tax deducted from net",
              abs((_p3["net_per_unit"] - _p3_st["net_per_unit"])
                  - 1500000 * 0.02) < 1)
        _p3_bo = _import_option(_iu_chains[30], 3, _iu_market, _iu_types,
                                _iu_sch, 17000, 21315, 0.10, broker_fee=0.027)
        check("Buy-order sourcing priced at jita_buy + broker fee",
              abs(_p3_bo["input_cost_buy_sourced"] - 6 * 85000 * 1.027) < 1)
        check("Buy-sourced net = revenue - buy cost - POCO tax",
              abs(_p3_bo["net_per_unit_buy_sourced"]
                  - (1500000 - 6 * 85000 * 1.027 - _exp_tax)) < 1)
        check("Direct option: whole HTIFs, throughput matches the build",
              _p3_bo["htif_count"] == 14
              and abs(_p3_bo["units_hr_per_planet"] - 14.0) < 1e-9,
              _p3_bo["htif_count"])
    if _s:
        check("P3 target only offers P1/P2 buy-in",
              {o["buy_tier"] for o in _s["options"]} == {"P1", "P2"})
    check("import_upgrade_options ranks best-first",
          all(_iu["products"][i]["best"]["net_isk_hr_per_planet"]
              >= _iu["products"][i + 1]["best"]["net_isk_hr_per_planet"]
              for i in range(len(_iu["products"]) - 1)))
    # ── Best-play head-to-head (extract vs import) ──
    print("\nBest-play head-to-head:")
    _bp_ranked = [{
        "chain": _iu_chains[20], "output_name": "S",
        "units_hr": 10.0, "tax_per_hr": 20000.0, "planet_count": 3,
        "planets_used": [1, 2, 3], "haul_minutes_per_day": 30.0,
        "jita_buy_price": 85000, "viable": True, "flags": [],
    }]
    # Two cheap customs offices, then pricier ones — enough spread that the
    # planner has to assign real planets rather than assume one rate. All
    # Barren/Temperate so P4 can use any of them (the restriction gets its own
    # test below).
    _rp_inv = {"Cheapville": {"Temperate": 2}, "Pricyton": {"Barren": 4}}
    _rp_taxes = {"Cheapville.Temperate.A": 0.01, "Cheapville.Temperate.B": 0.01,
                 "Pricyton.Barren.A": 0.10, "Pricyton.Barren.B": 0.10,
                 "Pricyton.Barren.C": 0.10, "Pricyton.Barren.D": 0.10}
    _LAST_RUN["state"] = {
        "ctx": {"pi_types": _iu_types, "schematics": _iu_sch,
                "chains": _iu_chains, "inv": _rp_inv,
                "planet_taxes": _rp_taxes},
        "ectx": {"market_prices": _iu_market, "jita_jumps": 15},
        "cfg": {"tax_rate": 0.10, "pg_budget": 17000, "cpu_budget": 21315,
                "hauler_m3": 9000,
                "haul": {"sec_per_jump": 45, "sec_per_station": 180}},
        "ranked": _bp_ranked, "pg_budget": 17000, "cpu_budget": 21315,
    }
    _bp = best_pi_plays(isk_per_haul_min=100000, min_tier="P2")
    _bp_s = next((p for p in _bp["plays"] if p["output_name"] == "S"), None)
    check("Best-play built for P3 target", _bp_s is not None)
    if _bp_s:
        _ex = next(o for o in _bp_s["options"] if o["strategy"] == "extract")
        _im = next(o for o in _bp_s["options"] if o["strategy"] == "import")
        check("Extract re-priced at Jita buy (10x85k)",
              abs(_ex["gross_isk_hr"] - 10 * 85000) < 1, _ex["gross_isk_hr"])
        check("Extract net before haul = gross - tax",
              abs(_ex["net_isk_hr"] - (850000 - 20000)) < 1)
        _rt = _bp["jita_round_trip_min"]
        check("Extract haul = local circuit + Jita delivery",
              abs(_ex["haul_min_per_day"] - (30 + 1 * _rt)) < 0.01,
              _ex["haul_min_per_day"])
        check("Haul cost subtracted from net",
              abs(_ex["net_after_haul_isk_hr"]
                  - (830000 - (30 + _rt) * 100000 / 24)) < 1)
        check("Both strategies present for P3", _im["strategy"] == "import")
        check("Winner = higher net after haul",
              _bp_s["winner_strategy"] ==
              (_ex if _ex["net_after_haul_isk_hr"] >= _im["net_after_haul_isk_hr"]
               else _im)["strategy"])
    check("Best-plays ranked by total net after haul",
          all(_bp["plays"][i]["best_net_after_haul_isk_hr"]
              >= _bp["plays"][i + 1]["best_net_after_haul_isk_hr"]
              for i in range(len(_bp["plays"]) - 1)))

    # ── Haul cadence + POCO buffer ──
    print("\nHaul cadence & POCO buffer:")
    _th = _import_option(_iu_chains[30], 3, _iu_market, _iu_types, _iu_sch,
                         17000, 21315, 0.10,
                         cadence={"haul_days": 3, "poco_m3": 35000,
                                  "buffer_days": 1})
    # 3d window: export leg 100 m3/unit x 24h x 3d is the binding one
    _th_uncapped = 15715 / 1100
    check("POCO throttle scales to the worst window leg",
          abs(_th["poco_scale"] - 35000 / (100 * _th_uncapped * 24 * 3)) < 1e-6
          and abs(_th["units_hr_per_planet"]
                  - _th_uncapped * _th["poco_scale"]) < 1e-6
          and any("POCO BUFFER" in f for f in _th["flags"]),
          f"scale={_th['poco_scale']}")
    check("POCO throttle shrinks the storage the planet needs",
          _th["storage_count"] < 4 and _th["on_planet_m3"]
          <= _th["on_planet_capacity_m3"] + 1e-6,
          f"storage={_th['storage_count']}")
    _th2 = _import_option(_iu_chains[20], 2, _iu_market, _iu_types, _iu_sch,
                          17000, 21315, 0.10,
                          cadence={"haul_days": 1, "poco_m3": 35000,
                                   "buffer_days": 1})
    check("POCO throttle no-op when the window fits",
          _th2["poco_scale"] == 1.0
          and not any("POCO BUFFER" in f for f in _th2["flags"]))

    # ── Jita book depth ──
    print("\nJita book depth:")
    _bk = [(100.0, 10), (90.0, 10), (50.0, 1000)]
    check("Book walk fills top-down",
          _walk_book(_bk, 20) == (95.0, 20.0))
    check("Book walk reports short fill",
          _walk_book([(100.0, 5)], 20) == (100.0, 5.0))
    check("Book walk on empty book is zero", _walk_book([], 10) == (0.0, 0.0))
    _dp = {"jita_buy_book": _bk, "jita_vwap": 60, "jita_avg_daily_vol": 100}
    _f_small = _jita_fill(_dp, 10, True, 1)
    _f_big = _jita_fill(_dp, 20, True, 1)
    check("Small dump clears at top of book",
          _f_small["price"] == 100.0 and _f_small["slippage"] == 0.0)
    check("Bigger dump walks the book down and reports slippage",
          _f_big["price"] == 95.0 and abs(_f_big["slippage"] - 0.05) < 1e-9)
    _f_over = _jita_fill(_dp, 2040, True, 1)
    check("Unfillable remainder prices at VWAP",
          abs(_f_over["price"] - (100 * 10 + 90 * 10 + 50 * 1000
                                  + 60 * 1020) / 2040) < 1e-9
          and _f_over["filled_frac"] < 1.0)
    check("Book share measures the haul against 30d throughput",
          abs(_jita_fill(_dp, 300, True, 3)["book_share"] - 1.0) < 1e-9)
    _f_buy = _jita_fill({"jita_sell_book": [(10.0, 5), (12.0, 100)],
                         "jita_vwap": 11}, 10, False, 1)
    check("Buying walks the sell book up (positive slippage)",
          _f_buy["price"] == 11.0 and abs(_f_buy["slippage"] - 0.1) < 1e-9)
    check("Empty book flagged", _depth_flags(_jita_fill({}, 5, True), "SELL")
          == ["NO JITA BOOK (SELL)"])

    # ── Storage vs factories ──
    print("\nOn-planet storage sizing:")
    _su, _, _, _ss = _factory_units_per_planet({"aif": 1.0}, 17000, 21315)
    check("No buffer requested -> no storage, PG/CPU ceiling only",
          _ss == 0 and abs(_su - min((17000 - 700) / 700,
                                     (21315 - 3600) / 500)) < 1e-9)
    _su2, _, _, _ss2 = _factory_units_per_planet({"aif": 1.0}, 17000, 21315,
                                                 m3_per_unit=100,
                                                 buffer_days=1)
    check("Buffer forces storage and cuts throughput",
          _ss2 > 0 and _su2 < _su
          and _su2 * 100 * 24 <= 10000 + _ss2 * 12000 + 1e-6,
          f"units={_su2:.2f} storage={_ss2}")
    check("Absurd buffer is capacity-bound, not silently ignored",
          _factory_units_per_planet({"aif": 1.0}, 17000, 21315,
                                    m3_per_unit=10000, buffer_days=3)[0]
          < _su2)

    _LAST_RUN["state"]["cfg"]["haul_every_days"] = 3
    _LAST_RUN["state"]["cfg"]["hauler_m3"] = 55000
    _bp3 = best_pi_plays(isk_per_haul_min=100000, min_tier="P2")
    check("Cadence stored in result",
          _bp3["haul_every_days"] == 3 and _bp3["poco_buffer_m3"] == 35000)
    _ex3 = next(o for o in next(p for p in _bp3["plays"]
                                if p["output_name"] == "S")["options"]
                if o["strategy"] == "extract")
    check("Cadence: extract haul amortised over window",
          abs(_ex3["haul_min_per_day"]
              - (30 / 3 + _bp3["jita_round_trip_min"] / 3)) < 0.01,
          _ex3["haul_min_per_day"])
    _im3 = next(o for o in next(p for p in _bp3["plays"]
                                if p["output_name"] == "WM")["options"]
                if o["strategy"] == "import")
    check("Cadence: P4 import throttled by POCO buffer",
          any("POCO BUFFER" in f for f in _im3["flags"]), _im3["flags"])

    # ── Run plan (portfolio + shopping list) ──
    print("\nRun plan:")
    _LAST_RUN["state"]["cfg"]["max_planets"] = 5
    _rp = pi_run_plan(isk_per_haul_min=100000)
    check("Run plan fills every planet slot",
          _rp["slots_used"] == 5
          and sum(p["planet_count"] for p in _rp["planets"]) == 5)
    check("Deep books -> all slots on the single best product",
          len(_rp["planets"]) == 1 and _rp["planets"][0]["output_name"] == "WM",
          [p["output_name"] for p in _rp["planets"]])
    _rp_wm = _rp["planets"][0]
    _in0 = _rp_wm["inputs"][0]
    check("Shopping list = per-planet input x planet count",
          len(_rp["shopping_list"]) == 1
          and abs(_rp["shopping_list"][0]["units"]
                  - _in0["units_per_planet_per_run"] * _rp_wm["planet_count"])
          < 1e-6, _rp["shopping_list"][0]["units"])
    check("Shopping list ISK = units x walked Jita sell price",
          abs(_rp["shopping_list"][0]["isk"]
              - _rp["shopping_list"][0]["units"] * _in0["jita_sell"]) < 1)
    check("Daily drop = run quantity / haul cadence",
          abs(_in0["units_per_planet_per_day"] * 3
              - _in0["units_per_planet_per_run"]) < 1e-6)
    check("Every planet gets a named customs office, cheapest first",
          sum(len(p["sites"]) for p in _rp["planets"]) == _rp["slots_used"]
          and all(p["sites"][0]["planet"] for p in _rp["planets"]),
          [s["planet"] for p in _rp["planets"] for s in p["sites"]])
    check("Facility counts are whole numbers you can actually place",
          all(float(p["facilities"]["aif"]).is_integer()
              and float(p["facilities"]["htif"]).is_integer()
              for p in _rp["planets"]))
    check("Built facilities fit the command centre",
          all(p["pg_used"] <= p["pg_budget"]
              and p["cpu_used"] <= p["cpu_budget"] for p in _rp["planets"]))
    check("Trips sized against the hauler hold",
          _rp["trips_per_run"] == max(
              math.ceil(_rp["import_m3_per_run"] / 55000),
              math.ceil(_rp["export_m3_per_run"] / 55000)))
    check("Headline net is exactly the parts minus the haul",
          abs(_rp["total_net_isk_hr"]
              - (sum(p["total_net_isk_hr"] for p in _rp["planets"])
                 - _rp["haul_cost_isk_hr"])) < 1e-6)
    check("Headline spend is exactly the per-planet spend",
          abs(_rp["spend_per_run"]
              - sum(p["spend_per_run"] for p in _rp["planets"])) < 1e-6)
    check("Cargo totals are the sum of the planets",
          abs(_rp["import_m3_per_run"]
              - sum(p["import_m3_per_run"] for p in _rp["planets"])) < 1e-6
          and abs(_rp["export_m3_per_run"]
                  - sum(p["export_m3_per_run"] for p in _rp["planets"])) < 1e-6)
    check("Every built planet holds its own buffer",
          all(p["on_planet_m3"] <= p["on_planet_capacity_m3"] + 1e-6
              for p in _rp["planets"]))
    _shallow = dict(_iu_market)
    _shallow[30] = {**_iu_market[30], "jita_buy_book": [(1500000, 400)],
                    "jita_vwap": 200000}
    _LAST_RUN["state"]["ectx"]["market_prices"] = _shallow
    _rp2 = pi_run_plan(isk_per_haul_min=100000)
    check("Thin buy book pushes later slots onto another product",
          len(_rp2["planets"]) > 1,
          [(p["output_name"], p["planet_count"]) for p in _rp2["planets"]])
    _LAST_RUN["state"]["ectx"]["market_prices"] = _iu_market

    # ── Ceilings: working capital and hauler trips ──
    _cap = _rp["spend_per_run"] / 3.0
    _rp_cap = pi_run_plan(isk_per_haul_min=100000, capital=_cap)
    check("Capital ceiling is respected",
          _rp_cap["spend_per_run"] <= _cap + 1e-6,
          f"{_rp_cap['spend_per_run']:.0f} > {_cap:.0f}")
    check("Capital ceiling costs throughput, not correctness",
          _rp_cap["slots_used"] <= _rp["slots_used"]
          and _rp_cap["total_net_isk_hr"] <= _rp["total_net_isk_hr"] + 1e-6)
    check("Tightening capital never improves the plan",
          all(pi_run_plan(isk_per_haul_min=100000, capital=_cap * f)
              ["total_net_isk_hr"]
              <= pi_run_plan(isk_per_haul_min=100000, capital=_cap * (f + 0.25))
              ["total_net_isk_hr"] + 1e-6
              for f in (0.25, 0.5, 0.75)))
    check("Cheapest customs offices go to the biggest tax bills",
          all(p["sites"] == sorted(p["sites"], key=lambda s: s["tax_rate"])
              for p in _rp_cap["planets"])
          and [s["tax_rate"] for p in _rp["planets"] for s in p["sites"]]
          != [] and min(s["tax_rate"] for p in _rp["planets"]
                        for s in p["sites"]) == 0.01)
    _rp_trip = pi_run_plan(isk_per_haul_min=100000, max_trips=1)
    check("Trip ceiling is respected", _rp_trip["trips_per_run"] <= 1,
          _rp_trip["trips_per_run"])
    check("Trip ceiling holds every leg inside one hold",
          max(_rp_trip["import_m3_per_run"],
              _rp_trip["export_m3_per_run"]) <= 55000 + 1e-6)
    _rp_none = pi_run_plan(isk_per_haul_min=100000, capital=1.0)
    check("Impossible ceiling yields an empty, flagged plan",
          not _rp_none["planets"] and _rp_none["slots_used"] == 0
          and any("Nothing fits" in w for w in _rp_none["warnings"]))
    # An exact search must never be beaten by the plan a tighter run found.
    check("Unconstrained plan is at least as good as any constrained one",
          _rp["total_net_isk_hr"] >= _rp_cap["total_net_isk_hr"] - 1e-6
          and _rp["total_net_isk_hr"] >= _rp_trip["total_net_isk_hr"] - 1e-6)
    # ── P4 needs a Barren/Temperate planet ──
    print("\nHigh-Tech Production Plant siting:")
    check("HTIF exists only for Barren and Temperate",
          HTIF_PLANET_TYPES == {"Barren", "Temperate"})
    _ctx_rp = _LAST_RUN["state"]["ctx"]
    _inv_before, _tax_before = _ctx_rp["inv"], _ctx_rp["planet_taxes"]
    # The cheapest planets are now Gas, which cannot host a High-Tech plant.
    _ctx_rp["inv"] = {"Gasville": {"Gas": 4}, "Rockton": {"Barren": 2}}
    _ctx_rp["planet_taxes"] = {"Gasville.Gas.A": 0.01, "Gasville.Gas.B": 0.01,
                               "Gasville.Gas.C": 0.01, "Gasville.Gas.D": 0.01,
                               "Rockton.Barren.A": 0.10,
                               "Rockton.Barren.B": 0.10}
    _rp_g = pi_run_plan(isk_per_haul_min=100000)
    _p4s = [p for p in _rp_g["planets"] if p["tier"] == "P4"]
    check("P4 never sited on a planet that cannot host an HTIF",
          all(s["type"] in HTIF_PLANET_TYPES for p in _p4s for s in p["sites"]),
          [(p["output_name"], [s["planet"] for s in p["sites"]]) for p in _p4s])
    check("P4 planet count capped by Barren/Temperate availability",
          sum(p["planet_count"] for p in _p4s) <= 2,
          sum(p["planet_count"] for p in _p4s))
    check("Non-P4 products may still use the cheap Gas planets",
          all(s["tax_rate"] <= 0.10 for p in _rp_g["planets"]
              for s in p["sites"]))
    _ctx_rp["inv"] = {"Gasville": {"Gas": 6}}
    _ctx_rp["planet_taxes"] = {f"Gasville.Gas.{c}": 0.01 for c in "ABCDEF"}
    _rp_nog = pi_run_plan(isk_per_haul_min=100000)
    check("No Barren/Temperate at all -> no P4 in the plan",
          not any(p["tier"] == "P4" for p in _rp_nog["planets"]),
          [p["output_name"] for p in _rp_nog["planets"]])
    _ctx_rp["inv"], _ctx_rp["planet_taxes"] = _inv_before, _tax_before

    _rp_state = _LAST_RUN["state"]
    _LAST_RUN["state"] = None
    check("pi_run_plan errors with no run", "error" in pi_run_plan())
    _LAST_RUN["state"] = _rp_state

    _LAST_RUN["state"] = _saved_run
    check("import_upgrade_options errors with no run",
          "error" in import_upgrade_options())
    check("best_pi_plays errors with no run",
          "error" in best_pi_plays())

    print(f"\n{'='*40}")
    if errors:
        print(f"{len(errors)} check(s) FAILED:")
        for e in errors:
            print(f"  {e}")
        return 1
    else:
        print("All checks passed.")
        return 0


# ── CLI ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="PI Dossier -- Planetary Industry production chain analyser")
    parser.add_argument("--max-haul-minutes", type=float, default=None,
                        help="Override max haul minutes/day")
    parser.add_argument("--tax", type=float, default=None,
                        help="Override tax rate (e.g. 0.15)")
    parser.add_argument("--hauler-capacity", type=float, default=None,
                        help="Override hauler capacity m3")
    parser.add_argument("--max-market-jumps", type=int, default=None,
                        help="Override max market jumps")
    parser.add_argument("--top", type=int, default=20,
                        help="Show top N chains per tier (default: 20)")
    parser.add_argument("--output", type=str, default=None,
                        help="Output file path (default: pi_dossier_YYYYMMDD.md)")
    parser.add_argument("--self-test", action="store_true",
                        help="Run verification checks")

    args = parser.parse_args()

    if args.self_test:
        sys.exit(self_test())

    overrides = {}
    if args.max_haul_minutes is not None:
        overrides["max_haul_minutes"] = args.max_haul_minutes
    if args.tax is not None:
        overrides["tax_rate"] = args.tax
    if args.hauler_capacity is not None:
        overrides["hauler_m3"] = args.hauler_capacity
    if args.max_market_jumps is not None:
        overrides["max_market_jumps"] = args.max_market_jumps

    data = generate_pi_dossier_data(overrides=overrides if overrides else None)

    if "error" in data:
        print(f"Error: {data['error']}", file=sys.stderr)
        sys.exit(1)

    markdown = data["markdown"]
    print(markdown)

    # Write to file
    if args.output:
        outpath = args.output
    else:
        today = datetime.date.today().strftime("%Y%m%d")
        outpath = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               f"pi_dossier_{today}.md")
    with open(outpath, "w", encoding="utf-8") as f:
        f.write(markdown)
    print(f"\nDossier written to {outpath}")


if __name__ == "__main__":
    main()
