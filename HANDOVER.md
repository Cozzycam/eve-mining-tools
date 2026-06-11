# EVE Mining Tools — Handover Notes

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
