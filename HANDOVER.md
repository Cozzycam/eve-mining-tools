# EVE Mining Tools — Handover Notes

## Ore Scanner — 2026-06-15 — Reprocess per-product breakdown ("show both")

Player reprocesses raw ore as their main path (compression is the rare
fleet-dependent case), so the repro path got a per-product breakdown.
ISK/hr still uses the realistic one-hub basket; the detail card now also
exposes where each output sells best and the split-sell upside.

- `MATERIAL_NAMES` static map (28 IDs: minerals 34-40 + 11399, moon goo
  16633-16653) — names confirmed via ESI /universe/names. R64: 16650
  Dysprosium, 16651 Neodymium, 16652 Promethium, 16653 Thulium.
- `calc_best_repro_region` now records `in_range_prices` (every hub that
  passed the jump/range gate) + `chosen_rkey`, then builds
  `repro_products` [{name, qty (per full hold), chosen_val (@ best basket
  hub), best_val, best_hub, same}] + `repro_split_isk_hold` (Σ best_val).
  Deterministic ores only (random/erratic skipped).
- Threaded through scan entry + JSON (`_round_products` rounds qty→int,
  vals→2dp). New keys: `repro_products`, `repro_split_isk_hold`.
- Detail card (`summaryCardHtml`): "Reprocess output — full hold" table
  (product · qty · value @ one-trip hub · best hub, ✓ when same / ↗ when
  it sells better elsewhere), plus "One trip total" vs "Split-sell max
  (N hubs) +X%". Verified: Glistening Bitumens one-trip @ Jita 9.24M,
  split-sell 9.92M (+7.4%; Pyerite→Cistuvaert, Mexallon→Dodixie).
- **"Compress in hold" now defaults OFF** (compression is the exception).
- Note: per-product best hubs respect Max sell jumps (only in-range hubs
  considered for both basket and split).

## Ore Scanner — 2026-06-15 — Repro jumps fix + clickable per-ore detail

Follow-ups to the max-sell-jumps work, from a live screenshot showing
every R4 ore as "REPRO @ Cistuvaert, 0 jumps" while the player was in
Jufvitte (4j from Cistuvaert):

- **Reprocess no longer claims 0 travel for the local region.** Mineral
  value is the region-*best* (hub) buy, so selling means hauling to that
  hub — `calc_best_repro_region` now costs `jumps→hub` for every region
  including the local one (removed the `rkey==local_region_key → 0`
  special case). Now repro hubs vary per ore and respect max_jumps
  honestly (verified: Glistening Bitumens reprocesses best at Dodixie 8j,
  Glistening Zeolites at Cistuvaert 4j).
- **Detail card is now click-driven.** Was hardcoded to `rows[0]`. Table
  rows are clickable (`selectOre(type_id)`, toggles); `selectedTypeId`
  pins the card to any ore so you can inspect Brimful Bitumens etc.
  Selected row gets a `.sel-row` outline.
- **New "Where to sell" breakdown** in the card (`summaryCardHtml`):
  one line per available path (raw/comp/repro/buyback) with sell
  location, one-way jumps, and ISK/hr (or ISK/m³), best flagged.
- **Demand line fixed.** It reflects *raw-ore* buy orders; on ores with
  no raw market (moon ore → reprocess) it showed "? units / 0 buy
  orders". Now relabelled "Raw ore demand" and, when 0 orders, shows
  "no raw buy orders — reprocess/buyback only".

## Ore Scanner — 2026-06-15 — Max sell jumps + drop Solo yield box

**Solo yield box removed** from the top row — yield is now per ship
(stored on the ship profile). `getEffectiveYield()` reads
`currentShip().yield` as the unboosted base; fleet boost still multiplies
on top. Dropped `yieldRate` from settings save/load and the auto-fill
hooks (no `yield-rate` element remains).

**Max sell jumps** (`#max-jumps`, one-way to hub; needs Your system):
caps how far a sell destination may be, and within that budget the
scanner weighs hauling to Dodixie/Jita/etc against selling local by
ISK/hr. Implementation:

- `scan(max_jumps=)` computes `reachable_hubs` once (region_key → one-way
  jumps) for hubs within budget; empty when `max_jumps` unset → no gating
  (prior behaviour preserved).
- **Raw path widened**: when `max_jumps` set + not compressing, after the
  local-region eval it checks each in-budget hub's best buy for the raw
  ore and keeps the best ISK/hr-after-haul. (Raw stays local-only when
  `max_jumps` is blank — avoids 5×/ore calls by default.)
- **Compressed loop gated**: skips out-of-budget hubs *before* the API
  call (`rkey not in reachable_hubs`); local region always kept.
- **Reprocess gated**: `calc_best_repro_region(max_jumps=)` skips
  non-local regions whose hub is beyond budget.
- **`_eval_best_order(max_jumps=)`**: drops individual orders >max_jumps
  one-way (sell-local orders are 0j, always kept).
- Handler parses `maxjumps`; response carries `max_jumps` +
  `reachable_hubs` [{hub, jumps}]; status line shows "Hubs ≤Nj one-way:
  …" or "No hubs within Nj (selling in-region only)".
- Jumps shown are one-way; ISK/hr still costs the round trip (2×).
- Verified live (Jufvitte): mj=8 → Cistuvaert(4)+Dodixie(8), Jita(13)
  excluded, top raw ore sells at Dodixie; mj=3 → in-region only; mj=20
  +compress → top picks Jita(13).

## Ore Scanner — 2026-06-15 — Saved ships + per-ship jump time

Ship dropdown was hardcoded presets (hold size only) plus a "Custom…"
hold field; yield and travel time were global. Numbers didn't reflect
the player's skills/fit. Replaced with localStorage-backed **ship
profiles**, each carrying its own stats:

- `oreShips` in localStorage: `[{name, hold (m³), yield (m³/min,
  unboosted), jumpSecs (avg time between jumps)}]`. Seeds Venture/
  Procurer/Retriever/Covetor on first load (Venture 45s/jump, rest 66s).
- Dropdown is populated from saved ships; "Manage ships" toggles an
  inline editor (`#ship-editor`) to add/edit/delete. Selecting a ship
  auto-fills the Solo yield field (if the ship has a yield) so fleet
  boost still multiplies on top. `currentShip()` resolves the selection.
- **Per-ship jump time** now drives ISK/hr. `calc_isk_hr(...,
  secs_per_jump=SECS_PER_JUMP)` replaces the global 66s constant;
  threaded through `_eval_best_order`, `calc_best_repro_region`, and
  `scan(secs_per_jump=)`. Handler parses `jumpsecs` query param; frontend
  sends `hold` + `jumpsecs` from the selected ship (no more `ship`/
  `custom` branch). Default 66s preserves prior numbers exactly.
- Backend `SHIPS` dict kept only as a fallback for the legacy `ship=`
  query param; the JS `SHIPS` injection is now unused but harmless.

## Web server — 2026-06-12 — PI generate: single-flight + heartbeat

User's VPS generate timed out (~5 min browser idle limit) and retries
stacked concurrent multi-GB allocator runs (a timed-out browser does NOT
stop the server thread) until the 4GB box hit 97% kernel-time memory
thrash. `/api/pi/generate` now:

- **Single-flight**: identical-param requests join the in-flight run
  (`_PI_RUNS` keyed by sorted overrides); different params queue behind
  `_PI_COMPUTE_LOCK` so at most one allocator is resident at a time.
- **Heartbeat**: response streams a 1-space byte every 10s while the
  worker computes — browser idle timeout never fires; `resp.json()`
  ignores leading whitespace. (HTTP/1.0 close-delimited body, no
  Content-Length.)
- systemd drop-in on VPS: `Environment=PYTHONUNBUFFERED=1`
  (`/etc/systemd/system/eve-mining-tools.service.d/override.conf`) so
  progress prints reach journalctl live.
- Measured clean VPS run: HTTP 200 in 2m47s, ~1.1GB peak RSS; local
  ~86s. Verified two concurrent local requests share one run.

## PI Dossier (v3.3) — 2026-06-12 — Route honours collect-before-drop order

The TSP picked the shortest tour with only the sell hub pinned last, so a
factory system could land *before* the extractor systems feeding it — the
route told you to visit the factory empty-handed (reported on the live
Guidance Systems layout: Home → Intaki(factory) → Ekuenbiron → Vaere →
sell). Daily circuit is collect P1 → drop at factory + pick up output →
sell, so order matters.

- `_layout_systems_and_stops` now also returns precedence pairs
  `(extractor_system, factory_system)` per chain (from `is_factory` on
  `planets_used` entries; co-located extractor+factory adds no pair).
- `_solve_tsp(..., precedence=)` filters permutations violating any pair.
  Pairs involving home or the pinned sell hub are skipped (home is first,
  sell is last — trivially fine). Contradictory pairs (two chains with
  factories in each other's extractor systems) fall back to the
  unconstrained shortest tour rather than the -1 estimate path.
- `_cached_route` cache key now includes the precedence set (same system
  set + sell hub can need different orders for different chain mixes).
- Self-test: precedence ordering, equal-length tie flip, cycle fallback,
  `_layout_systems_and_stops` pair extraction — all pass; verified on
  live data (factory system now routed after its extractor systems).

## PI Dossier (v3.2) — 2026-06-11 — Switchable per-layout map routes

`_generate_system_map_svg` draws every recommended layout's route in its
own SVG layer (`g.pi-route-layer[data-layout=N]`, two groups per layout:
lines below nodes, highlight rings + sell diamond above; layer 0 visible).
Nodes render neutral; route membership comes from the active overlay.
Web UI: `showPiRoute(i)` toggles layers via Route buttons under the map
or by clicking a layout heading. Existing section-hover node highlighting
unchanged.

## PI Dossier (v3.1) — 2026-06-11 — Self-calibrating density model

`build_density_estimator(extraction_rates, density_data)`: every observed
rate whose planet also has a density scan becomes a calibration point
(density %, observed P0/hr).

- 1–3 points: static band table scaled by the median observed/predicted
  ratio (clamped 0.3–3.0×) — a couple of observations firm up ALL estimates.
- ≥4 points spanning a ≥2× density spread: log-log power fit
  (rate = a·density^b, b clamped 0.2–1.5) blends with the scaled table,
  linearly reaching full weight at 8 points.
- OBS entries still override estimates on their own planet regardless;
  DFL (unscanned) planets unaffected.
- Surfaced in: console log, markdown header (**Rate model:** line), JSON
  `calibration` field, web PI tab status line under character info.
- Constants: CALIBRATION_MIN_POINTS_FIT=4, CALIBRATION_FULL_WEIGHT_POINTS=8,
  CALIBRATION_SCALE_CLAMP=(0.3, 3.0).
- Self-test: static fallback, single-point scaling, 6-point linear-trend
  recovery (b≈1), zero-density, obs-without-density excluded.

Caveat: observations bake in current Planetology skills + head count
(model assumes 10 heads) — re-observe after training those skills.

## PI Dossier (v3.0) — 2026-06-11 — Combo-local allocator rework

Planet selection moved inside the allocator. Previously each chain picked its
planets once, globally (highest rate anywhere, distance-blind), and
`allocate_system_first` could only accept/reject whole chains. Now:

- **Analysis context** (`_build_analysis_ctx`): per-instance rate table
  `{(system, ptype, letter): {p0: (rate, OBS|EST|DFL)}}` from
  planet_density/planet_extraction, plus ranked candidate lists per P0.
  OBS > EST > DFL semantics preserved per instance (old `get_p0_rate` removed).
- **Combo-local selection** (`_resolve_chain_selection(chain, ctx, combo_set,
  exclude)`): for each 1..4-system subset, chains re-resolve planets using only
  that subset's planets. Memoized on (chain, selection) via `ctx["vc_memo"]`.
- **Self-contained P2** now evaluates per planet *letter* — both P0 rates come
  from the same physical planet's scan (no more cross-instance rate mixing).
- **Factory planets** get a real (system, type, letter): cheapest-POCO spare
  planet, extractor systems preferred (co-located = no extra route cost).
  Counts as "Any" in the knapsack so allocation stays flexible.
- **Instance deconfliction** (`_finalize_layout`): top-10 candidate layouts are
  re-resolved chain-by-chain (best first) with claimed planets excluded — no
  two colonies on one physical planet; yields recomputed with the planet each
  chain actually gets; then re-ranked.
- **Pruning**: only "interesting" systems enumerate (top-8 per P0 + 5
  cheapest-tax + home, `TOP_SYSTEMS_PER_P0`/`LOW_TAX_FACTORY_SYSTEMS`); route
  lower-bound prune; knapsack + TSP result caching. 24k subsets in ~20s local.
- **ISK per haul-minute** added to layout routes (markdown + web).
- `compute_economics` split into `_build_econ_ctx` + `_compute_economics_single`
  (idempotent via `_econ_done` guard) so layout candidates price incrementally.
- Self-test extended: instance rates, combo restriction, exclusion fallback,
  SC instance integrity, factory placement, alloc helpers. All passing.

Known kept behaviors: P2 prefers self-contained over factory when one exists in
the combo (1 planet vs 3 — better per-slot); P4 chains pass through with
planet-count estimates only; legacy `allocate_5_planets` remains unused.

## PI Dossier (v1.0) — 2026-05-22

### New module: `pi_dossier.py`

Planetary Industry production chain analyser. Ranks all PI products (P1/P2/P3)
by net ISK/hr at local buy orders, accounting for POCO taxes and haul time.
Generates recommended 5-planet layouts using a greedy allocator.

### New config files

- `pi_config.ini` — home system, tax rate, hauler capacity, PG/CPU budgets, haul model
- `planet_inventory.ini` — available planets per system (editable via web UI)
- `planet_extraction.ini` — observed P0/hr extraction rates per planet type/system

### Skills.ini extension

Added `[planetary_industry]` section is NOT used (PI skills are read from the
existing `[skills]` section as `Command Center Upgrades`, `Interplanetary
Consolidation`, `Planetology`, `Advanced Planetology`, `Remote Sensing`).

### Data sources

- **EVE Ref SDE API** (`ref-data.everef.net`): PI schematics (cycle times, inputs,
  outputs), type info (volumes, base prices). Cached 30 days.
- **ESI**: market buy orders for all PI products (local region + Jita). Cached 5 min.
- **ESI groups**: type discovery for P0 (groups 1032/1033/1035), P1 (1042), P2 (1034), P3 (1040).

### Layout computation

Three layout types modeled:
1. **P1 extractor**: 1 planet — ECU + BIFs + launchpad
2. **P2 self-contained**: 1 planet — 2 ECUs + BIFs + AIF + launchpad (when both P0s exist on same planet type)
3. **P2 factory**: 3 planets — 2 extraction planets + 1 factory planet
4. **P3 multi-planet**: estimated planet count based on P0 dependency tree

### Web UI

New "PI Dossier" tab in ore_scanner.py with:
- Config controls (tax, hauler, max haul, market jumps)
- Planet inventory editor (editable table, saved via POST)
- Extraction rates editor
- Generate button → structured results + copy markdown

### API endpoints

- `GET /api/pi/config` — returns current config, inventory, extraction rates
- `GET /api/pi/generate?tax=&hauler_m3=&max_haul_minutes=&max_market_jumps=` — generate dossier
- `POST /api/pi/save-inventory` — save planet inventory JSON
- `POST /api/pi/save-extraction` — save extraction rates JSON

### Known limitations (v1)

- P3 chains show estimated planet counts but don't generate detailed per-planet layouts
- Planet type name "Microorganisms" (one word) matches EVE Ref; earlier code used "Micro Organisms"
- CCU budget table may not match user's in-game values exactly — user provides actual PG/CPU in config
- P4 production out of scope
- No POCO ownership auto-discovery
- No extractor cycle decay modeling (steady-state averages only)

---

## Fit Dossier — v1.2 fixes

### CPU/PG skill bonus application — now reads from ESI dogma

The v1.1 code hardcoded `0.05` (5%) as the per-level bonus for both
CPU Management and Power Grid Management:

```python
cpu_adj = cpu_base * (1 + 0.05 * cpu_skill)
```

v1.2 reads the actual per-level bonus attribute from the skill's ESI
type info at runtime via `_skill_bonus_per_level()`. The bonus attribute
IDs are:

- CPU Management (type 3426): attr 424 (`cpuOutputBonus2`)
- Power Grid Management (type 3413): attr 313 (`powerEngineeringOutputBonus`)

These are applied as PostPercent (the EVE dogma operator 6 pattern):
`base * (1 + bonus * level / 100)`.

Root cause of the investigation: the original spec reported a mismatch
between the dossier's 312 tf and a PyFA reading of 280.8 tf for CPU
Mgmt 4 on a Retriever. After thorough investigation (ESI types, dogma
effects, EVE Ref, EVE Uni wiki, patch notes, all ESI API versions),
every source confirmed +5%/level, and 312 tf is the correct value for
CPU Mgmt 4 against a 260-base hull. The PyFA reading turned out to be
an available-CPU figure (total minus fitted modules), not total output.

### Align time now applies Spaceship Command + Evasive Maneuvering

v1.1 computed align time from base agility only:

```python
align_time = -ln(0.25) * agility * mass / 1e6
```

v1.2 applies agility-modifying skills before computing align time:

- Spaceship Command (type 3327): attr 151 (`agilityBonus` = -2.0/level)
- Evasive Maneuvering (type 3453): attr 151 (`agilityBonus` = -5.0/level)

Both bonuses are read from ESI at runtime and applied as multiplicative
PostPercent factors. The Navigation section now shows both base and
skill-adjusted inertia/align values.

### Smoke test catches this class of bug

The self-test (check 10) now runs a consistency smoke test:

1. Parses the Retriever hull with a fixed skill profile
2. Independently computes expected CPU, PG, and align from the same
   ESI bonus values and base stats
3. Verifies `parse_ship_hull()` produces identical results
4. Sanity-checks that adjusted values differ from base in the right
   direction (CPU/PG increase, align decreases)

This catches formula bugs (wrong operator, missing skill, stale
hardcoded constant) without encoding external target values that depend
on game patches.

### Audit of other skill-applied stats

Audited in this pass:

- **Mining hold capacity**: uses hull dogma effect (attr 3187 = 5.0 on
  Retriever, applied per Mining Barge level). This is a hull bonus, not
  a skill bonus — the 5% comes from the Retriever's own dogma, which is
  correct.
- **Capacitor / shield / armor / structure HP**: displayed as base
  values only. No skill bonuses applied in the dossier (correct — these
  are modified by modules, not shown as adjusted hull stats).
- **Max velocity**: shown as base only. Navigation skill (+5%/level to
  velocity) is not applied because the dossier shows hull stats, and
  velocity bonuses are situational (afterburners etc).

Not audited: drone bandwidth, signature radius, scan resolution. These
are displayed as base values and no skills modify them on the hull.

## v1.3 fixes

### Module/rig drawback columns

Modules and rigs can have penalties ("drawbacks") that affect the ship
or other modules but are NOT reflected in the module's own CPU/PG cost.
v1.3 surfaces these in a "Drawback" column in each category table.

Three drawback patterns discovered via ESI dogma:

1. **Generic `drawback` attr (1138)** — present on most rigs. The
   value (e.g. -10.0 or +10.0) is applied as PostPercent to a ship
   attribute determined by the rig's dogma effect:

   | Effect ID | Effect name | Target attr | Label |
   |-----------|-------------|-------------|-------|
   | 2712 | drawbackArmorHP | 265 (armorHP) | armor HP |
   | 2713 | drawbackCPUOutput | 48 (cpuOutput) | ship CPU output |
   | 2714 | drawbackCPUNeedLaunchers | 50 (cpu) | launcher CPU need |
   | 2716 | drawbackSigRad | 552 (signatureRadius) | signature radius |
   | 2717 | drawbackAgility | 70 (agility) | agility |
   | 2718 | drawbackShieldCapacity | 263 (shieldCapacity) | shield capacity |

   Some rigs (e.g. Capital Drone rigs) use custom per-rig effects
   instead of the standard ones above. The code handles this by falling
   back to ESI effect lookup: it scans the module's effects for any
   modifier that applies attr 1138 to a ship attribute (domain=shipID,
   operator=6). Results are cached.

2. **`cpuPenaltyPercent` attr (1082)** — on Mining Laser Upgrades.
   Value is the percentage increase in CPU usage of the mining lasers
   they upgrade. E.g. MLU I has cpuPenaltyPercent=10 → "+10% mining
   laser CPU need". Stacking-penalised when multiple MLUs are fitted.

3. **`shieldRechargeRateMultiplier` attr (134)** — on Processor
   Overclocking Unit rigs. Value > 1.0 means slower recharge. E.g.
   Medium Processor OC Unit I has 1.05 → "+5% shield recharge time".

### Rigs that do NOT have drawbacks

Contrary to what some wiki descriptions imply, these rigs have no
drawback attribute in the current SDE:

- Ancillary Current Router (all sizes)
- Capacitor Control Circuit (all sizes)
- Semiconductor Memory Cell (all sizes)
- Egress Port Maximizer (all sizes)
- Command Processor (all sizes)

### AI Notes section updated

The dossier's "Notes for the AI step" now warns about drawback budget
arithmetic: MLU CPU penalty stacking, drone mining augmentor CPU
reduction, and other rig drawbacks.

### Smoke test (checks 11)

Self-test now verifies:
- MLU I has cpuPenaltyPercent = 10 and the drawback is surfaced
- Drone Mining Augmentor I has drawback = -10 and it's surfaced as
  "ship CPU output"

## v1.4 fixes

### Porpoise / Industrial Command Ships support

The Porpoise (type 42244, group 941 "Industrial Command Ship") is now
fully supported as a dossier target. Key additions:

**New module groups enumerated:**
- Industrial Cores (seed: Medium Industrial Core I, 62590)
- Mining Foreman Bursts (seed: Mining Foreman Burst I, 42528)
- Remote Shield Boosters (seed: Small Remote Shield Booster I, 3586)
- Compressors (seed: Medium Asteroid Ore Compressor I, 62622) — ore,
  gas, ice, mercoxit, and moon ore compressors are all in group 4174

**Solo-drone-mining lens:**
- When ship_class == "Industrial Command Ship", high-slot categories
  are reordered: utility modules (cores, bursts, compressors) first,
  then mining lasers last with a "(no hull laser bonus)" note.
- AI Notes section gets ICS-specific guidance about drone-first mining
  and Industrial Core fuel usage.
- Drone table header shows the +10% drone mining yield per ICS level
  bonus (attr 3221 on hull, effect 8294).

**Capacity surfacing:**
- Fleet hangar (attr 912, `fleetHangarCapacity`) shown when > 0.
- Fuel bay (attr 1549, `specialFuelBayCapacity`) shown when > 0.
- Ore hold skill bonus now dynamic: Mining Barge for barges, Industrial
  Command Ships for ICS, Exhumers for exhumers.

**Skills.ini:** Industrial Command Ships set to 0 (untrained, planning
ahead). Required skill type_id = 29637.

### Engineering rig drawback audit

Re-audited Capacitor Control Circuit, Semiconductor Memory Cell,
Egress Port Maximizer, and Ancillary Current Router. All confirmed
to have NO drawback attribute (1138) in the current SDE. Their
Drawback column correctly shows "—". These rigs are strictly positive
with no penalties.

### Porpoise ESI data reference

- Type ID: 42244, group: 941
- Slots: 4H / 4M / 2L / 3R (cal 400)
- CPU: 350, PG: 420, Cap: 3500 GJ
- Drone bay: 125 m³, BW: 50 Mbit/s
- Mining hold: 50,000 m³, fleet hangar: 5,000 m³, fuel bay: 4,800 m³
- Mass: 4,500,000 kg, agility: 1.5
- Required skill: attr 182 = 29637 (Industrial Command Ships)

## v1.5 fixes — Porpoise data corrections

Five issues fixed, all rooted in `_extract_hull_bonuses` being too
narrow for non-barge hulls.

### 1. ICS skill now reads correctly from skills.ini

Root cause: the bonus extractor only knew "miningBarge" and "exhumer"
effect name patterns. ICS effects use "industrialCommand" prefix.
Added to SKILL_PATTERNS map. Now shows "at Industrial Command Ships 1"
instead of "at ship skill 0".

### 2. All per-level bonuses surfaced

Root cause: `MODIFIED_NAMES` dict only contained attrs 77, 73, 1556.
Porpoise bonuses modify drone damage (64), drone HP (9/263/265),
cargo (38), burst range (54), burst strength (2469+), etc. Replaced
the allowlist with an expanded `ATTR_LABELS` dict covering all common
ship attributes, with ESI fallback for unknown ones.

### 3. Role bonuses now appear for all hull classes

Root cause: role bonuses were also filtered by `MODIFIED_NAMES`.
The rewritten extractor surfaces all role bonuses regardless of
which attributes they modify. Porpoise now shows all 6 role bonuses
(RSB range, burst PG reduction, tractor bonuses, burst relay, etc.).
Retriever role bonuses unchanged (no regression).

### 4. Bonus labels use full descriptions

Root cause: the old code only labeled "ore mining yield", "cycle time",
"mining hold capacity". The rewrite groups multiple target attributes
into one line (e.g. "+10% armor HP, structure HP, shield HP") and uses
descriptive labels from `ATTR_LABELS`.

### 5. Industrial Cores / Compressors filtered by hull size

Added `SIZE_FILTERED_CATS` set for categories that share ESI groups
with modules from other ship classes. For these categories, candidates
are filtered by name prefix matching the hull's rig size (attr 1547):
Small/Medium/Large/Capital. Porpoise (Medium) now shows only Medium
Industrial Core I/II and Medium compressors. Bastion Module, Siege
Module, and Large variants are excluded.

### ICS skill scaling verified

At ICS 5: Mining Drone I yield correctly scales to 37.5 m³ (+50%),
mining hold scales to 62,500 m³ (+25%). Drone yield bonus uses
hull attr 3221 (=10.0) × skill level, applied in the drone table.

## v1.6 fixes — Bonus extraction correctness

### Switched from dogma attribute synthesis to SDE traits

Root cause of all v1.5 bonus labelling issues: `_extract_hull_bonuses`
was reverse-engineering bonus descriptions from raw dogma attribute IDs
and effect names. This produced fabricated entries ("+10% armor HP" from
base stat attributes), mislabelled bonuses ("optimal range" instead of
"Mining Foreman Burst range"), and wrong section assignments.

The fix: `_extract_hull_bonuses` now reads the `traits` field from the
EVE Ref SDE API (`ref-data.everef.net/types/{id}`), which contains
canonical, human-readable bonus text identical to the in-game info
window. ESI's `/universe/types/` endpoint does NOT include `traits`.

New helper added to `eve_common.py`: `get_type_traits(type_id)` fetches
from `ref-data.everef.net` with 7-day file caching (same as ESI static
data).

### Traits structure

```
traits.types[skill_type_id][n] = {
    bonus: 5,          // per-level value
    bonus_text.en: "bonus to ship cargo...",
    importance: 1,     // display order
    unit_id: 105       // 105 = percentage
}
traits.role_bonuses[n] = {
    bonus: 400,        // or absent for capability-style
    bonus_text.en: "bonus to RSB optimal range",
    importance: 2
}
```

Capability-style entries (no `bonus` field) are shown as-is:
"can fit Medium Industrial Core", "Can use two Command Burst modules".

### Results

Porpoise: exactly 5 per-level + 8 role bonuses, all matching in-game.
Retriever: 3 per-level + 3 role, labels now use canonical SDE text.
No fabricated entries. No wrong-section assignments.
