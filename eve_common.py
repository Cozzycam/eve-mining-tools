"""Shared ESI utilities for EVE Online tools.

Extracted from ore_scanner.py to support multiple tools (ore_scanner, fit_dossier).
Stdlib only — no third-party dependencies.
"""

import hashlib
import json
import os
import time
import urllib.request
import urllib.error

# ── Constants ──────────────────────────────────────────────────

ESI_BASE = "https://esi.evetech.net/latest"
ESI_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "EveTools/1.0 (Campbell's eve-tools)",
}

CACHE_DIR = os.path.join(os.path.expanduser("~"), ".eve-tools-cache")

# Cache TTLs (seconds)
CACHE_TTL_STATIC = 7 * 86400   # 7 days — type info, dogma, groups
CACHE_TTL_MARKET = 300          # 5 minutes — market orders

# ── Regions ────────────────────────────────────────────────────

REGION_LIST = [
    {"key": "verge",   "id": 10000068, "name": "Verge Vendor"},
    {"key": "dodixie", "id": 10000032, "name": "Sinq Laison (Dodixie)"},
    {"key": "jita",    "id": 10000002, "name": "The Forge (Jita)"},
    {"key": "amarr",   "id": 10000043, "name": "Domain (Amarr)"},
    {"key": "hek",     "id": 10000042, "name": "Metropolis (Hek)"},
    {"key": "rens",    "id": 10000030, "name": "Heimatar (Rens)"},
]
REGIONS = {r["key"]: r for r in REGION_LIST}

# ── In-memory caches (for long-running processes like ore_scanner) ──

_name_cache = {}
_route_cache = {}
_system_id_cache = {}


# ── File-backed cache ─────────────────────────────────────────

def _cache_path(key):
    """Return filesystem path for a cache key."""
    h = hashlib.sha256(key.encode()).hexdigest()[:16]
    return os.path.join(CACHE_DIR, f"{h}.json")


def cache_get(key, ttl_seconds):
    """Return cached data if fresh, else None."""
    path = _cache_path(key)
    try:
        with open(path, "r") as f:
            entry = json.load(f)
        if time.time() - entry["ts"] < ttl_seconds:
            return entry["data"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError, OSError):
        pass
    return None


def cache_set(key, data):
    """Store data in file cache."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(key)
    try:
        with open(path, "w") as f:
            json.dump({"ts": time.time(), "data": data}, f)
    except OSError:
        pass


def cache_clear():
    """Remove all cached files."""
    if os.path.isdir(CACHE_DIR):
        for fname in os.listdir(CACHE_DIR):
            if fname.endswith(".json"):
                try:
                    os.remove(os.path.join(CACHE_DIR, fname))
                except OSError:
                    pass


# ── ESI HTTP helpers ──────────────────────────────────────────

def esi_get(url):
    """HTTP GET to ESI. Returns parsed JSON or None on error."""
    req = urllib.request.Request(url, headers=ESI_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return None


def esi_get_cached(url, ttl=CACHE_TTL_STATIC):
    """ESI GET with file-backed caching and retry on failure."""
    cached = cache_get(url, ttl)
    if cached is not None:
        return cached
    for attempt in range(3):
        data = esi_get(url)
        if data is not None:
            cache_set(url, data)
            return data
        if attempt < 2:
            time.sleep(0.5 * (attempt + 1))
    return None


def esi_post(url, payload):
    """HTTP POST to ESI. Returns parsed JSON or None on error."""
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers={
        **ESI_HEADERS, "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return None


# ── Name resolution ───────────────────────────────────────────

def resolve_system_name(system_id):
    """Resolve solar system ID to name."""
    key = f"sys:{system_id}"
    if key in _name_cache:
        return _name_cache[key]
    data = esi_get(f"{ESI_BASE}/universe/systems/{system_id}/?datasource=tranquility")
    name = data["name"] if data and "name" in data else f"System {system_id}"
    _name_cache[key] = name
    return name


def resolve_station_name(location_id):
    """Resolve station/structure ID to name."""
    key = f"sta:{location_id}"
    if key in _name_cache:
        return _name_cache[key]
    if 60000000 <= location_id <= 64000000:
        data = esi_get(f"{ESI_BASE}/universe/stations/{location_id}/?datasource=tranquility")
        name = data["name"] if data and "name" in data else None
    else:
        name = None
    _name_cache[key] = name
    return name


def search_system_id(name):
    """Resolve a solar system name to its ID."""
    key = name.strip().lower()
    if key in _system_id_cache:
        return _system_id_cache[key]
    data = esi_post(f"{ESI_BASE}/universe/ids/?datasource=tranquility", [name])
    if data and "systems" in data and data["systems"]:
        sid = data["systems"][0]["id"]
        _system_id_cache[key] = sid
        return sid
    return None


# ── Routes ────────────────────────────────────────────────────

def get_jump_count(origin_id, dest_id, avoid=None):
    """Get shortest jump count between two solar systems.

    avoid: optional iterable of system IDs the route must not pass
    through (ESI route `avoid` param). Origin/destination are dropped
    from the avoid list automatically.
    """
    if origin_id == dest_id:
        return 0
    avoid_ids = tuple(sorted(set(avoid or ()) - {origin_id, dest_id}))
    key = (origin_id, dest_id, avoid_ids)
    if key in _route_cache:
        return _route_cache[key]
    url = f"{ESI_BASE}/route/{origin_id}/{dest_id}/?datasource=tranquility&flag=shortest"
    if avoid_ids:
        url += "&avoid=" + ",".join(str(i) for i in avoid_ids)
    data = esi_get(url)
    if data and isinstance(data, list):
        jumps = len(data) - 1
    else:
        jumps = -1
    _route_cache[key] = jumps
    return jumps


# ── Jump matrix and positions ────────────────────────────────

CACHE_TTL_ROUTE = 30 * 86400  # 30 days — routes rarely change


def build_jump_matrix(system_names, avoid=None):
    """Build a pairwise jump matrix for a set of system names.

    Resolves names to IDs, fetches all pairwise shortest routes with
    file-caching (30-day TTL) and parallel ESI calls.

    avoid: optional iterable of system IDs that routes must not pass
    through. A pair's own endpoints are dropped from the avoid list so
    distances to/from an avoided system itself stay computable.

    Returns: (system_ids: dict[str,int], matrix: dict[tuple[int,int], int])
    Matrix keyed both directions: matrix[(a,b)] == matrix[(b,a)].
    Unreachable pairs stored as -1.
    """
    import concurrent.futures

    avoid = set(avoid or ())

    # Resolve names to IDs
    system_ids = {}
    for name in system_names:
        sid = search_system_id(name)
        if sid:
            system_ids[name] = sid

    # Build pairs — N*(N-1)/2
    ids = list(system_ids.values())
    pairs = []
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            pairs.append((ids[i], ids[j]))

    matrix = {}
    for sid in ids:
        matrix[(sid, sid)] = 0

    def _fetch_pair(a, b):
        eff_avoid = sorted(avoid - {a, b})
        cache_key = f"route:{min(a, b)}:{max(a, b)}"
        if eff_avoid:
            cache_key += ":avoid:" + "-".join(str(i) for i in eff_avoid)
        cached = cache_get(cache_key, CACHE_TTL_ROUTE)
        if cached is not None:
            return a, b, cached
        url = f"{ESI_BASE}/route/{a}/{b}/?datasource=tranquility&flag=shortest"
        if eff_avoid:
            url += "&avoid=" + ",".join(str(i) for i in eff_avoid)
        data = esi_get(url)
        if data and isinstance(data, list):
            jumps = len(data) - 1
        else:
            jumps = -1  # unreachable
        cache_set(cache_key, jumps)
        return a, b, jumps

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(_fetch_pair, a, b) for a, b in pairs]
        for fut in concurrent.futures.as_completed(futures):
            a, b, jumps = fut.result()
            matrix[(a, b)] = jumps
            matrix[(b, a)] = jumps

    return system_ids, matrix


def get_system_positions(system_ids):
    """Fetch 2D galaxy-plane positions for systems.

    Uses ESI /universe/systems/{id}/ to get x,z coordinates.
    File-cached with 30-day TTL.

    Args:
        system_ids: dict[str, int] — system name -> ID
    Returns:
        dict[str, tuple[float, float]] — name -> (x, z)
    """
    positions = {}
    for name, sid in system_ids.items():
        cache_key = f"syspos:{sid}"
        cached = cache_get(cache_key, CACHE_TTL_ROUTE)
        if cached is not None:
            positions[name] = tuple(cached)
            continue
        data = esi_get(f"{ESI_BASE}/universe/systems/{sid}/?datasource=tranquility")
        if data and "position" in data:
            pos = data["position"]
            x, z = pos.get("x", 0), pos.get("z", 0)
            positions[name] = (x, z)
            cache_set(cache_key, [x, z])
        else:
            positions[name] = (0, 0)
    return positions


# ── Market ────────────────────────────────────────────────────

def fetch_buy_orders(region_id, type_id):
    """Fetch all buy orders for a type in a region (uncached)."""
    url = f"{ESI_BASE}/markets/{region_id}/orders/?datasource=tranquility&order_type=buy&type_id={type_id}"
    data = esi_get(url)
    return data if data is not None else []


def fetch_best_buy(region_id, type_id, use_cache=False):
    """Return (price, order_dict) for the highest buy order, or (0, None)."""
    if use_cache:
        url = f"{ESI_BASE}/markets/{region_id}/orders/?datasource=tranquility&order_type=buy&type_id={type_id}"
        orders = esi_get_cached(url, CACHE_TTL_MARKET) or []
    else:
        orders = fetch_buy_orders(region_id, type_id)
    buy_orders = [o for o in orders if o.get("is_buy_order", True)] if orders else []
    if not buy_orders:
        return 0, None
    best = max(buy_orders, key=lambda o: o["price"])
    return best["price"], best


def fetch_best_sell(region_id, type_id, use_cache=False):
    """Return (price, order_dict) for the lowest sell order, or (0, None)."""
    url = f"{ESI_BASE}/markets/{region_id}/orders/?datasource=tranquility&order_type=sell&type_id={type_id}"
    if use_cache:
        data = esi_get_cached(url, CACHE_TTL_MARKET) or []
    else:
        data = esi_get(url) or []
    sell_orders = [o for o in data if not o.get("is_buy_order", False)]
    if not sell_orders:
        return 0, None
    best = min(sell_orders, key=lambda o: o["price"])
    return best["price"], best


# ── Type and group info ───────────────────────────────────────

def get_type_info(type_id):
    """Fetch type information from ESI (cached 7 days)."""
    url = f"{ESI_BASE}/universe/types/{type_id}/?datasource=tranquility"
    return esi_get_cached(url, CACHE_TTL_STATIC)


def get_group_info(group_id):
    """Fetch group information from ESI (cached 7 days)."""
    url = f"{ESI_BASE}/universe/groups/{group_id}/?datasource=tranquility"
    return esi_get_cached(url, CACHE_TTL_STATIC)


def get_dogma_attribute(attribute_id):
    """Fetch dogma attribute metadata from ESI (cached 7 days)."""
    url = f"{ESI_BASE}/dogma/attributes/{attribute_id}/?datasource=tranquility"
    return esi_get_cached(url, CACHE_TTL_STATIC)


def get_type_traits(type_id):
    """Fetch ship traits from the EVE Ref SDE API (cached 7 days).

    Returns the traits dict with 'types' (skill bonuses keyed by skill type ID)
    and 'role_bonuses'. Returns empty dict on failure.
    """
    url = f"https://ref-data.everef.net/types/{type_id}"
    return (esi_get_cached(url, CACHE_TTL_STATIC) or {}).get("traits", {})


def search_type_ids(names):
    """Resolve a list of names to type IDs via ESI.

    Returns dict of {name: type_id}. Names not found are omitted.
    """
    if not names:
        return {}
    result = {}
    # ESI /universe/ids/ accepts up to 500 names per call
    for i in range(0, len(names), 500):
        batch = names[i:i + 500]
        data = esi_post(f"{ESI_BASE}/universe/ids/?datasource=tranquility", batch)
        if data:
            for item in data.get("inventory_types", []):
                result[item["name"]] = item["id"]
    return result
