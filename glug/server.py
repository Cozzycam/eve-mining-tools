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


# est. souls aboard by archetype/tier — lore-flavoured, not canon
COMPLEMENT = {
    "miner":   {1: 5, 2: 30, 3: 140, 4: 900, 5: 6000},
    "hauler":  {1: 6, 2: 40, 3: 180, 4: 1200, 5: 8000},
    "generic": {1: 4, 2: 35, 3: 200, 4: 1500, 5: 12000},
    "hq":      {1: 0, 2: 0, 3: 0, 4: 0, 5: 0},
}

ROLE_ROOMS = {
    "miner":   ["intake", "processing", "ore_hold", "crystal_bay"],
    "hauler":  ["cargo", "cargo", "crane_bay", "manifest"],
    "generic": ["magazine", "fire_control", "armory"],
}


def build_interior(ship_type_id, training):
    """Deterministic room layout in normalized hull coords (x,y,w,h in 0..1)."""
    import random as _r
    info = eve_common.get_type_info(ship_type_id) or {}
    group = (eve_common.get_group_info(info.get("group_id", 0)) or {})
    archetype = classify_hull(group.get("name"))
    tier = size_tier(info.get("mass"))
    rng = _r.Random(ship_type_id)

    if archetype == "hq":
        rooms = [
            {"kind": "lobby", "label": "Reception", "x": .02, "y": .55, "w": .2, "h": .3},
            {"kind": "lab", "label": "R&D — Beverages", "x": .24, "y": .55, "w": .24, "h": .3},
            {"kind": "magazine", "label": "R&D — Defense", "x": .5, "y": .55, "w": .24, "h": .3},
            {"kind": "vault", "label": "Scrip Vault", "x": .76, "y": .55, "w": .21, "h": .3},
            {"kind": "office", "label": "Marketing", "x": .02, "y": .18, "w": .3, "h": .3},
            {"kind": "mess", "label": "Executive Canteen", "x": .34, "y": .18, "w": .3, "h": .3},
            {"kind": "bridge", "label": "CEO Suite", "x": .66, "y": .18, "w": .31, "h": .3},
        ]
        return {"archetype": "hq", "tier": 0, "complement": 0,
                "ship_class": "Corporate Headquarters", "mass": None, "rooms": rooms}

    # Two decks, bow (right) to stern (left). Bridge fore, engineering aft.
    role_pool = ROLE_ROOMS[archetype]
    n_role = min(2 + tier, 6)
    role_rooms = [role_pool[i % len(role_pool)] for i in range(n_role)]
    upper = ["bridge"] + (["training_pod"] if training else []) + ["mess", "bunks"]
    lower = role_rooms + ["engineering"]

    def lay(names, y, h):
        out, x = [], 0.03
        widths = [1.0 + 0.6 * rng.random() for _ in names]
        scale = 0.94 / sum(widths)
        for name, w in zip(names, widths):
            out.append({"kind": name, "label": ROOM_LABELS.get(name, name.title()),
                        "x": round(x, 3), "y": y, "w": round(w * scale - 0.012, 3),
                        "h": h})
            x += w * scale
        return out

    rooms = lay(list(reversed(lower)), .52, .34) + lay(list(reversed(upper)), .14, .32)
    return {"archetype": archetype, "tier": tier,
            "complement": COMPLEMENT[archetype][tier],
            "ship_class": group.get("name", "?"), "mass": info.get("mass"),
            "rooms": rooms}


ROOM_LABELS = {
    "bridge": "Bridge", "mess": "Glug Canteen", "bunks": "Crew Quarters",
    "engineering": "Engineering", "training_pod": "Executive Pod",
    "intake": "Raw Intake", "processing": "Processing Line",
    "ore_hold": "Ore Hold", "crystal_bay": "Crystal Bay",
    "cargo": "Cargo Bay", "crane_bay": "Crane Bay", "manifest": "Manifest Office",
    "magazine": "Magazine", "fire_control": "Fire Control", "armory": "Armory",
}


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
        result = game.hire_crew(db, now)
        msg = (f"Welcome aboard, {result[0]} ({result[1]})!" if result
               else "Insufficient scrip.")
    elif action == "train":
        result = game.train_crew(db, int(payload.get("id", 0)), now)
        msg = (f"{result[0]} advanced to L{result[1]}." if result
               else "Training failed.")
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
        if self.path == "/":
            try:
                with open(PAGE_PATH, "rb") as f:
                    page = f.read()
            except OSError:
                page = b"terminal.html missing"
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
