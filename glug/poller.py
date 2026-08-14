"""Glug poller — records Cozzynk's real EVE activity into SQLite.

Stdlib only. Append-only event store; the game layer (game.py) derives
work orders and events from these tables. Safe to restart anytime.

Tables:
    snapshots      — change-detected JSON snapshots per feed kind
                     (ship, location, online, wallet, skillqueue, skills,
                      planets, planet:<id>)
    wallet_journal — immutable journal entries, deduped by ESI id
    mining_ledger  — per-day per-ore quantities, upserted (grows during the day)

Usage:
    python poller.py            # run forever (the VPS service mode)
    python poller.py once       # single polling pass, then exit
    python poller.py dump       # show row counts + latest snapshots
    python poller.py selftest   # offline checks
"""

import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sso

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "glug.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots(
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    ts   REAL NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snap_kind ON snapshots(kind, id);
CREATE TABLE IF NOT EXISTS wallet_journal(
    id          INTEGER PRIMARY KEY,
    ts          TEXT,
    ref_type    TEXT,
    amount      REAL,
    balance     REAL,
    description TEXT,
    raw         TEXT
);
CREATE TABLE IF NOT EXISTS mining_ledger(
    date            TEXT,
    type_id         INTEGER,
    solar_system_id INTEGER,
    quantity        INTEGER,
    PRIMARY KEY(date, type_id, solar_system_id)
);
"""


def open_db(path=DB_PATH):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


# ── Recorders ─────────────────────────────────────────────────

def snapshot_if_changed(db, kind, data):
    """Insert a snapshot row only if data differs from the latest of its kind.

    Returns True if a new row was written.
    """
    if data is None:
        return False
    blob = json.dumps(data, sort_keys=True)
    row = db.execute(
        "SELECT data FROM snapshots WHERE kind=? ORDER BY id DESC LIMIT 1",
        (kind,)).fetchone()
    if row and row[0] == blob:
        return False
    db.execute("INSERT INTO snapshots(kind, ts, data) VALUES(?,?,?)",
               (kind, time.time(), blob))
    db.commit()
    return True


def record_journal(db, entries):
    """Insert journal entries, deduped by ESI id. Returns count inserted."""
    new = 0
    for e in entries or []:
        cur = db.execute(
            "INSERT OR IGNORE INTO wallet_journal(id, ts, ref_type, amount, "
            "balance, description, raw) VALUES(?,?,?,?,?,?,?)",
            (e["id"], e.get("date"), e.get("ref_type"), e.get("amount"),
             e.get("balance"), e.get("description"), json.dumps(e, sort_keys=True)))
        new += cur.rowcount
    db.commit()
    return new


def record_mining(db, entries):
    """Upsert mining ledger rows (quantities grow during the day)."""
    for e in entries or []:
        db.execute(
            "INSERT INTO mining_ledger(date, type_id, solar_system_id, quantity) "
            "VALUES(?,?,?,?) ON CONFLICT(date, type_id, solar_system_id) "
            "DO UPDATE SET quantity=excluded.quantity",
            (e["date"], e["type_id"], e["solar_system_id"], e["quantity"]))
    db.commit()


def _paged(path, max_pages=10):
    """Fetch a paginated authenticated ESI endpoint until an empty page."""
    out = []
    for page in range(1, max_pages + 1):
        data = sso.esi_auth_get(f"{path}?page={page}")
        if not data:
            break
        out.extend(data)
        if len(data) < 1000:  # short page = last page (journal pages are 2500+)
            break
    return out


# ── Feeds ─────────────────────────────────────────────────────

def make_feeds(cid):
    """Return the polling schedule: (name, interval_seconds, poll_fn)."""
    def snap(kind, path):
        def fn(db):
            return snapshot_if_changed(db, kind, sso.esi_auth_get(path))
        return fn

    def journal(db):
        n = record_journal(db, _paged(f"/characters/{cid}/wallet/journal/"))
        return n > 0

    def mining(db):
        record_mining(db, _paged(f"/characters/{cid}/mining/"))
        return False  # upserts aren't "events"; don't log as change

    def planets(db):
        plist = sso.esi_auth_get(f"/characters/{cid}/planets/")
        changed = snapshot_if_changed(db, "planets", plist)
        for p in plist or []:
            pid = p["planet_id"]
            detail = sso.esi_auth_get(f"/characters/{cid}/planets/{pid}/")
            changed |= snapshot_if_changed(db, f"planet:{pid}", detail)
        return changed

    return [
        ["location",   30,   snap("location", f"/characters/{cid}/location/")],
        ["ship",       30,   snap("ship", f"/characters/{cid}/ship/")],
        ["online",     60,   snap("online", f"/characters/{cid}/online/")],
        ["wallet",     300,  snap("wallet", f"/characters/{cid}/wallet/")],
        ["skillqueue", 600,  snap("skillqueue", f"/characters/{cid}/skillqueue/")],
        ["skills",     3600, snap("skills", f"/characters/{cid}/skills/")],
        ["journal",    1800, journal],
        ["mining",     900,  mining],
        ["planets",    1800, planets],
    ]


def poll_pass(db, feeds, now=None, due_only=True):
    """Run every due feed once. Mutates feeds' next-due times."""
    now = now if now is not None else time.time()
    for feed in feeds:
        name, interval, fn = feed[0], feed[1], feed[2]
        next_due = feed[3] if len(feed) > 3 else 0
        if due_only and now < next_due:
            continue
        try:
            changed = fn(db)
            if changed:
                print(f"[{time.strftime('%H:%M:%S')}] {name}: change recorded")
        except Exception as e:  # keep the poller alive no matter what
            print(f"[{time.strftime('%H:%M:%S')}] {name}: ERROR {e}", file=sys.stderr)
        if len(feed) > 3:
            feed[3] = now + interval
        else:
            feed.append(now + interval)


def run():
    cid = sso.character_id()
    if not cid:
        sys.exit("Not logged in — run: python sso.py login <client_id> [secret]")
    db = open_db()
    feeds = make_feeds(cid)
    print(f"Glug poller online for character {cid}. DB: {DB_PATH}")
    while True:
        poll_pass(db, feeds)
        time.sleep(5)


# ── CLI ───────────────────────────────────────────────────────

def dump():
    db = open_db()
    for table in ("snapshots", "wallet_journal", "mining_ledger"):
        n = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"{table}: {n} rows")
    print("\nLatest snapshots:")
    rows = db.execute(
        "SELECT kind, MAX(ts), data FROM snapshots GROUP BY kind ORDER BY kind"
    ).fetchall()
    for kind, ts, data in rows:
        age = int(time.time() - ts)
        preview = data if len(data) <= 100 else data[:100] + "…"
        print(f"  {kind} ({age}s ago): {preview}")
    row = db.execute(
        "SELECT ts, amount, description FROM wallet_journal "
        "ORDER BY ts DESC LIMIT 5").fetchall()
    if row:
        print("\nRecent journal:")
        for ts, amount, desc in row:
            print(f"  {ts}  {amount:>15,.0f}  {desc}")


def selftest():
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA)
    assert snapshot_if_changed(db, "t", {"a": 1}) is True
    assert snapshot_if_changed(db, "t", {"a": 1}) is False, "dup snapshot recorded"
    assert snapshot_if_changed(db, "t", {"a": 2}) is True
    assert snapshot_if_changed(db, "t", None) is False, "None recorded"
    assert db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 2

    entries = [{"id": 1, "date": "d", "ref_type": "r", "amount": 5.0,
                "balance": 10.0, "description": "x"}]
    assert record_journal(db, entries) == 1
    assert record_journal(db, entries) == 0, "journal dup inserted"

    record_mining(db, [{"date": "d1", "type_id": 1, "solar_system_id": 9,
                        "quantity": 100}])
    record_mining(db, [{"date": "d1", "type_id": 1, "solar_system_id": 9,
                        "quantity": 250}])
    q = db.execute("SELECT quantity FROM mining_ledger").fetchall()
    assert q == [(250,)], f"mining upsert failed: {q}"

    calls = []
    feeds = [["a", 60, lambda db: calls.append("a") or False],
             ["b", 60, lambda db: calls.append("b") or False]]
    poll_pass(db, feeds, now=1000)
    poll_pass(db, feeds, now=1030)   # nothing due
    poll_pass(db, feeds, now=1061)   # both due again
    assert calls == ["a", "b", "a", "b"], f"scheduling wrong: {calls}"
    print("selftest OK")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        run()
    elif cmd == "once":
        cid = sso.character_id() or sys.exit("Not logged in.")
        db = open_db()
        poll_pass(db, make_feeds(cid), due_only=False)
        print("Pass complete.")
        dump()
    elif cmd == "dump":
        dump()
    elif cmd == "selftest":
        selftest()
    else:
        sys.exit(f"Unknown command: {cmd}")
