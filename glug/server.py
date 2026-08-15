"""Glug web terminal — the diegetic company ERP in a browser.

Stdlib http.server serving one inline page + JSON API over glug.db.
Every /api/state call runs game.sim() first (lazy idle-game advance).

Usage: python server.py [port]     (default 8750, binds 0.0.0.0)
"""

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import eve_common
import game
import sso

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8750
PAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "terminal.html")


# ── Ship interiors (procedural, from real hull data) ──────────

def classify_hull(group_name):
    """Map an ESI group name to an interior archetype."""
    g = (group_name or "").lower()
    if "capsule" in g or "shuttle" in g:
        return "hq"
    if any(k in g for k in ("mining", "exhumer", "expedition", "industrial command")):
        return "miner"
    if any(k in g for k in ("industrial", "transport", "freighter", "hauler")):
        return "hauler"
    return "generic"


def size_tier(mass):
    """1=frigate .. 5=capital, from real hull mass."""
    for tier, ceiling in ((1, 2e6), (2, 9e6), (3, 3e7), (4, 3e8)):
        if (mass or 0) < ceiling:
            return tier
    return 5


# Souls aboard — grounded in the lore Crew Statistics table (capsuleer-
# minimum column): pod-flown hulls run skeleton crews. By mass tier:
# t1≈frigate, t2≈destroyer/cruiser, t3≈battlecruiser, t4≈battleship/
# freighter, t5≈capital. Archetype scales it (haulers are mostly cargo).
CREW_BY_TIER = {1: 3, 2: 25, 3: 90, 4: 250, 5: 1500}
EVAC_BY_TIER = {1: 30, 2: 400, 3: 600, 4: 7000, 5: 25000}  # max capacity, lore
CREW_ARCH_MULT = {"miner": 1.0, "hauler": 0.75, "generic": 1.2, "hq": 0}
SHIP_CREW = {
    12731: 70,   # Bustard — Campbell-calibrated vs the lore table
}


def complement(type_id, archetype, tier):
    if type_id in SHIP_CREW:
        return SHIP_CREW[type_id]
    return round(CREW_BY_TIER[tier] * CREW_ARCH_MULT[archetype])

ROLE_ROOMS = {
    "miner":   ["intake", "processing", "ore_hold", "crystal_bay"],
    "hauler":  ["cargo", "cargo", "crane_bay", "manifest"],
    "generic": ["magazine", "fire_control", "armory"],
}

# Approx hull lengths in metres by group keyword, first match wins.
# ponytail: community-derived approximations, deliberately a calibration
# table — tune freely, canon lengths are not in the SDE.
GROUP_LENGTHS = [
    ("capsule", 4), ("shuttle", 30), ("corvette", 45),
    ("expedition frigate", 105), ("mining frigate", 95), ("frigate", 75),
    ("destroyer", 160), ("battlecruiser", 500), ("battleship", 800),
    ("cruiser", 300), ("deep space transport", 500),
    ("blockade runner", 250), ("industrial command", 1100),
    ("capital industrial", 3600), ("ore freighter", 1700),
    ("freighter", 1100), ("industrial", 420),
    ("exhumer", 260), ("mining barge", 240),
]
TIER_LENGTHS = {1: 80, 2: 250, 3: 500, 4: 1100, 5: 3600}

DECK_PITCH = 6.0   # metres per deck (industrial hulls have tall holds)

# Hand-traced hull profiles from Campbell's in-game side shots.
# columns: (x_frac stern→bow, top_frac, bot_frac) — 0 = top of hull envelope.
# A deck exists at x where its band lies inside [top, bot].
SHIP_PROFILES = {
    12731: {   # Bustard — re-traced from Campbell's hi-res 775 m side shot (2026-08-15)
        "length_m": 775, "height_m": 220,
        "columns": [
            (0.00, 0.16, 0.72),                      # stern armour block
            (0.10, 0.16, 0.75), (0.105, 0.06, 0.80), # command tower steps up
            (0.22, 0.06, 0.82),                      # tower + undercarriage
            (0.24, 0.10, 0.95),                      # hex transition, keel begins
            (0.34, 0.10, 1.00),                      # hanging keel pod (deepest)
            (0.41, 0.01, 0.86),                      # cargo box top, spine below
            (0.71, 0.01, 0.86),
            (0.74, 0.01, 0.67),                      # box only, spine ends
            (0.97, 0.01, 0.67),
            (1.00, 0.05, 0.65),                      # bow bevel
        ],
        "bridge_at": "stern",
        # Campbell's zone annotation (2026-08-15): red=cargo, green=admin,
        # purple=functional/engineering. Stern block + keel are engineering.
        "zones": [
            (0.00, 0.13, 0.0, 1.0, "engineering"),   # engines / stern block
            (0.13, 0.32, 0.0, 1.0, "admin"),         # command tower (green)
            (0.32, 1.00, 0.0, 0.67, "cargo"),        # the big box (red)
            (0.32, 0.70, 0.67, 1.0, "engineering"),  # keel machinery (purple)
        ],
    },
    33697: {   # Prospect — traced from Campbell's annotated side shot (2026-08-15)
        "length_m": 105, "height_m": 28,
        "columns": [
            (0.00, 0.15, 0.75),                     # stern lip / exhaust
            (0.05, 0.00, 0.95), (0.38, 0.00, 0.95), # bulky main hull
            (0.42, 0.10, 0.80),                     # taper to neck
            (0.45, 0.15, 0.55), (0.55, 0.30, 0.75), # slim cockpit neck
            (0.60, 0.35, 0.95), (0.95, 0.35, 0.95), # harvester boom modules
            (1.00, 0.45, 0.80),                     # tail taper
        ],
        "bridge_at": "stern",
        "zones": [
            (0.00, 0.05, 0.0, 1.0, "engineering"),  # engines
            (0.05, 0.42, 0.0, 1.0, "cargo"),        # main hull = ore hold (red)
            (0.42, 0.55, 0.0, 1.0, "admin"),        # cockpit neck (green)
            (0.55, 1.00, 0.0, 1.0, "engineering"),  # harvester boom (purple)
        ],
    },
}

ZONE_POOLS = {
    "cargo": {"hauler": ["cargo", "cargo", "crane_bay"],
              "miner": ["ore_hold", "cargo", "crane_bay"],
              "generic": ["cargo", "magazine"]},
    "admin": ["manifest", "mess", "bunks", "office"],
}
ZONE_SIZES = {"cargo": (60, 110), "admin": (18, 45), "engineering": (40, 70),
              None: (35, 65)}


def zone_at(zones, xf, yf):
    for (x0, x1, t0, b0, cat) in zones or []:
        if x0 <= xf <= x1 and t0 <= yf <= b0:
            return cat
    return None


def hull_length(type_id, group_name, tier):
    if type_id in SHIP_PROFILES:
        return SHIP_PROFILES[type_id]["length_m"]
    g = (group_name or "").lower()
    for key, metres in GROUP_LENGTHS:
        if key in g:
            return metres
    return TIER_LENGTHS[tier]


def fallback_profile(length, archetype):
    """Generic boxy hull for un-traced ships: bow taper, stern shoulder."""
    return {
        "length_m": length,
        "height_m": max(12.0, min(400.0, length * 0.24)),
        "columns": [
            (0.00, 0.10, 0.90), (0.04, 0.00, 1.00),
            (0.90, 0.00, 1.00), (1.00, 0.18, 0.82),
        ],
        "bridge_at": "bow",
        "zones": [
            (0.00, 0.12, 0.0, 1.0, "engineering"),
            (0.85, 1.00, 0.0, 1.0, "admin"),
        ],
    }


def _interp_cols(cols, x):
    for i in range(len(cols) - 1):
        (x0, t0, b0), (x1, t1, b1) = cols[i], cols[i + 1]
        if x0 <= x <= x1:
            f = 0 if x1 == x0 else (x - x0) / (x1 - x0)
            return t0 + (t1 - t0) * f, b0 + (b1 - b0) * f
    return cols[-1][1], cols[-1][2]


def deck_spans(cols, tf, bf, length, samples=160):
    """x-ranges (metres) where the deck band [tf, bf] fits inside the hull."""
    spans, start = [], None
    for i in range(samples + 1):
        x = i / samples
        top, bot = _interp_cols(cols, x)
        ok = top <= tf + 1e-6 and bot >= bf - 1e-6
        if ok and start is None:
            start = x
        if (not ok or i == samples) and start is not None:
            end = x if not ok else 1.0
            if (end - start) * length >= 10:   # ignore slivers under 10 m
                spans.append((round(start * length, 1), round(end * length, 1)))
            start = None
    return spans


def build_interior(ship_type_id, training):
    """Deterministic REAL-SCALE layout: rooms in metres along the hull.

    deck 0 = upper, deck 1 = lower. Bow at x=length, stern at x=0.
    The client renders at a fixed px-per-metre so a 1.8 m crew sprite is
    the scale yardstick and big hulls are genuinely, scrollably big.
    """
    import random as _r
    info = eve_common.get_type_info(ship_type_id) or {}
    group = (eve_common.get_group_info(info.get("group_id", 0)) or {})
    archetype = classify_hull(group.get("name"))
    tier = size_tier(info.get("mass"))
    rng = _r.Random(ship_type_id)

    if archetype == "hq":
        rooms = [
            {"kind": "office", "label": "Marketing", "deck": 0, "x": 1, "w": 18},
            {"kind": "mess", "label": "Executive Canteen", "deck": 0, "x": 20, "w": 18},
            {"kind": "bridge", "label": "CEO Suite", "deck": 0, "x": 39, "w": 20},
            {"kind": "lobby", "label": "Reception", "deck": 1, "x": 1, "w": 13},
            {"kind": "lab", "label": "R&D — Beverages", "deck": 1, "x": 15, "w": 15},
            {"kind": "magazine", "label": "R&D — Defense", "deck": 1, "x": 31, "w": 15},
            {"kind": "vault", "label": "Scrip Vault", "deck": 1, "x": 47, "w": 12},
            ]
        return {"archetype": "hq", "tier": 0, "complement": 0, "length_m": 60,
                "height_m": 12, "n_decks": 2, "deck_pitch": DECK_PITCH,
                "decks": [{"spans": [(0, 60)]}, {"spans": [(0, 60)]}],
                "columns": [(0, 0, 1), (1, 0, 1)],
                "ship_class": "Corporate Headquarters", "mass": None,
                "type_id": None, "rooms": rooms}

    L = hull_length(ship_type_id, group.get("name"), tier)
    prof = SHIP_PROFILES.get(ship_type_id) or fallback_profile(L, archetype)
    n_decks = max(2, round(prof["height_m"] / DECK_PITCH))
    role_pool = ROLE_ROOMS[archetype]
    support = ["mess", "bunks"]
    rooms, decks = [], []
    bridge_done, pod_done = False, not training

    for d in range(n_decks):
        tf, bf = d / n_decks, (d + 1) / n_decks
        spans = deck_spans(prof["columns"], tf, bf, L)
        decks.append({"spans": spans})
        if not spans:
            continue
        frac = d / max(1, n_decks - 1)
        rngd = _r.Random(ship_type_id * 1000 + d)
        yf = (d + 0.5) / n_decks
        for (x0, x1) in spans:
            x, i = x0 + 2, rngd.randrange(4)
            while x < x1 - 12:
                cat = zone_at(prof.get("zones"), x / L, yf)
                lo, hi = ZONE_SIZES.get(cat, ZONE_SIZES[None])
                w = min(rngd.uniform(lo, hi), x1 - x - 2)
                if not bridge_done and cat == "admin":
                    kind, bridge_done = "bridge", True
                elif bridge_done and not pod_done and cat == "admin":
                    kind, pod_done = "training_pod", True
                    w = min(w, 30)
                elif cat == "cargo":
                    pool = ZONE_POOLS["cargo"][archetype]
                    kind = pool[i % len(pool)]
                elif cat == "admin":
                    kind = ZONE_POOLS["admin"][i % 4]
                elif cat == "engineering":
                    kind = "engineering"
                elif frac > 0.82:
                    kind = "engineering"
                else:
                    kind = role_pool[i % len(role_pool)] if i % 3 else support[i % 2]
                rooms.append({"kind": kind, "cat": cat or "",
                              "label": ROOM_LABELS.get(kind, kind.title()),
                              "deck": d, "x": round(x, 1), "w": round(w, 1)})
                x += w + 2.5
                i += 1

    return {"archetype": archetype, "tier": tier,
            "complement": complement(ship_type_id, archetype, tier),
            "evac_rating": EVAC_BY_TIER[tier], "length_m": L,
            "height_m": prof["height_m"], "n_decks": n_decks,
            "deck_pitch": DECK_PITCH, "decks": decks,
            "columns": prof["columns"],
            "ship_class": group.get("name", "?"), "mass": info.get("mass"),
            "type_id": ship_type_id, "rooms": rooms}


ROOM_LABELS = {
    "bridge": "Bridge", "mess": "Glug Canteen", "bunks": "Crew Quarters",
    "engineering": "Engineering", "training_pod": "Executive Pod",
    "intake": "Raw Intake", "processing": "Processing Line",
    "ore_hold": "Ore Hold", "crystal_bay": "Crystal Bay",
    "cargo": "Cargo Cell", "crane_bay": "Crane Bay", "manifest": "Manifest Office",
    "office": "Offices",
    "magazine": "Magazine", "fire_control": "Fire Control", "armory": "Armory",
}


def franchise_list(db):
    """Franchises with resolved names and 2D galaxy positions (all cached)."""
    names = game.get_state(db, "loc_names", {})
    changed = False
    out = []
    for lid, sysid, opened in db.execute(
            "SELECT location_id, solar_system_id, opened_ts FROM franchises"):
        key = str(lid)
        if key not in names:
            n = eve_common.resolve_station_name(lid)
            if not n and lid > 10**12:  # player structure → authenticated lookup
                d = sso.esi_auth_get(f"/universe/structures/{lid}/")
                n = d.get("name") if d else None
            names[key] = n or f"Station {lid}"
            changed = True
        sysname = eve_common.resolve_system_name(sysid) if sysid else "?"
        pos = eve_common.get_system_positions({sysname: sysid}).get(sysname) \
            if sysid else None
        out.append({"name": names[key], "system": sysname, "pos": pos,
                    "opened": opened})
    if changed:
        game.set_state(db, "loc_names", names)
        db.commit()
    return out


def _latest(db, kind):
    row = db.execute("SELECT data FROM snapshots WHERE kind=? "
                     "ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
    return json.loads(row[0]) if row else None


def build_state():
    db = game.open_db()
    now = game.sim(db)

    ship = _latest(db, "ship") or {}
    loc = _latest(db, "location") or {}
    online = _latest(db, "online") or {}
    queue = _latest(db, "skillqueue") or []
    wallet = _latest(db, "wallet")

    ship_type = (eve_common.get_type_info(ship.get("ship_type_id", 0)) or {})
    training = None
    for entry in queue:
        if entry.get("finish_date"):
            skill = eve_common.get_type_info(entry["skill_id"]) or {}
            training = {"skill": skill.get("name", "?"),
                        "level": entry["finished_level"],
                        "finish": entry["finish_date"]}
            break

    crew = [{"id": c[0], "name": c[1], "dept": c[2], "level": c[3],
             "rate": game.crew_rate(c[3]), "train_cost": game.train_cost(c[3])}
            for c in db.execute(
                "SELECT id, name, department, level FROM crew ORDER BY id")]

    orders = [{"kind": o[0], "label": o[1], "pct": 100 * o[3] / o[2] if o[2] else 100}
              for o in db.execute(
                  "SELECT kind, label, units, done FROM work_orders "
                  "WHERE completed_ts IS NULL ORDER BY created_ts LIMIT 20")]
    n_open, n_done = (db.execute(
        "SELECT SUM(completed_ts IS NULL), SUM(completed_ts IS NOT NULL) "
        "FROM work_orders").fetchone() or (0, 0))

    ledger = [{"age_h": (now - r[0]) / 3600, "amount": r[1], "reason": r[2]}
              for r in db.execute(
                  "SELECT ts, amount, reason FROM scrip_ledger "
                  "ORDER BY ts DESC, rowid DESC LIMIT 15")]

    interior = build_interior(ship.get("ship_type_id", 0), training)
    state = {
        "interior": interior,
        "products": game.product_state(db),
        "franchises": franchise_list(db),
        "brand_equity": game.get_state(db, "brand_equity", 0),
        "earn_mult": game.earn_mult(db),
        "franchise_rate": game.FRANCHISE_RATE,
        "counters": game.get_state(db, "counters", {}),
        "scrip": game.get_state(db, "scrip", 0.0),
        "age_days": (now - game.get_state(db, "founded_ts", now)) / 86400,
        "hire_cost": game.hire_cost(db),
        "crew": crew,
        "orders": orders, "n_open": n_open or 0, "n_done": n_done or 0,
        "ledger": ledger,
        "ceo": {
            "ship_name": ship.get("ship_name"),
            "ship_type": ship_type.get("name"),
            "system": eve_common.resolve_system_name(loc["solar_system_id"])
                      if loc.get("solar_system_id") else None,
            "docked": bool(loc.get("station_id") or loc.get("structure_id")),
            "online": online.get("online", False),
            "wallet": wallet,
            "training": training,
        },
    }
    db.close()
    return state


def do_action(payload):
    db = game.open_db()
    now = game.sim(db)
    action = payload.get("action")
    if action == "hire":
        result = game.hire_crew(db, now, dept=payload.get("dept"))
        msg = (f"Contractor promoted: {result[0]} ({result[1]}). "
               f"Glug Premium Employment Membership™ granted." if result
               else "Insufficient scrip for a membership.")
    elif action == "train":
        result = game.train_crew(db, int(payload.get("id", 0)), now)
        msg = (f"{result[0]} advanced to L{result[1]}." if result
               else "Training failed.")
    elif action == "research":
        result = game.set_research(db, payload.get("key")) or None
        msg = ("R&D reassigned. The lab hums with purpose."
               if result else "That product line is unavailable.")
    elif action == "launch":
        result = game.launch_product(db, payload.get("key"), now)
        msg = (f"🎉 {result['name']} LAUNCHED. +{result['be']} Brand Equity. "
               f"Marketing is weeping with joy." if result
               else "Launch conditions not met.")
    else:
        result, msg = None, "Unknown action."
    db.close()
    return {"ok": result is not None, "msg": msg}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        pages = {"/": PAGE_PATH,
                 "/spike": os.path.join(os.path.dirname(PAGE_PATH), "artspike.html")}
        if self.path in pages:
            try:
                with open(pages[self.path], "rb") as f:
                    page = f.read()
            except OSError:
                page = b"page missing"
            self._send(200, page, "text/html; charset=utf-8")
        elif self.path == "/api/state":
            self._send(200, build_state())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/api/action":
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            payload = {}
        self._send(200, do_action(payload))

    def log_message(self, *args):
        pass




def main():
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Glug terminal on http://0.0.0.0:{PORT}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
