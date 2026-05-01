# ore_scanner.py — Current State Audit

_Generated 2026-04-29. Read-only recon — no changes proposed._

---

## 1. Architecture Overview

### Entry point and call flow

`ore_scanner.py` is a **browser-based web app**, not a CLI tool. It runs a stdlib `http.server.HTTPServer` on `localhost:8747`.

```
main()
  → kill_existing()          # kill any prior instance holding the port
  → HTTPServer(ScanHandler)  # bind :8747
  → webbrowser.open()        # auto-open browser after 0.5s

GET /            → serves inline HTML_PAGE (single-page app, ~990 lines of embedded HTML/CSS/JS)
GET /api/scan    → _handle_scan()
                    → parse query params (region, ship/hold, from-system, yield, cls, comp, repro)
                    → scan()              # core: iterate ores, fetch_buy_orders per ore, evaluate
                       → fetch_buy_orders()        per ore (via eve_common)
                       → fetch_material_prices()   if repro enabled (moon goo buy orders)
                       → evaluate_order_range()    if travel-aware (sell-local + jump calc)
                       → calc_isk_hr()             if yield provided
                    → enrich_results()    # resolve system/station names, fill jump counts
                    → serialize JSON response
```

### External dependencies

**Stdlib only.** Imports: `http.server`, `json`, `sys`, `threading`, `time`, `urllib.request`, `urllib.error`, `webbrowser`, `urllib.parse`, `hashlib`, `os`, `socket`, `subprocess` (for `kill_existing`). No `requirements.txt`, no pip packages.

### File structure

| File | Purpose | LoC |
|------|---------|-----|
| `ore_scanner.py` | Web server + scan logic + inline HTML/JS UI | 1,832 |
| `eve_common.py` | Shared ESI helpers, caching, market, routing | 271 |
| `fit_dossier.py` | Ship fitting dossier generator (separate tool, CLI) | ~1,570 |
| `make_icon.py` | Generates `ore_scanner.ico` | 132 |
| `skills.ini` | Character skill profile (Cozzynk) | 105 |
| `HANDOVER.md` | Changelog for fit_dossier v1.2–v1.6 | ~287 |
| `EVE Ore Scanner.bat/.vbs/.lnk` | Windows launcher files | — |

**Approximate total LoC (Python):** ~3,800

---

## 2. Data Sources Currently Used

### ESI endpoints called (all via `eve_common.py`)

| Endpoint | Used by | Cache |
|----------|---------|-------|
| `GET /markets/{region}/orders/?order_type=buy&type_id={id}` | ore_scanner (per ore + per compressed variant + per repro material) | None (live every scan) |
| `POST /universe/ids/` | system name → ID resolution (`from` param) | In-memory `_system_id_cache` |
| `GET /route/{origin}/{dest}/?flag=shortest` | Jump count for travel-aware mode | In-memory `_route_cache` |
| `GET /universe/systems/{id}/` | System name resolution | In-memory `_name_cache` |
| `GET /universe/stations/{id}/` | Station name resolution (NPC stations only, 60M–64M range) | In-memory `_name_cache` |
| `GET /universe/types/{id}/` | fit_dossier only (type info) | File cache, 7d TTL |
| `GET /universe/groups/{id}/` | fit_dossier only | File cache, 7d TTL |
| `GET /dogma/attributes/{id}/` | fit_dossier only | File cache, 7d TTL |
| `GET ref-data.everef.net/types/{id}` | fit_dossier only (SDE traits) | File cache, 7d TTL |

**ore_scanner.py itself calls only:** market orders, universe/ids (POST), route, systems, stations. It does **not** call type info, dogma, or everef endpoints.

### Regions queried

6 hardcoded regions in `eve_common.py:30-37`:

| Key | Region | ID |
|-----|--------|----|
| `verge` | Verge Vendor | 10000068 |
| `dodixie` | Sinq Laison (Dodixie) | 10000032 |
| `jita` | The Forge (Jita) | 10000002 |
| `amarr` | Domain (Amarr) | 10000043 |
| `hek` | Metropolis (Hek) | 10000042 |
| `rens` | Heimatar (Rens) | 10000030 |

Region selection is a dropdown in the browser UI. The API accepts `?region=<key>`. Default is `verge`.

### Caching layer

- **File-backed cache** at `~/.eve-tools-cache/` — SHA256-hashed filenames, JSON with timestamp. Used by `eve_common.esi_get_cached()`. TTLs: 7 days (static), 5 min (market). **ore_scanner does NOT use the file cache** — it calls `fetch_buy_orders()` which uses raw uncached `esi_get()`.
- **In-memory caches** in `eve_common.py`: `_name_cache` (system/station names), `_route_cache` (jump counts), `_system_id_cache` (name→ID). Not TTL'd, persist for server lifetime.
- **Client-side (localStorage)**: settings persistence (`oreScanner` key) + price history for sparklines (`orePriceHistory` key, max 288 points per ore ≈ 24h at 5-min intervals).

### Hardcoded reference tables

| Table | Location | Size | Contents |
|-------|----------|------|----------|
| `BELT_ORES` | ore_scanner.py:32-144 | 108 entries | type_id, name, vol, group, cls for every belt ore variant |
| `MOON_ORES` | ore_scanner.py:151-217 | 60 entries | type_id, name, vol, group, cls, tier for every moon ore variant |
| `COMP_IDS` | ore_scanner.py:220-277 | 158 mappings | raw_type_id → compressed_type_id |
| `REPRO_FORMULAS` | ore_scanner.py:284-310 | 20 entries | base moon ore → list of (material_id, quantity) |
| `REPRO_VARIANTS` | ore_scanner.py:312-334 | 60 entries | every moon ore variant → (base_id, yield_multiplier) |
| `REPRO_MATERIAL_IDS` | ore_scanner.py:337-340 | ~20 IDs | set of unique reprocessing output material type IDs |
| `SHIPS` | ore_scanner.py:349-354 | 4 entries | ship_name → ore_hold_m3 (Venture/Procurer/Retriever/Covetor) |
| `REGION_LIST` | eve_common.py:30-37 | 6 entries | region key, ID, display name |

---

## 3. CLI Flags and Inputs

ore_scanner.py has **no CLI flags**. It is invoked as `python ore_scanner.py` and starts the web server. All configuration is done through the browser UI.

### Browser UI controls

| Control | Type | Default | Notes |
|---------|------|---------|-------|
| Region | dropdown | Verge Vendor | 6 options |
| Ship | dropdown | Venture (5,000 m³) | Venture/Procurer/Retriever/Covetor/Custom |
| Custom hold size | number input | 5000 | Shown only when Ship = Custom |
| Ore class | dropdown | Highsec (Class I) | All / belt subsets / moon tiers |
| Your system | text input | empty | Optional; enables travel-aware mode |
| Solo yield (m³/min) | number input | empty | Enables ISK/hr calculation |
| Fleet boost checkbox | checkbox | off | Reveals Duration bonus % and Repro yield % |
| Duration bonus % | number input | empty | Adjusts effective yield |
| Sales tax % | number input | 7.5 | After-tax column |
| Compress in hold | checkbox | on | Multiplies hold by 100 for effective capacity |
| Show all variants | checkbox | off | Disables best-per-family collapsing |
| Repro yield % | number input | empty | Enables reprocessed value column (moon ores) |
| Compare compressed | checkbox | off | Adds compressed ISK/m³ column |
| Scan button | button | — | Triggers scan |
| Auto-refresh | checkbox + interval | off, 5 min | 3/5/10 min intervals |

No interactive prompts. Settings are persisted in `localStorage`.

### API query parameters (GET /api/scan)

`region`, `ship` or `hold`, `from` (system name), `yield` (m³/min), `cls` (ore class), `compress` (flag), `all` (flag), `comp` (flag), `repro` (percent).

---

## 4. Pricing Paths Supported

### Raw ore → buy orders
**Yes.** Primary pricing path. Fetches highest buy order per ore type in the selected region. This is the default ISK/m³ ranking.

### Compressed ore → buy orders
**Yes.** When "Compare compressed" is checked. Fetches buy orders for the compressed variant (via `COMP_IDS` mapping). Shows `comp_isk_m3` column. Compressed ISK/m³ = `comp_best_buy / (100 × ore_vol)`. Green COMP badge when compressed sells higher than raw.

### Reprocess → outputs
**Yes, for moon ores only.** When Repro yield % is set (> 0). Uses `REPRO_FORMULAS` to compute ISK from selling reprocessed materials (moon goo + minerals) at buy order prices. Belt ores have no reprocessing formulas in the code — they get `repro_isk_m3 = 0`. Shows Repro ISK/m³ column and Repro ISK/hr when yield is also set.

### No other pricing paths. No sell order prices (always buy = instant sell). No cross-region arbitrage.

---

## 5. Ore Coverage

### Belt ores
**Yes, all variants.** 108 entries covering:
- 8 highsec families (Veldspar, Scordite, Pyroxeres, Plagioclase, Omber, Kernite, Mordunium, Ytirium) — 32 variants
- 11 lowsec families (Dark Ochre, Gneiss, Griemeer, Hedbergite, Hemorphite, Hezorime, Jaspet, Kylixium, Nocxite, Rakovene, Talassonite) — 42 variants
- 9 nullsec families (Arkonor, Bezdnacine, Bistot, Crokite, Ducinium, Eifyrium, Mercoxit, Spodumain, Ueganite) — 34 variants

Variant naming uses **II-Grade / III-Grade / IV-Grade** (not +5%/+10%). Some families have only 3 variants (no IV-Grade): Rakovene, Talassonite, Bezdnacine, Mercoxit.

### Moon ores
**Yes, all tiers, all variants.** 60 entries covering:
- R4 (Ubiquitous): 4 families × 3 variants = 12 (base / Brimful / Glistening)
- R8 (Common): 4 families × 3 = 12 (base / Copious / Twinkling)
- R16 (Uncommon): 4 families × 3 = 12 (base / Lavish / Shimmering)
- R32 (Rare): 4 families × 3 = 12 (base / Replete / Glowing)
- R64 (Exceptional): 4 families × 3 = 12 (base / Bountiful / Shining)

Reprocessing formulas exist for all 20 base moon ores. Variant multipliers: base=1.0, improved=1.15, excellent=2.0.

### Ice
**Not covered.** No ice types in `BELT_ORES` or `MOON_ORES`.

### Gas
**Not covered.** No gas types anywhere.

---

## 6. Output Formatting

### Default table columns

Always shown: `#`, `Ore`, `Buy/unit`, `ISK/m³`, `Full Hold`, `Sell at`, `Orders`, `Demand`.

Conditionally shown:
- `Comp ISK/m³` — when "Compare compressed" is on
- `Repro ISK/m³` — when repro yield % is set and moon ores have data
- `After tax` — when sales tax > 0
- `ISK/hr` or `Repro ISK/hr` — when yield is provided
- `Jumps` — when any result has jump data
- `Sell Local?` — when travel-aware mode is active
- `Trend` (sparkline) — when price history exists in localStorage

### Sorting / ranking logic

Primary sort depends on mode:
1. If repro + yield → sort by `repro_isk_hr` descending
2. If repro only → sort by `repro_isk_m3` descending
3. If travel-aware + yield → sort by `isk_hr` descending
4. Default → sort by `isk_m3` descending

All columns are clickable for re-sorting (ascending/descending toggle).

### "Best per family" collapsing

**Yes, implemented.** When "Show all variants" is unchecked (default), `scan()` collapses to one entry per group (ore family), keeping the variant with the highest value of whichever sort key is active. When checked, all variants are shown.

### Other output modes

**None.** No JSON export, no CSV, no CLI output. The only output is the browser table + summary card. The `/api/scan` endpoint returns JSON, but this is an internal API consumed by the embedded JS, not a documented external API.

---

## 7. Travel / Route Awareness

**Implemented.** This is not based on any `FEATURE-TRAVEL-ISK-HOUR.md` spec file (that file does not exist in the repo). The feature is built directly into the scanner.

### How it works

- **Input:** User types a system name in "Your system" field + provides "Solo yield (m³/min)".
- **System resolution:** Name → system_id via `POST /universe/ids/`.
- **Jump calculation:** `GET /route/{origin}/{dest}/?flag=shortest` for each candidate sell system. Results cached in-memory.
- **Order range evaluation:** `evaluate_order_range()` checks if the buy order's range (station/system/region/N jumps) covers the player's system. If yes → `sell_local=True, jumps=0`. If no → computes actual jump distance.
- **ISK/hr formula:** `(isk_hold / cycle_minutes) × 60` where `cycle_minutes = mine_time + travel_time`. Mine time = `hold_size / yield_m3_min`. Travel time = `jumps × 2 × 66s + 60s` (round trip + dock/sell/undock). Constants: `SECS_PER_JUMP=66`, `SECS_DOCK_UNDOCK=60`.
- **Candidate selection:** For travel-aware mode, evaluates top 5 buy orders by price, picks the one with highest ISK/hr (or ISK/m³ if no yield given).
- **Output:** Jumps column with color-coded badges (0=green, 1-5=blue, 6-10=orange, 11+=red). Sell Local column (checkmark + range label or X). ISK/hr column. Summary card shows distance + sell local status.

### Fleet boost interaction

Duration bonus % adjusts effective yield: `solo / (1 - boostPct/100)`. This feeds into ISK/hr calculation.

---

## 8. Procurer Ore Hold Investigation

**Not resolved.** The `SHIPS` dict on line 351 hardcodes `"procurer": 16000` with a TODO comment acknowledging the discrepancy:

```python
"procurer":  16000,  # TODO: base 12k + Mining Barge skill bonus; hardcoded at level IV for now
```

The value 16,000 represents base 12,000 × (1 + 0.05 × Mining Barge IV × some factor), but no actual skill-based calculation exists. All ship hold sizes in the `SHIPS` dict are static numbers. The "Compress in hold" option multiplies the hold by 100, so with that on, Procurer effectively shows 1,600,000 m³.

Note: `fit_dossier.py` does compute skill-adjusted ore hold dynamically from ESI dogma attributes, but ore_scanner.py does not use any of that logic.

---

## 9. Spec Docs in the Repo

Only one markdown file exists in the project:

| File | Summary | Status |
|------|---------|--------|
| `HANDOVER.md` | Changelog for fit_dossier.py covering v1.2 through v1.6 fixes (CPU/PG skill bonuses, align time skills, drawback columns, Porpoise/ICS support, SDE traits switch) | **Implemented** — documents changes already shipped in fit_dossier.py |

### Status of specifically requested docs

- **HANDOVER.md** — Exists, fully implemented (documents post-facto what was built)
- **CLAUDE-CODE-MOON-ORE-SPEC.md** — **Does not exist** in the repo
- **FEATURE-TRAVEL-ISK-HOUR.md** — **Does not exist** in the repo (the feature itself is implemented in ore_scanner.py, but there's no spec doc)

---

## 10. Known Issues / TODOs

### TODO/FIXME/XXX comments

```
ore_scanner.py:351:    "procurer":  16000,  # TODO: base 12k + Mining Barge skill bonus; hardcoded at level IV for now
```

That is the only TODO/FIXME/XXX in the entire codebase.

### Observed rough edges

- **Ship hold sizes are all hardcoded** — no skill-adjusted computation. Procurer is wrong for any Mining Barge level other than IV. Retriever/Covetor/Venture may also be off depending on skills.
- **No Porpoise in ship dropdown** — fit_dossier supports Porpoise, but ore_scanner's `SHIPS` dict only has Venture/Procurer/Retriever/Covetor. Users must use "Custom" and type 50000.
- **Belt ore reprocessing not supported** — `REPRO_FORMULAS` only covers moon ores. Repro column shows 0 for all belt ores.
- **Market order fetching is uncached and serial** — each ore type triggers a separate HTTP request with 150ms sleep. A full "all ores" scan (168 types) takes ~34 seconds minimum. With compressed comparison enabled, it doubles.
- **Station name resolution only works for NPC stations** — player-owned structures (location_id > 64M) always show `None`. Citadel names require authenticated ESI.
- **No pagination on market orders** — `fetch_buy_orders()` calls the ESI endpoint once. If a type has >1000 orders in a region, results are truncated (ESI pages at 1000). Unlikely for most ores but possible for Veldspar in The Forge.
- **`oreCounts` in JS is hardcoded** — line 1185 has `{'0':168,'belt':108,'1':32,...}`. If ores are added/removed, this must be updated manually.
- **Sell order prices never fetched** — the scanner only looks at buy orders (instant sell). The "Buy/unit" label is clear, but some users might expect sell order context.
- **Inline HTML blob** — `HTML_PAGE` is a ~990-line raw string containing all HTML, CSS, and JS. Not minified, not in a separate file. Works but hard to maintain.
