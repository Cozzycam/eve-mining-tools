"""Glug game core — the company simulation.

Reads the poller's event tables (wallet_journal, mining_ledger, snapshots)
from glug.db, converts new activity into work orders, and advances a lazy
simulation: crew burn down work orders over elapsed time and earn scrip.
No tick daemon — state advances on every read (idle-game offline progress).

First run founds the company fresh: cursors start at "now", so historical
events don't dump a 282-order backlog on day one.

Usage:
    python game.py status        # ingest + advance + show the company
    python game.py hire          # hire a new crew member
    python game.py train <id>    # train crew member (level +1)
    python game.py selftest      # offline checks
"""

import json
import math
import os
import random
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import eve_common
import poller

# ── Tunables (all first-pass balance; expect retuning) ────────

FOUNDING_SCRIP = 500.0
FOUNDING_CREW = 2
BASE_RATE = 60.0          # work units/hour per crew at level 1
RATE_GROWTH = 1.2         # per level multiplier
HIRE_BASE = 100.0
HIRE_GROWTH = 1.15        # cost * growth^existing_crew
TRAIN_BASE = 75.0
TRAIN_GROWTH = 1.6        # cost * growth^(level-1)
BUSYWORK_RATE = 8.0       # scrip/hour per idle crew (drought rule)
ISK_PER_UNIT = 10000.0    # journal: 1 work unit per 10k ISK moved
ORDER_UNIT_CAP = 5000.0   # one event never exceeds this many units
COLONY_ORDER_UNITS = 50.0 # per PI colony cycle detected

SCRIP_PER_UNIT = {"ore": 1.0, "income": 0.8, "colony": 1.2}

DEPARTMENTS = ["Logistics", "Catering", "Safety & Compliance", "Marketing", "R&D"]

FIRST = ["Aile", "Bex", "Cato", "Dree", "Emek", "Fenn", "Gozi", "Hale", "Ilya",
         "Jax", "Kess", "Lomi", "Moro", "Nyra", "Osun", "Pell", "Quon", "Rask",
         "Suvi", "Tovak"]
LAST = ["Aldent", "Brack", "Corvyn", "Dallor", "Ekku", "Fauve", "Grinn",
        "Hazouf", "Ithri", "Jorto", "Kelmar", "Lusko", "Marn", "Nedge",
        "Ollo", "Pryce", "Quilla", "Renn", "Sarpa", "Tull"]

GAME_SCHEMA = """
CREATE TABLE IF NOT EXISTS game_state(
    key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS crew(
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL,
    department TEXT NOT NULL,
    hired_ts REAL NOT NULL,
    level    INTEGER NOT NULL DEFAULT 1,
    xp       REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS work_orders(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    label      TEXT NOT NULL,
    units      REAL NOT NULL,
    done       REAL NOT NULL DEFAULT 0,
    created_ts REAL NOT NULL,
    completed_ts REAL,
    source     TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS mining_cursor(
    date TEXT, type_id INTEGER, solar_system_id INTEGER, consumed INTEGER,
    PRIMARY KEY(date, type_id, solar_system_id)
);
CREATE TABLE IF NOT EXISTS scrip_ledger(
    ts REAL, amount REAL, reason TEXT
);
"""


def open_db(path=poller.DB_PATH):
    db = sqlite3.connect(path)
    db.executescript(poller.SCHEMA)   # poller tables may not exist yet
    db.executescript(GAME_SCHEMA)
    return db


# ── State helpers ─────────────────────────────────────────────

def get_state(db, key, default=None):
    row = db.execute("SELECT value FROM game_state WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_state(db, key, value):
    db.execute("INSERT INTO game_state(key, value) VALUES(?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value)))


def earn(db, amount, reason, now):
    if amount <= 0:
        return
    set_state(db, "scrip", get_state(db, "scrip", 0.0) + amount)
    db.execute("INSERT INTO scrip_ledger(ts, amount, reason) VALUES(?,?,?)",
               (now, amount, reason))


def spend(db, amount, reason, now):
    scrip = get_state(db, "scrip", 0.0)
    if amount > scrip:
        return False
    set_state(db, "scrip", scrip - amount)
    db.execute("INSERT INTO scrip_ledger(ts, amount, reason) VALUES(?,?,?)",
               (now, -amount, reason))
    return True


# ── Founding ──────────────────────────────────────────────────

def found_company(db, now, rng=random):
    """First run: fresh company, cursors at 'now' so history isn't backlog."""
    set_state(db, "founded_ts", now)
    set_state(db, "last_sim_ts", now)
    set_state(db, "scrip", FOUNDING_SCRIP)
    row = db.execute("SELECT MAX(id) FROM wallet_journal").fetchone()
    set_state(db, "journal_cursor", row[0] or 0)
    row = db.execute("SELECT MAX(id) FROM snapshots").fetchone()
    set_state(db, "snap_cursor", row[0] or 0)
    db.execute("INSERT OR REPLACE INTO mining_cursor "
               "SELECT date, type_id, solar_system_id, quantity FROM mining_ledger")
    for _ in range(FOUNDING_CREW):
        hire_crew(db, now, free=True, rng=rng)
    db.execute("INSERT INTO scrip_ledger(ts, amount, reason) VALUES(?,?,?)",
               (now, FOUNDING_SCRIP, "Glug Corporate Seed Grant"))
    db.commit()


# ── Ingestion: poller events → work orders ────────────────────

def _add_order(db, kind, label, units, source, now):
    if units <= 0:
        return
    db.execute("INSERT OR IGNORE INTO work_orders"
               "(kind, label, units, created_ts, source) VALUES(?,?,?,?,?)",
               (kind, label, min(units, ORDER_UNIT_CAP), now, source))


def _type_name_vol(type_id):
    info = eve_common.get_type_info(type_id) or {}
    return info.get("name", f"Type {type_id}"), info.get("volume", 1.0)


def ingest(db, now):
    """Turn new poller rows into work orders. Idempotent via cursors/sources."""
    # Wallet journal → paperwork orders
    cursor = get_state(db, "journal_cursor", 0)
    rows = db.execute(
        "SELECT id, ref_type, amount, description FROM wallet_journal "
        "WHERE id>? ORDER BY id", (cursor,)).fetchall()
    for jid, ref_type, amount, desc in rows:
        amount = amount or 0
        units = max(1.0, abs(amount) / ISK_PER_UNIT)
        verb = "Receivables" if amount >= 0 else "Payables"
        label = f"{verb}: {desc or ref_type or 'transaction'}"
        _add_order(db, "income", label[:80], units, f"journal:{jid}", now)
        cursor = jid
    set_state(db, "journal_cursor", cursor)

    # Mining ledger deltas → raw material intake orders
    rows = db.execute(
        "SELECT m.date, m.type_id, m.solar_system_id, m.quantity, "
        "COALESCE(c.consumed, 0) FROM mining_ledger m LEFT JOIN mining_cursor c "
        "ON m.date=c.date AND m.type_id=c.type_id "
        "AND m.solar_system_id=c.solar_system_id "
        "WHERE m.quantity > COALESCE(c.consumed, 0)").fetchall()
    for date, tid, sysid, qty, consumed in rows:
        name, vol = _type_name_vol(tid)
        m3 = (qty - consumed) * vol
        _add_order(db, "ore", f"Intake: {name} ({m3:,.0f} m3)", m3,
                   f"mining:{date}:{tid}:{sysid}:{qty}", now)
        db.execute("INSERT OR REPLACE INTO mining_cursor VALUES(?,?,?,?)",
                   (date, tid, sysid, qty))

    # PI colony snapshot changes → colony resupply orders
    snap_cursor = get_state(db, "snap_cursor", 0)
    rows = db.execute(
        "SELECT id, kind FROM snapshots WHERE id>? AND kind LIKE 'planet:%' "
        "ORDER BY id", (snap_cursor,)).fetchall()
    for sid, kind in rows:
        _add_order(db, "colony", f"Branch office cycle ({kind})",
                   COLONY_ORDER_UNITS, f"snap:{sid}", now)
    row = db.execute("SELECT MAX(id) FROM snapshots").fetchone()
    set_state(db, "snap_cursor", row[0] or snap_cursor)
    db.commit()


# ── Simulation ────────────────────────────────────────────────

def crew_rate(level):
    return BASE_RATE * RATE_GROWTH ** (level - 1)


def advance(db, now):
    """Lazy sim: drain work orders with crew capacity accrued since last call."""
    last = get_state(db, "last_sim_ts", now)
    hours = max(0.0, (now - last) / 3600.0)
    if hours == 0:
        set_state(db, "last_sim_ts", now)
        db.commit()
        return
    crew = db.execute("SELECT id, level FROM crew").fetchall()
    capacity = sum(crew_rate(lvl) for _, lvl in crew) * hours
    total_capacity = capacity

    orders = db.execute(
        "SELECT id, kind, units, done FROM work_orders "
        "WHERE completed_ts IS NULL ORDER BY created_ts, id").fetchall()
    for oid, kind, units, done in orders:
        if capacity <= 0:
            break
        chunk = min(capacity, units - done)
        capacity -= chunk
        done += chunk
        earn(db, chunk * SCRIP_PER_UNIT.get(kind, 1.0),
             f"work:{kind}", now)
        db.execute(
            "UPDATE work_orders SET done=?, completed_ts=? WHERE id=?",
            (done, now if done >= units else None, oid))

    # ponytail: busywork approximated as leftover-capacity fraction of the
    # window, not per-order timing; fine until orders get sub-hour granularity
    if crew and total_capacity > 0 and capacity > 0:
        idle_hours = hours * (capacity / total_capacity)
        earn(db, BUSYWORK_RATE * len(crew) * idle_hours,
             "busywork: internal Glug contracts", now)

    set_state(db, "last_sim_ts", now)
    db.commit()


def sim(db, now=None):
    """Ingest new events and advance the sim. Call before any read/action."""
    now = now if now is not None else time.time()
    if get_state(db, "founded_ts") is None:
        found_company(db, now)
    ingest(db, now)
    advance(db, now)
    return now


# ── Actions ───────────────────────────────────────────────────

def hire_cost(db):
    n = db.execute("SELECT COUNT(*) FROM crew").fetchone()[0]
    return HIRE_BASE * HIRE_GROWTH ** n


def hire_crew(db, now, free=False, rng=random):
    cost = 0 if free else hire_cost(db)
    if not free and not spend(db, cost, "hire", now):
        return None
    n = db.execute("SELECT COUNT(*) FROM crew").fetchone()[0]
    name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
    dept = DEPARTMENTS[n % len(DEPARTMENTS)]
    db.execute("INSERT INTO crew(name, department, hired_ts) VALUES(?,?,?)",
               (name, dept, now))
    db.commit()
    return name, dept, cost


def train_cost(level):
    return TRAIN_BASE * TRAIN_GROWTH ** (level - 1)


def train_crew(db, crew_id, now):
    row = db.execute("SELECT name, level FROM crew WHERE id=?", (crew_id,)).fetchone()
    if not row:
        return None
    name, level = row
    cost = train_cost(level)
    if not spend(db, cost, f"train:{name}", now):
        return None
    db.execute("UPDATE crew SET level=level+1 WHERE id=?", (crew_id,))
    db.commit()
    return name, level + 1, cost


# ── CLI ───────────────────────────────────────────────────────

def status():
    db = open_db()
    now = sim(db)
    scrip = get_state(db, "scrip", 0.0)
    founded = get_state(db, "founded_ts", now)
    days = (now - founded) / 86400
    print(f"═══ GLUG™ INTERNAL OPERATIONS TERMINAL ═══")
    print(f"Company age: {days:.1f} days   Scrip: {scrip:,.0f} GS")
    print(f"Next hire: {hire_cost(db):,.0f} GS\n")

    print("Crew roster:")
    for cid, name, dept, level in db.execute(
            "SELECT id, name, department, level FROM crew ORDER BY id"):
        print(f"  #{cid} {name:<16} {dept:<20} L{level} "
              f"({crew_rate(level):,.0f} u/hr, train: {train_cost(level):,.0f} GS)")

    open_orders = db.execute(
        "SELECT kind, label, units, done FROM work_orders "
        "WHERE completed_ts IS NULL ORDER BY created_ts LIMIT 10").fetchall()
    n_open = db.execute("SELECT COUNT(*) FROM work_orders "
                        "WHERE completed_ts IS NULL").fetchone()[0]
    n_done = db.execute("SELECT COUNT(*) FROM work_orders "
                        "WHERE completed_ts IS NOT NULL").fetchone()[0]
    print(f"\nWork orders: {n_open} open, {n_done} completed")
    for kind, label, units, done in open_orders:
        pct = 100 * done / units if units else 100
        print(f"  [{kind:<6}] {label:<60} {pct:5.1f}%")

    print("\nRecent scrip ledger:")
    for ts, amount, reason in db.execute(
            "SELECT ts, amount, reason FROM scrip_ledger "
            "ORDER BY ts DESC, rowid DESC LIMIT 8"):
        age = (now - ts) / 3600
        print(f"  {age:6.1f}h ago  {amount:>+10,.1f}  {reason}")


def selftest():
    db = sqlite3.connect(":memory:")
    db.executescript(poller.SCHEMA)
    db.executescript(GAME_SCHEMA)
    rng = random.Random(7)
    t0 = 1_000_000.0

    # Pre-existing history that must NOT become backlog
    db.execute("INSERT INTO wallet_journal(id, amount, description) "
               "VALUES(1, 99999999, 'old money')")
    db.execute("INSERT INTO mining_ledger VALUES('2026-08-01', 30375, 9, 1000)")

    set_state(db, "founded_ts", t0)  # found manually with fixed rng
    set_state(db, "last_sim_ts", t0)
    set_state(db, "scrip", FOUNDING_SCRIP)
    set_state(db, "journal_cursor", 1)
    set_state(db, "snap_cursor", 0)
    db.execute("INSERT OR REPLACE INTO mining_cursor "
               "SELECT date, type_id, solar_system_id, quantity FROM mining_ledger")
    hire_crew(db, t0, free=True, rng=rng)
    hire_crew(db, t0, free=True, rng=rng)

    ingest(db, t0)
    assert db.execute("SELECT COUNT(*) FROM work_orders").fetchone()[0] == 0, \
        "history became backlog"

    # New activity: 50k ISK journal entry + mining delta of 500 units
    db.execute("INSERT INTO wallet_journal(id, amount, description) "
               "VALUES(2, 50000, 'PI dump')")
    db.execute("UPDATE mining_ledger SET quantity=1500")
    real_tnv = _type_name_vol
    globals()["_type_name_vol"] = lambda tid: ("Testore", 2.0)  # 500 * 2 m3
    try:
        ingest(db, t0 + 10)
    finally:
        globals()["_type_name_vol"] = real_tnv
    orders = db.execute("SELECT kind, units FROM work_orders ORDER BY id").fetchall()
    assert orders == [("income", 5.0), ("ore", 1000.0)], f"orders wrong: {orders}"
    ingest(db, t0 + 20)   # idempotent
    assert db.execute("SELECT COUNT(*) FROM work_orders").fetchone()[0] == 2

    # 1 hour: 2 crew L1 = 120 units capacity → income order (5u) done,
    # ore order 115u done. Scrip: 5*0.8 + 115*1.0 = 119
    advance(db, t0 + 3600)
    scrip = get_state(db, "scrip")
    assert abs(scrip - (FOUNDING_SCRIP + 119.0)) < 0.01, f"scrip {scrip}"
    done = db.execute("SELECT COUNT(*) FROM work_orders "
                      "WHERE completed_ts IS NOT NULL").fetchone()[0]
    assert done == 1, "income order should be complete"

    # Drain the rest (needs 885 more units ≈ 7.375h), then 1h pure idle:
    # busywork = 8 * 2 crew * 1h = 16
    advance(db, t0 + 3600 * 10)
    pre_idle = get_state(db, "scrip")
    advance(db, t0 + 3600 * 11)
    assert abs(get_state(db, "scrip") - pre_idle - 16.0) < 0.01, "busywork wrong"

    # Hiring: cost 100 * 1.15^2 = 132.25, deducted
    pre = get_state(db, "scrip")
    _, _, cost = hire_crew(db, t0, rng=rng)
    assert abs(cost - 132.25) < 0.01 and \
        abs(get_state(db, "scrip") - (pre - cost)) < 0.01
    # Training L1: cost 75
    pre = get_state(db, "scrip")
    name, lvl, cost = train_crew(db, 1, t0)
    assert lvl == 2 and abs(cost - 75.0) < 0.01
    assert crew_rate(2) == BASE_RATE * RATE_GROWTH
    print("selftest OK")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        status()
    elif cmd == "hire":
        db = open_db()
        now = sim(db)
        result = hire_crew(db, now)
        if result:
            name, dept, cost = result
            print(f"Welcome aboard, {name} ({dept}) — {cost:,.0f} GS. "
                  f"Glug thanks you for growing the family.")
        else:
            print(f"Insufficient scrip ({hire_cost(db):,.0f} GS required).")
    elif cmd == "train" and len(sys.argv) > 2:
        db = open_db()
        now = sim(db)
        result = train_crew(db, int(sys.argv[2]), now)
        if result:
            name, lvl, cost = result
            print(f"{name} completed Glug Advancement Seminar L{lvl} "
                  f"({cost:,.0f} GS).")
        else:
            print("Training failed (bad id or insufficient scrip).")
    elif cmd == "selftest":
        selftest()
    else:
        sys.exit(f"Unknown command: {cmd}")
