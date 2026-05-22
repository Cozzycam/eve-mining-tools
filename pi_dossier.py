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
import math
import os
import sys
import time

# Ensure same-directory imports work
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eve_common as esi

# ── Constants ─────────────────────────────────────────────────

EVEREF_BASE = "https://ref-data.everef.net"
CACHE_TTL_PI = 30 * 86400  # 30 days — PI schematics are very stable

# ESI group IDs for PI commodity tiers
PI_GROUPS = {
    "P0": [1032, 1033, 1035],  # Solid / Liquid-Gas / Organic raw resources
    "P1": [1042],               # Basic Commodities
    "P2": [1034],               # Refined Commodities
    "P3": [1040],               # Specialized Commodities
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

# PI facility power/CPU costs (from SDE, extremely stable)
FACILITY_COSTS = {
    "ecu_base":     {"pg": 400,  "cpu": 200},
    "ecu_per_head": {"pg": 550,  "cpu": 110},
    "bif":          {"pg": 800,  "cpu": 200},
    "aif":          {"pg": 700,  "cpu": 500},
    "launchpad":    {"pg": 700,  "cpu": 3600},
    "storage":      {"pg": 700,  "cpu": 500},
}

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

# CCU level → (powergrid, cpu) budgets (from EVE SDE)
CCU_BUDGETS = {
    0: (6000, 1675),
    1: (6000, 1675),
    2: (9000, 7057),
    3: (12000, 12136),
    4: (15000, 17215),
    5: (17000, 21315),
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
    }
    return char_info, pi_skills


def load_pi_config():
    """Load pi_config.ini."""
    cp = configparser.ConfigParser()
    cp.optionxform = str
    cp.read(_ini_path("pi_config.ini"), encoding="utf-8")

    cfg = {
        "home_system": cp.get("pi", "home_system", fallback="Jufvitte"),
        "max_market_jumps": cp.getint("pi", "max_market_jumps", fallback=5),
        "max_haul_minutes": cp.getfloat("pi", "max_haul_minutes_per_day", fallback=60),
        "tax_rate": cp.getfloat("pi", "default_tax_rate", fallback=0.15),
        "hauler_m3": cp.getfloat("pi", "hauler_capacity_m3", fallback=9000),
        "pg_budget": cp.getfloat("pi", "power_per_planet", fallback=17000),
        "cpu_budget": cp.getfloat("pi", "cpu_per_planet", fallback=21315),
        "max_planets": cp.getint("pi", "max_planets", fallback=5),
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
    """Load planet_inventory.ini → {system: {planet_type: count}}."""
    cp = configparser.ConfigParser()
    cp.optionxform = str
    cp.read(_ini_path("planet_inventory.ini"), encoding="utf-8")
    inv = {}
    for section in cp.sections():
        inv[section] = {}
        for ptype, count in cp.items(section):
            try:
                inv[section][ptype] = int(count)
            except ValueError:
                pass
    return inv


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


def _estimate_from_density(density_pct, heads=DEFAULT_ECU_HEADS):
    """Estimate P0/hr from density % using the calibrated lookup table.
    Returns 0 for 0% density — resource cannot be extracted.
    """
    if density_pct <= 0:
        return 0
    for (lo, hi), per_head in DENSITY_YIELD_PER_HEAD.items():
        if lo < density_pct <= hi:  # exclusive lower bound: 0% yields nothing
            return per_head * heads
    return 2200 * heads


def get_p0_rate(system, ptype, p0_name, extraction_rates, density_data,
                heads=DEFAULT_ECU_HEADS):
    """Get best P0 extraction rate across all planet instances, with source tag.

    Searches all instances (A, B, C...) for system+ptype and returns the
    best rate found. Priority: observed (OBS) > density estimate (EST) > default (DFL).
    Returns: (rate_p0_per_hr, source_tag, instance)
    """
    best_rate = 0
    best_tag = "DFL"
    best_instance = "A"

    # Find all instances for this system.ptype
    prefix = f"{system}.{ptype}."
    instance_keys = [k for k in extraction_rates if k.startswith(prefix)]
    instance_keys += [k for k in density_data if k.startswith(prefix) and k not in instance_keys]

    # If no specific instance data, check legacy key without instance
    if not instance_keys:
        instance_keys = [f"{system}.{ptype}.A"]

    has_any_scan_data = False

    for key in instance_keys:
        instance = key.split(".")[-1]

        # Check observed
        obs = extraction_rates.get(key, {}).get(p0_name)
        if obs is not None and obs > 0:
            if obs > best_rate:
                best_rate = obs
                best_tag = "OBS"
                best_instance = instance
            has_any_scan_data = True
            continue

        # Check density estimate
        dens_for_planet = density_data.get(key, {})
        if dens_for_planet:
            # This planet has been scanned
            has_any_scan_data = True
            density = dens_for_planet.get(p0_name)
            if density is not None and density > 0:
                est = _estimate_from_density(density, heads)
                if est > best_rate:
                    best_rate = est
                    best_tag = "EST"
                    best_instance = instance
            # else: resource is 0% or absent on a scanned planet → yields 0

    if best_rate == 0:
        if has_any_scan_data:
            # Scanned planet(s) exist but this resource isn't available → 0
            return 0, "EST:0%", "?"
        # No data at all — use conservative default
        return DEFAULT_EXTRACTION_RATE, "DFL", "?"

    return best_rate, best_tag, best_instance


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
    """Compute VWAP and volume stats over the last N days of history.

    Returns: {vwap, avg_daily_volume, total_volume, active_days, days_sampled}
    """
    recent = history[-days:] if len(history) >= days else history
    if not recent:
        return {"vwap": 0, "avg_daily_volume": 0, "total_volume": 0,
                "active_days": 0, "days_sampled": 0}

    total_value = 0
    total_volume = 0
    active_days = 0
    for d in recent:
        vol = d.get("volume", 0)
        avg = d.get("average", 0)
        if vol > 0:
            total_value += avg * vol
            total_volume += vol
            active_days += 1

    vwap = total_value / total_volume if total_volume > 0 else 0

    return {
        "vwap": vwap,
        "avg_daily_volume": total_volume / len(recent),
        "total_volume": total_volume,
        "active_days": active_days,
        "days_sampled": len(recent),
    }


SUSTAINED_PRICE_WINDOW_DAYS = 30  # blend price over this many days of production
SHALLOW_BUY_THRESHOLD_DAYS = 7   # flag if real buy depth < this many days
MIN_REAL_ORDER_PRICE = 2         # orders at <= this price are treated as stubs


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
            jumps = esi.get_jump_count(home_system_id, sys_id)
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
            local_buyer_jumps = esi.get_jump_count(home_system_id, sys_id)
            if local_buyer_jumps < 0:
                local_buyer_jumps = 0

        # Jita: history + live buy (no jump filtering — it's a destination)
        jita_hist = _fetch_market_history(jita_region_id, tid)
        jita_stats = _compute_history_stats(jita_hist, days=30)
        jita_live, _ = esi.fetch_best_buy(jita_region_id, tid, use_cache=True)

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
            # Jita
            "jita_vwap": jita_stats["vwap"],
            "jita_avg_daily_vol": jita_stats["avg_daily_volume"],
            "jita_total_vol": jita_stats["total_volume"],
            "jita_active_days": jita_stats["active_days"],
            "jita_buy": jita_live,
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


def _planet_budget_remaining(pg_budget, cpu_budget, facilities):
    """Calculate remaining PG/CPU after placing facilities."""
    pg_used = 0
    cpu_used = 0
    c = FACILITY_COSTS
    for f, count in facilities.items():
        if f == "ecu":
            epg, ecpu = _ecu_pg_cpu()
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


def compute_p1_layout(chain, extraction_rate, pg_budget, cpu_budget):
    """Compute layout for a P1 extraction planet.

    Returns: {facilities, units_hr, volume_hr, pg_used, cpu_used} or None
    """
    p0_per_hr = extraction_rate
    bif_input_rate = 6000

    # How many full BIFs can the extraction rate support?
    # Always place at least 1 BIF (it runs at partial capacity if underfed)
    max_bifs = max(1, int(p0_per_hr / bif_input_rate))

    for num_bifs in range(max_bifs, 0, -1):
        facilities = {"ecu": 1, "bif": num_bifs, "launchpad": 1}
        pg_rem, cpu_rem = _planet_budget_remaining(pg_budget, cpu_budget, facilities)
        if pg_rem >= 0 and cpu_rem >= 0:
            # Actual P1 output accounts for partial BIF feeding
            units_hr = _bif_p1_output(p0_per_hr, num_bifs)
            volume_hr = units_hr * chain["volume"]
            return {
                "facilities": facilities,
                "units_hr": units_hr,
                "volume_hr": volume_hr,
                "pg_used": pg_budget - pg_rem,
                "cpu_used": cpu_budget - cpu_rem,
                "role": "extractor",
                "p0_consumed_hr": min(p0_per_hr, num_bifs * bif_input_rate),
            }

    return None


def compute_p2_selfcontained_layout(chain, extraction_rates, pg_budget, cpu_budget):
    """Compute layout for a self-contained P2 planet (both P0s on same planet).

    extraction_rates: list of 2 P0 extraction rates for the planet.
    Returns layout dict or None.
    """
    bif_input_rate = 6000
    aif_p1_input_rate = 40  # P1/hr each type per AIF
    aif_output_rate = 5     # P2/hr per AIF

    # Each P0 line: 1+ BIFs, actual P1 output depends on extraction rate
    bifs_per_p0 = []
    p1_outputs = []
    for rate in extraction_rates:
        num_bifs = max(1, int(rate / bif_input_rate))
        bifs_per_p0.append(num_bifs)
        p1_outputs.append(_bif_p1_output(rate, num_bifs))

    # AIF throughput limited by the slower P1 line
    min_p1 = min(p1_outputs)
    # Each AIF needs 40 P1/hr from each input; fractional throughput allowed
    max_aifs_by_p1 = min_p1 / aif_p1_input_rate  # may be < 1.0

    # Try fitting AIFs (at least 1) within PG/CPU budget
    total_bifs = sum(bifs_per_p0)
    for num_aifs in range(max(1, int(max_aifs_by_p1)), 0, -1):
        facilities = {"ecu": 2, "bif": total_bifs, "aif": num_aifs, "launchpad": 1}
        pg_rem, cpu_rem = _planet_budget_remaining(pg_budget, cpu_budget, facilities)
        if pg_rem >= 0 and cpu_rem >= 0:
            # Actual output: limited by slower P1 line and AIF count
            effective_aif_throughput = min(num_aifs, max_aifs_by_p1)
            units_hr = effective_aif_throughput * aif_output_rate
            volume_hr = units_hr * chain["volume"]
            return {
                "facilities": facilities,
                "units_hr": units_hr,
                "volume_hr": volume_hr,
                "pg_used": pg_budget - pg_rem,
                "cpu_used": cpu_budget - cpu_rem,
                "role": "self-contained",
                "p0_consumed_hr": [min(r, b * bif_input_rate) for r, b in zip(extraction_rates, bifs_per_p0)],
            }

    return None


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


# ── Chain analysis ────────────────────────────────────────────

def find_viable_chains(chains, pi_types, schematics, planet_inv,
                       extraction_rates, density_data, cfg):
    """For each producible chain, compute layout options and economics.

    extraction_rates: {(system, ptype): {p0_name: rate}} — observed
    density_data: {(system, ptype): {p0_name: density_pct}} — scanned
    Returns list of analysed chain dicts, sorted by viability (not yet ranked by ISK).
    """
    pg_budget = cfg["pg_budget"]
    cpu_budget = cfg["cpu_budget"]

    # Flatten inventory: {planet_type: [(system, count), ...]}
    flat_inv = {}
    for system, planets in planet_inv.items():
        for ptype, count in planets.items():
            flat_inv.setdefault(ptype, []).append((system, count))

    # Total count per planet type
    total_by_type = {}
    for ptype, entries in flat_inv.items():
        total_by_type[ptype] = sum(c for _, c in entries)

    rate_ctx = {
        "extraction_rates": extraction_rates,
        "density_data": density_data,
    }

    results = []

    for tid, chain in chains.items():
        tier = chain["tier"]

        if tier == "P1":
            result = _analyse_p1_chain(chain, pi_types, flat_inv, total_by_type,
                                       rate_ctx, pg_budget, cpu_budget)
        elif tier == "P2":
            result = _analyse_p2_chain(chain, pi_types, schematics, flat_inv,
                                       total_by_type, rate_ctx,
                                       pg_budget, cpu_budget)
        elif tier == "P3":
            result = _analyse_p3_chain(chain, pi_types, schematics, flat_inv,
                                       total_by_type, rate_ctx,
                                       pg_budget, cpu_budget)
        else:
            continue

        if result:
            results.append(result)

    return results


def _get_p0_rate_for_planet(system, ptype, p0_name, rate_ctx):
    """Get P0 rate for a specific resource on a specific planet type in a system.
    Returns: (rate, tag) — drops instance for backward compat with callers.
    """
    rate, tag, _instance = get_p0_rate(system, ptype, p0_name,
                                        rate_ctx["extraction_rates"],
                                        rate_ctx["density_data"])
    return rate, tag


def _best_system_for_p0(p0_name, compatible_ptypes, flat_inv, rate_ctx):
    """Find best system+planet type for extracting a specific P0 resource.

    Returns: (system, ptype, rate, source_tag) or (None, None, 0, "")
    """
    best_sys = None
    best_ptype = None
    best_rate = 0
    best_tag = ""
    for ptype in compatible_ptypes:
        entries = flat_inv.get(ptype, [])
        for system, count in entries:
            if count <= 0:
                continue
            rate, tag = _get_p0_rate_for_planet(system, ptype, p0_name, rate_ctx)
            if rate > best_rate:
                best_rate = rate
                best_sys = system
                best_ptype = ptype
                best_tag = tag
    return best_sys, best_ptype, best_rate, best_tag


def _analyse_p1_chain(chain, pi_types, flat_inv, total_by_type,
                      rate_ctx, pg_budget, cpu_budget):
    """Analyse a P1 chain (single extraction planet)."""
    if not chain["p0_inputs"]:
        return None

    p0_name = chain["p0_inputs"][0]["name"]
    compatible_ptypes = P0_PLANET_MAP.get(p0_name, set())

    best_sys, best_ptype, best_rate, tag = _best_system_for_p0(
        p0_name, compatible_ptypes, flat_inv, rate_ctx)

    if not best_sys:
        return {
            "chain": chain, "viable": False,
            "flags": [f"NO {', '.join(compatible_ptypes)}"],
            "planets_used": [], "units_hr": 0, "volume_hr": 0,
        }

    layout = compute_p1_layout(chain, best_rate, pg_budget, cpu_budget)
    if not layout:
        return {
            "chain": chain, "viable": False, "flags": ["POWER LIMIT"],
            "planets_used": [], "units_hr": 0, "volume_hr": 0,
        }

    rate_detail = f"{p0_name}: {best_rate:.0f}/hr [{tag}]"
    return {
        "chain": chain, "viable": True, "layout_type": "p1_extractor",
        "planets_used": [{"system": best_sys, "type": best_ptype,
                          "role": f"Extract {p0_name} -> {chain['output_name']}",
                          "layout": layout, "rate_detail": rate_detail}],
        "planet_count": 1,
        "units_hr": layout["units_hr"],
        "volume_hr": layout["volume_hr"],
        "rate_sources": [tag],
        "flags": [],
    }


def _analyse_p2_chain(chain, pi_types, schematics, flat_inv, total_by_type,
                      rate_ctx, pg_budget, cpu_budget):
    """Analyse a P2 chain. Try self-contained first, then factory+extractors."""
    if len(chain["p0_inputs"]) < 2:
        return None

    p0_names = [p["name"] for p in chain["p0_inputs"]]
    p0_ptypes = [P0_PLANET_MAP.get(n, set()) for n in p0_names]

    # Try self-contained: find a planet type that has BOTH P0s
    common_ptypes = set.intersection(*p0_ptypes) if p0_ptypes else set()
    best_selfcontained = None

    for ptype in common_ptypes:
        entries = flat_inv.get(ptype, [])
        for system, count in entries:
            if count <= 0:
                continue
            # Get per-resource rates for both P0s on this planet
            rates = []
            tags = []
            for p0_name in p0_names:
                rate, tag = _get_p0_rate_for_planet(system, ptype, p0_name, rate_ctx)
                rates.append(rate)
                tags.append(tag)

            layout = compute_p2_selfcontained_layout(
                chain, rates, pg_budget, cpu_budget)
            if layout:
                if not best_selfcontained or layout["units_hr"] > best_selfcontained["layout"]["units_hr"]:
                    best_selfcontained = {
                        "system": system, "type": ptype, "layout": layout,
                        "rates": rates, "tags": tags,
                    }

    if best_selfcontained:
        layout = best_selfcontained["layout"]
        rate_details = []
        bottleneck_idx = best_selfcontained["rates"].index(min(best_selfcontained["rates"]))
        for i, (p0_name, rate, tag) in enumerate(
                zip(p0_names, best_selfcontained["rates"], best_selfcontained["tags"])):
            bif_input = 6000
            headroom = (rate - bif_input) / bif_input * 100 if rate > 0 else 0
            marker = " <- BOTTLENECK" if i == bottleneck_idx and len(p0_names) > 1 else ""
            rate_details.append(f"{p0_name}: {rate:.0f}/hr [{tag}] "
                                f"({headroom:+.0f}% headroom){marker}")

        return {
            "chain": chain, "viable": True, "layout_type": "p2_selfcontained",
            "planets_used": [{
                "system": best_selfcontained["system"],
                "type": best_selfcontained["type"],
                "role": f"Extract+Process -> {chain['output_name']}",
                "layout": layout,
                "rate_details": rate_details,
            }],
            "planet_count": 1,
            "units_hr": layout["units_hr"],
            "volume_hr": layout["volume_hr"],
            "rate_sources": best_selfcontained["tags"],
            "flags": [],
        }

    # Try factory setup: separate extraction planets + factory planet
    extraction_planets = []
    all_tags = []
    for i, p0_name in enumerate(p0_names):
        best_sys, best_ptype, best_rate, tag = _best_system_for_p0(
            p0_name, p0_ptypes[i], flat_inv, rate_ctx)
        if not best_sys:
            return {
                "chain": chain, "viable": False,
                "flags": [f"NO {', '.join(p0_ptypes[i])}"],
                "planets_used": [], "units_hr": 0, "volume_hr": 0,
            }

        p1_input = chain["inputs"][i]
        p1_type = pi_types.get(p1_input["type_id"])
        if not p1_type:
            continue

        p1_chain = {"volume": p1_type["volume"], "tier": "P1"}
        p1_layout = compute_p1_layout(p1_chain, best_rate, pg_budget, cpu_budget)
        if not p1_layout:
            return {
                "chain": chain, "viable": False, "flags": ["POWER LIMIT"],
                "planets_used": [], "units_hr": 0, "volume_hr": 0,
            }

        extraction_planets.append({
            "system": best_sys, "type": best_ptype,
            "role": f"Extract {p0_name} -> {p1_type['name']}",
            "layout": p1_layout,
            "p1_output_hr": p1_layout["units_hr"],
            "rate_detail": f"{p0_name}: {best_rate:.0f}/hr [{tag}]",
        })
        all_tags.append(tag)

    factory_layout = compute_factory_layout(chain, pg_budget, cpu_budget)
    if not factory_layout:
        return {
            "chain": chain, "viable": False, "flags": ["POWER LIMIT"],
            "planets_used": [], "units_hr": 0, "volume_hr": 0,
        }

    aif_p1_need = 40
    min_p1_supply = min(ep["p1_output_hr"] for ep in extraction_planets)
    max_aifs_by_supply = max(1, int(min_p1_supply / aif_p1_need))
    actual_aifs = min(factory_layout["facilities"]["aif"], max_aifs_by_supply)

    units_hr = actual_aifs * 5
    volume_hr = units_hr * chain["volume"]

    planets = list(extraction_planets)
    planets.append({
        "system": extraction_planets[0]["system"],
        "type": "Any",
        "role": f"Factory -> {chain['output_name']}",
        "layout": factory_layout,
    })

    return {
        "chain": chain, "viable": True, "layout_type": "p2_factory",
        "planets_used": planets, "planet_count": len(planets),
        "units_hr": units_hr, "volume_hr": volume_hr,
        "rate_sources": all_tags, "flags": [],
    }


def _analyse_p3_chain(chain, pi_types, schematics, flat_inv, total_by_type,
                      rate_ctx, pg_budget, cpu_budget):
    """Analyse a P3 chain. These need multiple planets."""
    # P3 needs 2-3 P2 inputs, each of which needs its own P1+P0 chain
    # Count total planets needed
    p0_names = list(chain["all_p0_names"])

    # Each unique P0 needs at least one extraction planet
    # Plus at least 1 factory planet for P2 production, 1 for P3 production
    extraction_count = len(p0_names)
    factory_count = 2  # at minimum: P2 factory + P3 factory (could share)
    total_needed = extraction_count + 1  # Simplified: extractors + 1 combined factory

    flags = []
    if total_needed > 5:
        flags.append("EXCEEDS 5 PLANETS")

    # Check planet availability
    for p0_name in p0_names:
        ptypes = P0_PLANET_MAP.get(p0_name, set())
        available = any(total_by_type.get(pt, 0) > 0 for pt in ptypes)
        if not available:
            flags.append(f"NO {', '.join(ptypes)} (for {p0_name})")

    # Estimate output: P3 factory output
    factory_layout = compute_factory_layout(chain, pg_budget, cpu_budget)
    if not factory_layout:
        flags.append("POWER LIMIT")
        return {
            "chain": chain, "viable": False, "flags": flags,
            "planets_used": [], "units_hr": 0, "volume_hr": 0,
        }

    # P3 AIF: 3 units/hr per AIF (60 min cycle, 3 output)
    # Limited to probably 1-2 AIFs in practice due to input supply
    estimated_aifs = min(factory_layout["facilities"]["aif"], 2)
    units_hr = estimated_aifs * 3
    volume_hr = units_hr * chain["volume"]

    viable = "EXCEEDS 5 PLANETS" not in flags and not any("NO " in f for f in flags)

    return {
        "chain": chain,
        "viable": viable,
        "layout_type": "p3_multi",
        "planets_used": [],  # Complex — show planet count estimate
        "planet_count": total_needed,
        "units_hr": units_hr,
        "volume_hr": volume_hr,
        "flags": flags,
    }


# ── Economics ─────────────────────────────────────────────────

def compute_economics(viable_chains, market_prices, cfg, pi_types):
    """Compute ISK/hr, tax, haul time for each viable chain.

    Primary price: sustained realised price — walks the buy order book within
    max_market_jumps and blends with VWAP for production beyond order depth.
    This models what you'd actually earn over 30 days, not just the instant
    top-of-book price.
    """
    home_system_id = esi.search_system_id(cfg["home_system"])
    jita_system_id = esi.search_system_id("Jita")
    jita_jumps = 0
    if home_system_id and jita_system_id:
        jita_jumps = esi.get_jump_count(home_system_id, jita_system_id)
        if jita_jumps < 0:
            jita_jumps = 15  # fallback

    for vc in viable_chains:
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
        vc["jita_vwap"] = jita_vwap
        vc["jita_buy_price"] = jita_buy
        vc["jita_avg_daily_vol"] = jita_avg_daily_vol
        vc["jita_active_days"] = jita_active_days

        # Gross ISK/hr uses sustained price (realistic over 30 days)
        vc["gross_isk_hr"] = units_hr * sustained_price

        # Tax
        tax_per_unit = _compute_chain_tax(vc, pi_types, cfg)
        vc["tax_per_hr"] = tax_per_unit * units_hr

        # Net ISK/hr = gross - tax
        vc["net_isk_hr"] = vc["gross_isk_hr"] - vc["tax_per_hr"]

        # Activity-adjusted ISK/hr — penalises products that rarely trade.
        # If a product only trades 6 of 30 days, production sits unsold 80%
        # of the time. Used for ranking and allocator, not displayed as "real"
        # ISK/hr — the raw net_isk_hr is what you earn when you DO sell.
        activity_factor = min(local_active_days / 30.0, 1.0) if local_active_days < 30 else 1.0
        vc["activity_factor"] = activity_factor
        vc["adjusted_net_isk_hr"] = vc["net_isk_hr"] * activity_factor

        # Jita ISK/hr (using Jita VWAP — you'd sell over time there)
        haul_model = cfg["haul"]
        jita_round_trip_sec = (jita_jumps * 2 * haul_model["sec_per_jump"]
                               + 2 * haul_model["sec_per_station"])
        jita_round_trip_min = jita_round_trip_sec / 60
        vc["jita_gross_isk_hr"] = units_hr * jita_vwap
        vc["jita_haul_min"] = jita_round_trip_min

        # Haul time for daily production
        vc["haul_minutes_per_day"] = _compute_haul_time(vc, cfg)

        # ── Flags ──

        # NO LOCAL BUYER: no buy orders within jump range
        if local_buy == 0:
            if local_vwap > 100:
                vc["flags"].append("NO LOCAL BUYER")
            elif jita_vwap > 100:
                vc["flags"].append("NO LOCAL MARKET")

        # SHALLOW BUY: real buy order depth covers less than 7 days of production
        if real_buy_days < SHALLOW_BUY_THRESHOLD_DAYS and units_per_day > 0:
            if real_buy_days > 0:
                vc["flags"].append(f"SHALLOW BUY ({real_buy_days:.0f}d depth)")
            elif local_buy > MIN_REAL_ORDER_PRICE:
                vc["flags"].append("SHALLOW BUY (<1d depth)")

        # Liquidity from history
        if local_active_days < 10:
            vc["flags"].append(f"LOW ACTIVITY ({local_active_days}d/30d)")

        if vc["haul_minutes_per_day"] > cfg["max_haul_minutes"]:
            vc["flags"].append("HAUL OVER BUDGET")

        # Launchpad fill time check
        if units_hr > 0 and chain["volume"] > 0:
            lp_capacity = 10000  # m³
            fill_hours = lp_capacity / (units_hr * chain["volume"])
            if fill_hours < 24:
                vc["flags"].append(f"MUST HAUL EVERY {fill_hours:.0f}H")

        # Jita spread: compare in-range buy vs Jita VWAP
        if local_buy > 10 and jita_vwap > local_buy:
            spread_pct = (jita_vwap - local_buy) / local_buy * 100
            if spread_pct > 30:
                vc["flags"].append(f"JITA +{spread_pct:.0f}%")


def _compute_chain_tax(vc, pi_types, cfg):
    """Compute total POCO tax per unit of output product."""
    tax_rate = cfg["tax_rate"]
    layout_type = vc.get("layout_type", "")

    chain = vc["chain"]
    output_base = chain["base_price"]

    if layout_type == "p1_extractor":
        # P1 extraction: product exported from planet
        # Export tax on P1
        return output_base * 0.5 * tax_rate

    elif layout_type == "p2_selfcontained":
        # P2 self-contained: product exported from planet
        # Only export tax on P2 (P0→P1→P2 all on-planet, no POCO transitions)
        return output_base * 0.5 * tax_rate

    elif layout_type == "p2_factory":
        # P2 factory: P1 exported from extraction planets + imported to factory + P2 exported
        # Per unit of P2:
        #   - Each P1 input: export from extractor planet + import to factory planet
        #   - P2: export from factory planet
        total_tax = 0
        for inp in chain["inputs"]:
            inp_type = pi_types.get(inp["type_id"])
            if inp_type:
                p1_base = inp_type["base_price"]
                # Export P1 from extractor + Import P1 to factory
                # Quantity: per cycle the AIF needs inp["quantity"] P1 per cycle
                # Per unit P2 output: inp["quantity"] / output_quantity
                output_qty = chain["schematic"]["output"]["quantity"]
                p1_per_p2 = inp["quantity"] / output_qty
                total_tax += p1_per_p2 * p1_base * 0.5 * tax_rate * 2  # export + import
        # Export P2 from factory
        total_tax += output_base * 0.5 * tax_rate
        return total_tax

    elif layout_type == "p3_multi":
        # Simplified: count all POCO transitions
        # P0→P1 export, P1 import to P2 factory, P2 export,
        # P2 import to P3 factory, P3 export
        total_tax = 0
        # P1 exports + imports (rough: 2 transitions per P1 type)
        for p0 in chain["p0_inputs"]:
            total_tax += 1 * 0.5 * tax_rate * 2  # base_price ~1 for P1
        # P2 exports + imports
        for inp in chain["inputs"]:
            inp_type = pi_types.get(inp["type_id"])
            if inp_type:
                output_qty = chain["schematic"]["output"]["quantity"]
                p2_per_p3 = inp["quantity"] / output_qty
                total_tax += p2_per_p3 * inp_type["base_price"] * 0.5 * tax_rate * 2
        # P3 export
        total_tax += output_base * 0.5 * tax_rate
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

    return 0


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
            ptype = p.get("type")
            system = p.get("system")
            if ptype and ptype != "Any" and system:
                key = (system, ptype)
                needed[key] = needed.get(key, 0) + 1

        can_allocate = True
        for (sys, ptype), count in needed.items():
            avail = remaining.get(sys, {}).get(ptype, 0)
            if avail < count:
                can_allocate = False
                break

        if not can_allocate:
            continue

        for (sys, ptype), count in needed.items():
            remaining[sys][ptype] -= count

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
        projections.append({
            "skill": f"Interplanetary Consolidation {next_level}",
            "effect": f"+1 planet slot ({next_level + 1} total)",
            "detail": "Allows one more production planet",
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
                    projections, market_prices, pi_types):
    """Generate the full dossier markdown."""
    lines = []
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    lines.append(f"# PI Dossier -- {char_info['name']} @ {cfg['home_system']}")
    lines.append(f"**Generated:** {now}")
    lines.append(f"**Skills:** CCU {pi_skills['ccu']}, IC {pi_skills['ic']}, "
                 f"Planetology {pi_skills['planetology']}, "
                 f"Adv Planetology {pi_skills['adv_planetology']}")
    lines.append(f"**Tax:** {cfg['tax_rate']*100:.0f}% (default)  |  "
                 f"**Hauler:** {_fmt_num(cfg['hauler_m3'])} m3  |  "
                 f"**Max haul:** {cfg['max_haul_minutes']:.0f} min/day")
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
    for tier in ["P1", "P2", "P3"]:
        tier_chains = [vc for vc in ranked_by_tier if vc["chain"]["tier"] == tier]
        if not tier_chains:
            continue

        tier_label = {"P1": "P1 (Self-contained extraction)",
                      "P2": "P2 (Refined Commodities)",
                      "P3": "P3 (Specialized Commodities)"}
        lines.append(f"### {tier_label.get(tier, tier)}")
        lines.append("")
        lines.append("| Rank | Product | Setup | Units/hr | Sustained | Net ISK/hr | Adj ISK/hr | Haul/day | Flags |")
        lines.append("|------|---------|-------|----------|-----------|------------|------------|----------|-------|")

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
            else:
                setup_str = setup

            flags_str = ", ".join(vc.get("flags", [])) if vc.get("flags") else "--"
            if not vc.get("viable"):
                flags_str = ", ".join(vc.get("flags", ["NOT VIABLE"]))

            net = vc.get("net_isk_hr", 0)
            adj = vc.get("adjusted_net_isk_hr", 0)
            # Only show adjusted if different from net (activity < 1.0)
            adj_str = f"{_fmt_isk(adj)}/hr" if abs(adj - net) > 1 else "="

            lines.append(
                f"| {rank} | {chain['output_name']} | {setup_str} | "
                f"{vc.get('units_hr',0):.0f} | {_fmt_isk(vc.get('local_sustained',0))} | "
                f"{_fmt_isk(net)}/hr | {adj_str} | "
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
    lines.append("- **Adj ISK/hr**: net x activity factor (days_traded/30). Penalises thin markets where production sits unsold. '=' means no penalty. Rankings use this.")
    lines.append("- **SHALLOW BUY**: real buy orders (>2 ISK) cover <7 days of production")
    lines.append("- **NO LOCAL BUYER**: no buy orders within jump range")
    lines.append("- **NO LOCAL MARKET**: no meaningful trade activity in region")
    lines.append("- **LOW ACTIVITY**: traded fewer than 10 of last 30 days")
    lines.append("- **HAUL OVER BUDGET**: daily haul exceeds max_haul_minutes_per_day")
    lines.append("- **POWER LIMIT**: layout pushes against PG or CPU ceiling")
    lines.append("- **NO [PLANET TYPE]**: chain needs a planet type not in your inventory")
    lines.append("- **MUST HAUL EVERY XH**: launchpad fills before 24h")
    lines.append("- **EXCEEDS 5 PLANETS**: chain needs more planet slots than available")
    lines.append("- **JITA +X%**: Jita VWAP significantly higher than local buy")
    lines.append("")

    return "\n".join(lines)


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

    # Resolve home system
    home_system_id = esi.search_system_id(cfg["home_system"])
    if not home_system_id:
        return {"error": f"Home system '{cfg['home_system']}' not found in ESI."}

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

    print("  PI Dossier: computing layouts and economics...")
    viable = find_viable_chains(chains, pi_types, schematics,
                                planet_inv, extraction_rates, density_data, cfg)
    compute_economics(viable, market_prices, cfg, pi_types)

    ranked = rank_chains(viable)
    layouts = allocate_5_planets(ranked, planet_inv, cfg["max_planets"],
                                 max_haul_minutes=cfg["max_haul_minutes"])

    projections = compute_projections(ranked[:5], pi_skills, cfg)

    markdown = render_markdown(layouts, ranked, cfg, char_info, pi_skills,
                               projections, market_prices, pi_types)

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

    layouts_json = []
    for layout in layouts:
        layout_entries = []
        for vc in layout["allocated"]:
            chain = vc["chain"]
            layout_entries.append({
                "output_name": chain["output_name"],
                "tier": chain["tier"],
                "layout_type": vc.get("layout_type", ""),
                "units_hr": vc.get("units_hr", 0),
                "net_isk_hr": vc.get("net_isk_hr", 0),
                "planets_used": [
                    {"system": p.get("system", ""), "type": p.get("type", ""),
                     "role": p.get("role", "")}
                    for p in vc.get("planets_used", [])
                ],
            })
        layouts_json.append({
            "strategy": layout["strategy"],
            "total_net": layout["total_net"],
            "allocated": layout_entries,
        })

    return {
        "char_info": char_info,
        "pi_skills": pi_skills,
        "config": {
            "home_system": cfg["home_system"],
            "tax_rate": cfg["tax_rate"],
            "hauler_m3": cfg["hauler_m3"],
            "max_haul_minutes": cfg["max_haul_minutes"],
            "max_market_jumps": cfg["max_market_jumps"],
            "pg_budget": cfg["pg_budget"],
            "cpu_budget": cfg["cpu_budget"],
            "max_planets": cfg["max_planets"],
        },
        "planet_inventory": planet_inv,
        "extraction_rates": extraction_rates,
        "density_data": density_data,
        "chains": chains_json,
        "layouts": layouts_json,
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
                cp.set(section_key, _name_to_underscore(resource), str(int(pct)))
    path = _ini_path("planet_density.ini")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# planet_density.ini\n")
        f.write("# Per-resource density % from in-game scan\n\n")
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

    factory = compute_factory_layout(test_chain_p2, 17000, 21315)
    check("P2 factory layout computes", factory is not None)
    if factory:
        check("P2 factory has AIFs", factory["facilities"]["aif"] > 0,
              f"aifs={factory['facilities']['aif']}")

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
