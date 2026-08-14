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

    state = {
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
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
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


PAGE = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GLUG&trade; Internal Operations Terminal</title>
<style>
:root { --bg:#101216; --panel:#181b21; --edge:#262b33; --ink:#c7cdd6;
        --dim:#6b7482; --glug:#ffd23e; --ok:#7bd88f; --warn:#e5484d; }
* { box-sizing:border-box; margin:0; }
body { background:var(--bg); color:var(--ink); font:14px/1.45 "Segoe UI",system-ui,sans-serif; padding:16px; }
header { display:flex; align-items:center; gap:12px; margin-bottom:14px; }
.logo { width:40px; height:40px; border-radius:50%; background:var(--glug);
        display:flex; align-items:center; justify-content:center;
        font-size:24px; color:#111; }
h1 { font-size:17px; letter-spacing:.06em; }
h1 small { display:block; font-size:11px; color:var(--dim); font-weight:400; letter-spacing:.12em; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:12px; }
.panel { background:var(--panel); border:1px solid var(--edge); border-radius:8px; padding:12px 14px; }
.panel h2 { font-size:11px; color:var(--dim); letter-spacing:.14em; text-transform:uppercase; margin-bottom:9px; }
.big { font-size:26px; font-weight:600; color:var(--glug); }
.kv { display:flex; justify-content:space-between; padding:2px 0; }
.kv span:first-child { color:var(--dim); }
table { width:100%; border-collapse:collapse; }
td { padding:4px 6px 4px 0; vertical-align:middle; }
.dim { color:var(--dim); } .pos { color:var(--ok); } .neg { color:var(--warn); }
.bar { background:var(--edge); border-radius:4px; height:8px; overflow:hidden; min-width:70px; }
.bar i { display:block; height:100%; background:var(--glug); }
button { background:var(--glug); color:#111; border:0; border-radius:5px;
         padding:5px 12px; font-weight:600; cursor:pointer; }
button.mini { padding:2px 8px; font-size:12px; }
button:disabled { opacity:.4; cursor:default; }
#toast { position:fixed; bottom:18px; right:18px; background:var(--glug); color:#111;
         padding:10px 16px; border-radius:6px; font-weight:600; display:none; }
footer { margin-top:14px; color:var(--dim); font-size:11px; letter-spacing:.05em; }
.tag { font-size:10px; padding:1px 6px; border-radius:3px; background:var(--edge); color:var(--dim);
       text-transform:uppercase; letter-spacing:.08em; }
</style></head><body>
<header>
  <div class="logo">&#9786;</div>
  <h1>GLUG&trade; INTERNAL OPERATIONS TERMINAL
    <small>NOURISHING WORKERS. DEFENDING MARGINS.</small></h1>
</header>
<div class="grid">
  <div class="panel"><h2>Company</h2>
    <div class="big" id="scrip">…</div>
    <div class="kv"><span>Company age</span><b id="age">…</b></div>
    <div class="kv"><span>Work orders open / filled</span><b id="orders-n">…</b></div>
    <div style="margin-top:9px"><button id="hire">Hire crew</button>
      <span class="dim" id="hire-cost"></span></div>
  </div>
  <div class="panel"><h2>Executive Status</h2>
    <div class="kv"><span>CEO</span><b id="ceo-online">…</b></div>
    <div class="kv"><span>Vessel</span><b id="ceo-ship">…</b></div>
    <div class="kv"><span>Location</span><b id="ceo-sys">…</b></div>
    <div class="kv"><span>Corporate treasury</span><b id="ceo-wallet">…</b></div>
    <div class="kv"><span>Executive development</span><b id="ceo-train">…</b></div>
  </div>
  <div class="panel"><h2>Crew Roster</h2><table id="crew"></table></div>
  <div class="panel"><h2>Work Order Queue</h2><table id="queue"></table></div>
  <div class="panel"><h2>Scrip Ledger</h2><table id="ledger"></table></div>
</div>
<footer>GLUG&trade; is a family company. This terminal is monitored for your convenience.</footer>
<div id="toast"></div>
<script>
const $ = id => document.getElementById(id);
const fmt = (n,d=0) => n==null ? "—" : n.toLocaleString(undefined,{maximumFractionDigits:d});
function toast(msg){ const t=$("toast"); t.textContent=msg; t.style.display="block";
  clearTimeout(t._h); t._h=setTimeout(()=>t.style.display="none",3500); }
async function act(body){ const r=await fetch("/api/action",{method:"POST",body:JSON.stringify(body)});
  const j=await r.json(); toast(j.msg); refresh(); }
$("hire").onclick = () => act({action:"hire"});
async function refresh(){
  const s = await (await fetch("/api/state")).json();
  $("scrip").textContent = fmt(s.scrip) + " GS";
  $("age").textContent = s.age_days.toFixed(1) + " days";
  $("orders-n").textContent = s.n_open + " / " + s.n_done;
  $("hire-cost").textContent = " " + fmt(s.hire_cost) + " GS";
  $("hire").disabled = s.scrip < s.hire_cost;
  const c = s.ceo;
  $("ceo-online").textContent = c.online ? "ON DECK" : "off premises";
  $("ceo-online").style.color = c.online ? "var(--ok)" : "var(--dim)";
  $("ceo-ship").textContent = c.ship_name ? `${c.ship_name} (${c.ship_type||"?"})` : "—";
  $("ceo-sys").textContent = (c.system||"—") + (c.docked ? " · docked" : c.system ? " · in space" : "");
  $("ceo-wallet").textContent = fmt(c.wallet) + " ISK";
  $("ceo-train").textContent = c.training
    ? `${c.training.skill} L${c.training.level} — done ${new Date(c.training.finish).toLocaleString()}`
    : "—";
  $("crew").innerHTML = s.crew.map(m =>
    `<tr><td><b>${m.name}</b><br><span class="tag">${m.dept}</span></td>
     <td>L${m.level}</td><td class="dim">${fmt(m.rate)} u/hr</td>
     <td><button class="mini" ${s.scrip<m.train_cost?"disabled":""}
       onclick='act({action:"train",id:${m.id}})'>Train ${fmt(m.train_cost)}</button></td></tr>`).join("");
  $("queue").innerHTML = s.orders.length ? s.orders.map(o =>
    `<tr><td><span class="tag">${o.kind}</span></td><td>${o.label}</td>
     <td style="width:90px"><div class="bar"><i style="width:${o.pct.toFixed(1)}%"></i></div></td>
     <td class="dim">${o.pct.toFixed(0)}%</td></tr>`).join("")
    : `<tr><td class="dim">Queue clear. Crew engaged in enriching internal Glug contracts.</td></tr>`;
  $("ledger").innerHTML = s.ledger.map(l =>
    `<tr><td class="dim">${l.age_h<1 ? Math.round(l.age_h*60)+"m" : l.age_h.toFixed(1)+"h"}</td>
     <td class="${l.amount>=0?"pos":"neg"}">${l.amount>=0?"+":""}${fmt(l.amount,1)}</td>
     <td>${l.reason}</td></tr>`).join("");
}
refresh(); setInterval(refresh, 10000);
</script></body></html>
"""


def main():
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Glug terminal on http://0.0.0.0:{PORT}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
