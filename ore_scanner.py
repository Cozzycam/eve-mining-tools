#!/usr/bin/env python3
"""
EVE Ore Profitability Scanner v0.8
Browser-based tool that pulls live buy-order data from CCP's ESI
and ranks mineable ores by ISK/m3, with travel-aware ISK/hour estimates.

Run:  python ore_scanner.py
      (or double-click "EVE Ore Scanner.bat")

No API key needed -- ESI market data is public.
Zero dependencies -- Python stdlib only.
"""

import http.server
import json
import sys
import threading
import time
import urllib.request
import urllib.error
import webbrowser
from urllib.parse import urlparse, parse_qs

from eve_common import (
    REGION_LIST, REGIONS,
    resolve_system_name, resolve_station_name,
    search_system_id, get_jump_count, fetch_buy_orders,
    fetch_best_buy,
)

# ── Ore definitions ───────────────────────────────────────────
# cls = ore class (1=highsec, 2=lowsec, 3=nullsec, 4=R4, 5=R8, 6=R16, 7=R32, 8=R64)
BELT_ORES = [
    # ── Class I: Highsec ──
    {"id": 1230,  "name": "Veldspar",               "vol": 0.1,  "group": "Veldspar",     "cls": 1},
    {"id": 17470, "name": "Veldspar II-Grade",       "vol": 0.1,  "group": "Veldspar",     "cls": 1},
    {"id": 17471, "name": "Veldspar III-Grade",      "vol": 0.1,  "group": "Veldspar",     "cls": 1},
    {"id": 46689, "name": "Veldspar IV-Grade",      "vol": 0.1,  "group": "Veldspar",     "cls": 1},
    {"id": 1228,  "name": "Scordite",               "vol": 0.15, "group": "Scordite",     "cls": 1},
    {"id": 17463, "name": "Scordite II-Grade",       "vol": 0.15, "group": "Scordite",     "cls": 1},
    {"id": 17464, "name": "Scordite III-Grade",      "vol": 0.15, "group": "Scordite",     "cls": 1},
    {"id": 46687, "name": "Scordite IV-Grade",      "vol": 0.15, "group": "Scordite",     "cls": 1},
    {"id": 1224,  "name": "Pyroxeres",              "vol": 0.3,  "group": "Pyroxeres",    "cls": 1},
    {"id": 17459, "name": "Pyroxeres II-Grade",      "vol": 0.3,  "group": "Pyroxeres",    "cls": 1},
    {"id": 17460, "name": "Pyroxeres III-Grade",     "vol": 0.3,  "group": "Pyroxeres",    "cls": 1},
    {"id": 46686, "name": "Pyroxeres IV-Grade",     "vol": 0.3,  "group": "Pyroxeres",    "cls": 1},
    {"id": 18,    "name": "Plagioclase",             "vol": 0.35, "group": "Plagioclase",  "cls": 1},
    {"id": 17455, "name": "Plagioclase II-Grade",    "vol": 0.35, "group": "Plagioclase",  "cls": 1},
    {"id": 17456, "name": "Plagioclase III-Grade",   "vol": 0.35, "group": "Plagioclase",  "cls": 1},
    {"id": 46685, "name": "Plagioclase IV-Grade",   "vol": 0.35, "group": "Plagioclase",  "cls": 1},
    {"id": 1227,  "name": "Omber",                   "vol": 0.6,  "group": "Omber",        "cls": 1},
    {"id": 17867, "name": "Omber II-Grade",          "vol": 0.6,  "group": "Omber",        "cls": 1},
    {"id": 17868, "name": "Omber III-Grade",         "vol": 0.6,  "group": "Omber",        "cls": 1},
    {"id": 46684, "name": "Omber IV-Grade",         "vol": 0.6,  "group": "Omber",        "cls": 1},
    {"id": 20,    "name": "Kernite",                 "vol": 1.2,  "group": "Kernite",      "cls": 1},
    {"id": 17452, "name": "Kernite II-Grade",        "vol": 1.2,  "group": "Kernite",      "cls": 1},
    {"id": 17453, "name": "Kernite III-Grade",       "vol": 1.2,  "group": "Kernite",      "cls": 1},
    {"id": 46683, "name": "Kernite IV-Grade",       "vol": 1.2,  "group": "Kernite",      "cls": 1},
    {"id": 74521, "name": "Mordunium",               "vol": 0.1,  "group": "Mordunium",    "cls": 1},
    {"id": 74522, "name": "Mordunium II-Grade",      "vol": 0.1,  "group": "Mordunium",    "cls": 1},
    {"id": 74523, "name": "Mordunium III-Grade",     "vol": 0.1,  "group": "Mordunium",    "cls": 1},
    {"id": 74524, "name": "Mordunium IV-Grade",     "vol": 0.1,  "group": "Mordunium",    "cls": 1},
    {"id": 74525, "name": "Ytirium",                 "vol": 0.6,  "group": "Ytirium",      "cls": 1},
    {"id": 74526, "name": "Ytirium II-Grade",        "vol": 0.6,  "group": "Ytirium",      "cls": 1},
    {"id": 74527, "name": "Ytirium III-Grade",       "vol": 0.6,  "group": "Ytirium",      "cls": 1},
    {"id": 74528, "name": "Ytirium IV-Grade",       "vol": 0.6,  "group": "Ytirium",      "cls": 1},
    # ── Class II: Lowsec ──
    {"id": 1232,  "name": "Dark Ochre",              "vol": 8.0,  "group": "Dark Ochre",   "cls": 2},
    {"id": 17436, "name": "Dark Ochre II-Grade",     "vol": 8.0,  "group": "Dark Ochre",   "cls": 2},
    {"id": 17437, "name": "Dark Ochre III-Grade",    "vol": 8.0,  "group": "Dark Ochre",   "cls": 2},
    {"id": 46675, "name": "Dark Ochre IV-Grade",    "vol": 8.0,  "group": "Dark Ochre",   "cls": 2},
    {"id": 1229,  "name": "Gneiss",                  "vol": 5.0,  "group": "Gneiss",       "cls": 2},
    {"id": 17865, "name": "Gneiss II-Grade",         "vol": 5.0,  "group": "Gneiss",       "cls": 2},
    {"id": 17866, "name": "Gneiss III-Grade",        "vol": 5.0,  "group": "Gneiss",       "cls": 2},
    {"id": 46679, "name": "Gneiss IV-Grade",        "vol": 5.0,  "group": "Gneiss",       "cls": 2},
    {"id": 81975, "name": "Griemeer",                "vol": 0.8,  "group": "Griemeer",     "cls": 2},
    {"id": 81976, "name": "Griemeer II-Grade",       "vol": 0.8,  "group": "Griemeer",     "cls": 2},
    {"id": 81977, "name": "Griemeer III-Grade",      "vol": 0.8,  "group": "Griemeer",     "cls": 2},
    {"id": 81978, "name": "Griemeer IV-Grade",      "vol": 0.8,  "group": "Griemeer",     "cls": 2},
    {"id": 21,    "name": "Hedbergite",              "vol": 3.0,  "group": "Hedbergite",   "cls": 2},
    {"id": 17440, "name": "Hedbergite II-Grade",     "vol": 3.0,  "group": "Hedbergite",   "cls": 2},
    {"id": 17441, "name": "Hedbergite III-Grade",    "vol": 3.0,  "group": "Hedbergite",   "cls": 2},
    {"id": 46680, "name": "Hedbergite IV-Grade",    "vol": 3.0,  "group": "Hedbergite",   "cls": 2},
    {"id": 1231,  "name": "Hemorphite",              "vol": 3.0,  "group": "Hemorphite",   "cls": 2},
    {"id": 17444, "name": "Hemorphite II-Grade",     "vol": 3.0,  "group": "Hemorphite",   "cls": 2},
    {"id": 17445, "name": "Hemorphite III-Grade",    "vol": 3.0,  "group": "Hemorphite",   "cls": 2},
    {"id": 46681, "name": "Hemorphite IV-Grade",    "vol": 3.0,  "group": "Hemorphite",   "cls": 2},
    {"id": 82163, "name": "Hezorime",                "vol": 5.0,  "group": "Hezorime",     "cls": 2},
    {"id": 82164, "name": "Hezorime II-Grade",       "vol": 5.0,  "group": "Hezorime",     "cls": 2},
    {"id": 82165, "name": "Hezorime III-Grade",      "vol": 5.0,  "group": "Hezorime",     "cls": 2},
    {"id": 82166, "name": "Hezorime IV-Grade",      "vol": 5.0,  "group": "Hezorime",     "cls": 2},
    {"id": 1226,  "name": "Jaspet",                  "vol": 2.0,  "group": "Jaspet",       "cls": 2},
    {"id": 17448, "name": "Jaspet II-Grade",         "vol": 2.0,  "group": "Jaspet",       "cls": 2},
    {"id": 17449, "name": "Jaspet III-Grade",        "vol": 2.0,  "group": "Jaspet",       "cls": 2},
    {"id": 46682, "name": "Jaspet IV-Grade",        "vol": 2.0,  "group": "Jaspet",       "cls": 2},
    {"id": 81900, "name": "Kylixium",                "vol": 1.2,  "group": "Kylixium",     "cls": 2},
    {"id": 81901, "name": "Kylixium II-Grade",       "vol": 1.2,  "group": "Kylixium",     "cls": 2},
    {"id": 81902, "name": "Kylixium III-Grade",      "vol": 1.2,  "group": "Kylixium",     "cls": 2},
    {"id": 81903, "name": "Kylixium IV-Grade",      "vol": 1.2,  "group": "Kylixium",     "cls": 2},
    {"id": 82016, "name": "Nocxite",                 "vol": 4.0,  "group": "Nocxite",      "cls": 2},
    {"id": 82017, "name": "Nocxite II-Grade",        "vol": 4.0,  "group": "Nocxite",      "cls": 2},
    {"id": 82018, "name": "Nocxite III-Grade",       "vol": 4.0,  "group": "Nocxite",      "cls": 2},
    {"id": 82019, "name": "Nocxite IV-Grade",       "vol": 4.0,  "group": "Nocxite",      "cls": 2},
    {"id": 52315, "name": "Rakovene",                "vol": 16.0, "group": "Rakovene",     "cls": 2},
    {"id": 56629, "name": "Rakovene II-Grade",       "vol": 16.0, "group": "Rakovene",     "cls": 2},
    {"id": 56630, "name": "Rakovene III-Grade",      "vol": 16.0, "group": "Rakovene",     "cls": 2},
    {"id": 52306, "name": "Talassonite",             "vol": 16.0, "group": "Talassonite",  "cls": 2},
    {"id": 56625, "name": "Talassonite II-Grade",    "vol": 16.0, "group": "Talassonite",  "cls": 2},
    {"id": 56626, "name": "Talassonite III-Grade",   "vol": 16.0, "group": "Talassonite",  "cls": 2},
    # ── Class III: Nullsec ──
    {"id": 22,    "name": "Arkonor",                 "vol": 16.0, "group": "Arkonor",      "cls": 3},
    {"id": 17425, "name": "Arkonor II-Grade",        "vol": 16.0, "group": "Arkonor",      "cls": 3},
    {"id": 17426, "name": "Arkonor III-Grade",       "vol": 16.0, "group": "Arkonor",      "cls": 3},
    {"id": 46678, "name": "Arkonor IV-Grade",       "vol": 16.0, "group": "Arkonor",      "cls": 3},
    {"id": 52316, "name": "Bezdnacine",              "vol": 16.0, "group": "Bezdnacine",   "cls": 3},
    {"id": 56627, "name": "Bezdnacine II-Grade",     "vol": 16.0, "group": "Bezdnacine",   "cls": 3},
    {"id": 56628, "name": "Bezdnacine III-Grade",    "vol": 16.0, "group": "Bezdnacine",   "cls": 3},
    {"id": 1223,  "name": "Bistot",                  "vol": 16.0, "group": "Bistot",       "cls": 3},
    {"id": 17428, "name": "Bistot II-Grade",         "vol": 16.0, "group": "Bistot",       "cls": 3},
    {"id": 17429, "name": "Bistot III-Grade",        "vol": 16.0, "group": "Bistot",       "cls": 3},
    {"id": 46676, "name": "Bistot IV-Grade",        "vol": 16.0, "group": "Bistot",       "cls": 3},
    {"id": 1225,  "name": "Crokite",                 "vol": 16.0, "group": "Crokite",      "cls": 3},
    {"id": 17432, "name": "Crokite II-Grade",        "vol": 16.0, "group": "Crokite",      "cls": 3},
    {"id": 17433, "name": "Crokite III-Grade",       "vol": 16.0, "group": "Crokite",      "cls": 3},
    {"id": 46677, "name": "Crokite IV-Grade",       "vol": 16.0, "group": "Crokite",      "cls": 3},
    {"id": 74533, "name": "Ducinium",                "vol": 16.0, "group": "Ducinium",     "cls": 3},
    {"id": 74534, "name": "Ducinium II-Grade",       "vol": 16.0, "group": "Ducinium",     "cls": 3},
    {"id": 74535, "name": "Ducinium III-Grade",      "vol": 16.0, "group": "Ducinium",     "cls": 3},
    {"id": 74536, "name": "Ducinium IV-Grade",      "vol": 16.0, "group": "Ducinium",     "cls": 3},
    {"id": 74529, "name": "Eifyrium",                "vol": 16.0, "group": "Eifyrium",     "cls": 3},
    {"id": 74530, "name": "Eifyrium II-Grade",       "vol": 16.0, "group": "Eifyrium",     "cls": 3},
    {"id": 74531, "name": "Eifyrium III-Grade",      "vol": 16.0, "group": "Eifyrium",     "cls": 3},
    {"id": 74532, "name": "Eifyrium IV-Grade",      "vol": 16.0, "group": "Eifyrium",     "cls": 3},
    {"id": 11396, "name": "Mercoxit",                "vol": 40.0, "group": "Mercoxit",     "cls": 3},
    {"id": 17869, "name": "Mercoxit II-Grade",       "vol": 40.0, "group": "Mercoxit",     "cls": 3},
    {"id": 17870, "name": "Mercoxit III-Grade",      "vol": 40.0, "group": "Mercoxit",     "cls": 3},
    {"id": 19,    "name": "Spodumain",               "vol": 16.0, "group": "Spodumain",    "cls": 3},
    {"id": 17466, "name": "Spodumain II-Grade",      "vol": 16.0, "group": "Spodumain",    "cls": 3},
    {"id": 17467, "name": "Spodumain III-Grade",     "vol": 16.0, "group": "Spodumain",    "cls": 3},
    {"id": 46688, "name": "Spodumain IV-Grade",     "vol": 16.0, "group": "Spodumain",    "cls": 3},
    {"id": 82205, "name": "Ueganite",                "vol": 5.0,  "group": "Ueganite",     "cls": 3},
    {"id": 82206, "name": "Ueganite II-Grade",       "vol": 5.0,  "group": "Ueganite",     "cls": 3},
    {"id": 82207, "name": "Ueganite III-Grade",      "vol": 5.0,  "group": "Ueganite",     "cls": 3},
    {"id": 82208, "name": "Ueganite IV-Grade",      "vol": 5.0,  "group": "Ueganite",     "cls": 3},
]

# ── Moon ore definitions ─────────────────────────────────────
# Variant adjectives differ by tier:
#   R4: Brimful / Glistening    R8: Copious / Twinkling
#   R16: Lavish / Shimmering    R32: Replete / Glowing
#   R64: Bountiful / Shining
MOON_ORES = [
    # ── R4: Ubiquitous ──
    {"id": 45490, "name": "Zeolites",              "vol": 10.0, "group": "Zeolites",   "cls": 4, "tier": "r4"},
    {"id": 46280, "name": "Brimful Zeolites",      "vol": 10.0, "group": "Zeolites",   "cls": 4, "tier": "r4"},
    {"id": 46281, "name": "Glistening Zeolites",   "vol": 10.0, "group": "Zeolites",   "cls": 4, "tier": "r4"},
    {"id": 45491, "name": "Sylvite",               "vol": 10.0, "group": "Sylvite",    "cls": 4, "tier": "r4"},
    {"id": 46282, "name": "Brimful Sylvite",       "vol": 10.0, "group": "Sylvite",    "cls": 4, "tier": "r4"},
    {"id": 46283, "name": "Glistening Sylvite",    "vol": 10.0, "group": "Sylvite",    "cls": 4, "tier": "r4"},
    {"id": 45492, "name": "Bitumens",              "vol": 10.0, "group": "Bitumens",   "cls": 4, "tier": "r4"},
    {"id": 46284, "name": "Brimful Bitumens",      "vol": 10.0, "group": "Bitumens",   "cls": 4, "tier": "r4"},
    {"id": 46285, "name": "Glistening Bitumens",   "vol": 10.0, "group": "Bitumens",   "cls": 4, "tier": "r4"},
    {"id": 45493, "name": "Coesite",               "vol": 10.0, "group": "Coesite",    "cls": 4, "tier": "r4"},
    {"id": 46286, "name": "Brimful Coesite",       "vol": 10.0, "group": "Coesite",    "cls": 4, "tier": "r4"},
    {"id": 46287, "name": "Glistening Coesite",    "vol": 10.0, "group": "Coesite",    "cls": 4, "tier": "r4"},
    # ── R8: Common ──
    {"id": 45494, "name": "Cobaltite",             "vol": 10.0, "group": "Cobaltite",  "cls": 5, "tier": "r8"},
    {"id": 46288, "name": "Copious Cobaltite",     "vol": 10.0, "group": "Cobaltite",  "cls": 5, "tier": "r8"},
    {"id": 46289, "name": "Twinkling Cobaltite",   "vol": 10.0, "group": "Cobaltite",  "cls": 5, "tier": "r8"},
    {"id": 45495, "name": "Euxenite",              "vol": 10.0, "group": "Euxenite",   "cls": 5, "tier": "r8"},
    {"id": 46290, "name": "Copious Euxenite",      "vol": 10.0, "group": "Euxenite",   "cls": 5, "tier": "r8"},
    {"id": 46291, "name": "Twinkling Euxenite",    "vol": 10.0, "group": "Euxenite",   "cls": 5, "tier": "r8"},
    {"id": 45496, "name": "Titanite",              "vol": 10.0, "group": "Titanite",   "cls": 5, "tier": "r8"},
    {"id": 46292, "name": "Copious Titanite",      "vol": 10.0, "group": "Titanite",   "cls": 5, "tier": "r8"},
    {"id": 46293, "name": "Twinkling Titanite",    "vol": 10.0, "group": "Titanite",   "cls": 5, "tier": "r8"},
    {"id": 45497, "name": "Scheelite",             "vol": 10.0, "group": "Scheelite",  "cls": 5, "tier": "r8"},
    {"id": 46294, "name": "Copious Scheelite",     "vol": 10.0, "group": "Scheelite",  "cls": 5, "tier": "r8"},
    {"id": 46295, "name": "Twinkling Scheelite",   "vol": 10.0, "group": "Scheelite",  "cls": 5, "tier": "r8"},
    # ── R16: Uncommon ──
    {"id": 45498, "name": "Otavite",               "vol": 10.0, "group": "Otavite",    "cls": 6, "tier": "r16"},
    {"id": 46296, "name": "Lavish Otavite",        "vol": 10.0, "group": "Otavite",    "cls": 6, "tier": "r16"},
    {"id": 46297, "name": "Shimmering Otavite",    "vol": 10.0, "group": "Otavite",    "cls": 6, "tier": "r16"},
    {"id": 45499, "name": "Sperrylite",            "vol": 10.0, "group": "Sperrylite", "cls": 6, "tier": "r16"},
    {"id": 46298, "name": "Lavish Sperrylite",     "vol": 10.0, "group": "Sperrylite", "cls": 6, "tier": "r16"},
    {"id": 46299, "name": "Shimmering Sperrylite", "vol": 10.0, "group": "Sperrylite", "cls": 6, "tier": "r16"},
    {"id": 45500, "name": "Vanadinite",            "vol": 10.0, "group": "Vanadinite", "cls": 6, "tier": "r16"},
    {"id": 46300, "name": "Lavish Vanadinite",     "vol": 10.0, "group": "Vanadinite", "cls": 6, "tier": "r16"},
    {"id": 46301, "name": "Shimmering Vanadinite", "vol": 10.0, "group": "Vanadinite", "cls": 6, "tier": "r16"},
    {"id": 45501, "name": "Chromite",              "vol": 10.0, "group": "Chromite",   "cls": 6, "tier": "r16"},
    {"id": 46302, "name": "Lavish Chromite",       "vol": 10.0, "group": "Chromite",   "cls": 6, "tier": "r16"},
    {"id": 46303, "name": "Shimmering Chromite",   "vol": 10.0, "group": "Chromite",   "cls": 6, "tier": "r16"},
    # ── R32: Rare ──
    {"id": 45502, "name": "Carnotite",             "vol": 10.0, "group": "Carnotite",  "cls": 7, "tier": "r32"},
    {"id": 46304, "name": "Replete Carnotite",     "vol": 10.0, "group": "Carnotite",  "cls": 7, "tier": "r32"},
    {"id": 46305, "name": "Glowing Carnotite",     "vol": 10.0, "group": "Carnotite",  "cls": 7, "tier": "r32"},
    {"id": 45503, "name": "Zircon",                "vol": 10.0, "group": "Zircon",     "cls": 7, "tier": "r32"},
    {"id": 46306, "name": "Replete Zircon",        "vol": 10.0, "group": "Zircon",     "cls": 7, "tier": "r32"},
    {"id": 46307, "name": "Glowing Zircon",        "vol": 10.0, "group": "Zircon",     "cls": 7, "tier": "r32"},
    {"id": 45504, "name": "Pollucite",             "vol": 10.0, "group": "Pollucite",  "cls": 7, "tier": "r32"},
    {"id": 46308, "name": "Replete Pollucite",     "vol": 10.0, "group": "Pollucite",  "cls": 7, "tier": "r32"},
    {"id": 46309, "name": "Glowing Pollucite",     "vol": 10.0, "group": "Pollucite",  "cls": 7, "tier": "r32"},
    {"id": 45506, "name": "Cinnabar",              "vol": 10.0, "group": "Cinnabar",   "cls": 7, "tier": "r32"},
    {"id": 46310, "name": "Replete Cinnabar",      "vol": 10.0, "group": "Cinnabar",   "cls": 7, "tier": "r32"},
    {"id": 46311, "name": "Glowing Cinnabar",      "vol": 10.0, "group": "Cinnabar",   "cls": 7, "tier": "r32"},
    # ── R64: Exceptional ──
    {"id": 45510, "name": "Xenotime",              "vol": 10.0, "group": "Xenotime",   "cls": 8, "tier": "r64"},
    {"id": 46312, "name": "Bountiful Xenotime",    "vol": 10.0, "group": "Xenotime",   "cls": 8, "tier": "r64"},
    {"id": 46313, "name": "Shining Xenotime",      "vol": 10.0, "group": "Xenotime",   "cls": 8, "tier": "r64"},
    {"id": 45511, "name": "Monazite",              "vol": 10.0, "group": "Monazite",   "cls": 8, "tier": "r64"},
    {"id": 46314, "name": "Bountiful Monazite",    "vol": 10.0, "group": "Monazite",   "cls": 8, "tier": "r64"},
    {"id": 46315, "name": "Shining Monazite",      "vol": 10.0, "group": "Monazite",   "cls": 8, "tier": "r64"},
    {"id": 45512, "name": "Loparite",              "vol": 10.0, "group": "Loparite",   "cls": 8, "tier": "r64"},
    {"id": 46316, "name": "Bountiful Loparite",    "vol": 10.0, "group": "Loparite",   "cls": 8, "tier": "r64"},
    {"id": 46317, "name": "Shining Loparite",      "vol": 10.0, "group": "Loparite",   "cls": 8, "tier": "r64"},
    {"id": 45513, "name": "Ytterbite",             "vol": 10.0, "group": "Ytterbite",  "cls": 8, "tier": "r64"},
    {"id": 46318, "name": "Bountiful Ytterbite",   "vol": 10.0, "group": "Ytterbite",  "cls": 8, "tier": "r64"},
    {"id": 46319, "name": "Shining Ytterbite",     "vol": 10.0, "group": "Ytterbite",  "cls": 8, "tier": "r64"},
]

# ── Erratic ore definitions (phased asteroid fields) ────────
# Reprocessing is random (yields 1 of 8 minerals per batch) — no fixed formula.
ERRATIC_ORES = [
    {"id": 90041, "name": "Prismaticite", "vol": 40.0, "group": "Prismaticite", "cls": 9},
]

# ── Compressed type IDs (raw_id → compressed_id, all 100:1 ratio) ─
COMP_IDS = {
    # Belt: Highsec
    1230: 62516, 17470: 62517, 17471: 62518, 46689: 62519,      # Veldspar
    1228: 62520, 17463: 62521, 17464: 62522, 46687: 62523,      # Scordite
    1224: 62524, 17459: 62525, 17460: 62526, 46686: 62527,      # Pyroxeres
    18: 62528, 17455: 62529, 17456: 62530, 46685: 62531,        # Plagioclase
    1227: 62532, 17867: 62533, 17868: 62534, 46684: 62535,      # Omber
    20: 62536, 17452: 62537, 17453: 62538, 46683: 62539,        # Kernite
    74521: 75275, 74522: 75276, 74523: 75277, 74524: 75278,     # Mordunium
    74525: 75279, 74526: 75280, 74527: 75281, 74528: 75282,     # Ytirium
    # Belt: Lowsec
    1232: 62556, 17436: 62557, 17437: 62558, 46675: 62559,      # Dark Ochre
    1229: 62552, 17865: 62553, 17866: 62554, 46679: 62555,      # Gneiss
    81975: 82316, 81976: 82317, 81977: 82318, 81978: 82319,     # Griemeer
    21: 62548, 17440: 62549, 17441: 62550, 46680: 62551,        # Hedbergite
    1231: 62544, 17444: 62545, 17445: 62546, 46681: 62547,      # Hemorphite
    82163: 82312, 82164: 82313, 82165: 82314, 82166: 82315,     # Hezorime
    1226: 62540, 17448: 62541, 17449: 62542, 46682: 62543,      # Jaspet
    81900: 82300, 81901: 82301, 81902: 82302, 81903: 82303,     # Kylixium
    82016: 82304, 82017: 82305, 82018: 82306, 82019: 82307,     # Nocxite
    52315: 62579, 56629: 62580, 56630: 62581,                    # Rakovene
    52306: 62582, 56625: 62583, 56626: 62584,                    # Talassonite
    # Belt: Nullsec
    22: 62568, 17425: 62569, 17426: 62570, 46678: 62571,        # Arkonor
    52316: 62576, 56627: 62577, 56628: 62578,                    # Bezdnacine
    1223: 62564, 17428: 62565, 17429: 62566, 46676: 62567,      # Bistot
    1225: 62560, 17432: 62561, 17433: 62562, 46677: 62563,      # Crokite
    74533: 75287, 74534: 75288, 74535: 75289, 74536: 75290,     # Ducinium
    74529: 75283, 74530: 75284, 74531: 75285, 74532: 75286,     # Eifyrium
    11396: 62586, 17869: 62587, 17870: 62588,                    # Mercoxit
    19: 62572, 17466: 62573, 17467: 62574, 46688: 62575,        # Spodumain
    82205: 82308, 82206: 82309, 82207: 82310, 82208: 82311,     # Ueganite
    # Moon: R4 Ubiquitous
    45490: 62463, 46280: 62464, 46281: 62467,                    # Zeolites
    45491: 62460, 46282: 62461, 46283: 62466,                    # Sylvite
    45492: 62454, 46284: 62455, 46285: 62456,                    # Bitumens
    45493: 62457, 46286: 62458, 46287: 62459,                    # Coesite
    # Moon: R8 Common
    45494: 62474, 46288: 62475, 46289: 62476,                    # Cobaltite
    45495: 62471, 46290: 62472, 46291: 62473,                    # Euxenite
    45496: 62477, 46292: 62478, 46293: 62479,                    # Titanite
    45497: 62468, 46294: 62469, 46295: 62470,                    # Scheelite
    # Moon: R16 Uncommon
    45498: 62483, 46296: 62484, 46297: 62485,                    # Otavite
    45499: 62486, 46298: 62487, 46299: 62488,                    # Sperrylite
    45500: 62489, 46300: 62490, 46301: 62491,                    # Vanadinite
    45501: 62480, 46302: 62481, 46303: 62482,                    # Chromite
    # Moon: R32 Rare
    45502: 62492, 46304: 62493, 46305: 62494,                    # Carnotite
    45503: 62501, 46306: 62502, 46307: 62503,                    # Zircon
    45504: 62498, 46308: 62499, 46309: 62500,                    # Pollucite
    45506: 62495, 46310: 62496, 46311: 62497,                    # Cinnabar
    # Moon: R64 Exceptional
    45510: 62510, 46312: 62511, 46313: 62512,                    # Xenotime
    45511: 62507, 46314: 62508, 46315: 62509,                    # Monazite
    45512: 62504, 46316: 62505, 46317: 62506,                    # Loparite
    45513: 62513, 46318: 62514, 46319: 62515,                    # Ytterbite
    # Erratic
    90041: 90307,                                                # Prismaticite
}

COMPRESSION_RATIO = 100  # universal: 100 raw units → 1 compressed unit

# ── Reprocessing formulas (per 100-unit batch, base ore) ─────
# Brimful/Copious/Lavish/Replete/Bountiful = x1.15
# Glistening/Twinkling/Shimmering/Glowing/Shining = x2.0
REPRO_FORMULAS = {
    # R4: minerals + moon goo
    45490: [(35, 8000), (36, 400), (16634, 65)],     # Zeolites → Pyerite, Mexallon, Atmospheric Gases
    45491: [(35, 4000), (36, 400), (16635, 65)],     # Sylvite → Pyerite, Mexallon, Evaporite Deposits
    45492: [(35, 6000), (36, 400), (16633, 65)],     # Bitumens → Pyerite, Mexallon, Hydrocarbons
    45493: [(35, 2000), (36, 400), (16636, 65)],     # Coesite → Pyerite, Mexallon, Silicates
    # R8: single moon material
    45494: [(16640, 40)],                             # Cobaltite → Cobalt
    45495: [(16639, 40)],                             # Euxenite → Scandium
    45496: [(16638, 40)],                             # Titanite → Titanium
    45497: [(16637, 40)],                             # Scheelite → Tungsten
    # R16: R16 goo + R4 goo
    45498: [(16643, 40), (16634, 10)],               # Otavite → Cadmium, Atmospheric Gases
    45499: [(16644, 40), (16635, 10)],               # Sperrylite → Platinum, Evaporite Deposits
    45500: [(16642, 40), (16636, 10)],               # Vanadinite → Vanadium, Silicates
    45501: [(16641, 40), (16633, 10)],               # Chromite → Chromium, Hydrocarbons
    # R32: R32 + R8 + R4 goo
    45502: [(16649, 50), (16640, 10), (16634, 15)],  # Carnotite → Technetium, Cobalt, Atmo Gases
    45503: [(16648, 50), (16638, 10), (16636, 15)],  # Zircon → Hafnium, Titanium, Silicates
    45504: [(16647, 50), (16639, 10), (16633, 15)],  # Pollucite → Caesium, Scandium, Hydrocarbons
    45506: [(16646, 50), (16637, 10), (16635, 15)],  # Cinnabar → Mercury, Tungsten, Evap Deposits
    # R64: R64 + R16 + R8 + R4 goo
    45510: [(16650, 22), (16642, 10), (16640, 20), (16634, 20)],  # Xenotime
    45511: [(16651, 22), (16641, 10), (16637, 20), (16635, 20)],  # Monazite
    45512: [(16652, 22), (16644, 10), (16639, 20), (16633, 20)],  # Loparite
    45513: [(16653, 22), (16643, 10), (16638, 20), (16636, 20)],  # Ytterbite
    # ── Belt ores: minerals per 100 units of base ore ──────────
    # Highsec
    1230:  [(34, 400)],                                      # Veldspar → Tritanium
    1228:  [(34, 150), (35, 110)],                           # Scordite → Tritanium, Pyerite
    1224:  [(35, 90), (36, 30)],                             # Pyroxeres → Pyerite, Mexallon
    18:    [(34, 175), (36, 70)],                            # Plagioclase → Tritanium, Mexallon
    1227:  [(35, 90), (37, 75)],                             # Omber → Pyerite, Isogen
    20:    [(36, 60), (37, 120)],                            # Kernite → Mexallon, Isogen
    74521: [(35, 97)],                                       # Mordunium → Pyerite
    74525: [(37, 240)],                                      # Ytirium → Isogen
    # Lowsec
    1232:  [(36, 1360), (37, 1200), (38, 320)],              # Dark Ochre → Mexallon, Isogen, Nocxium
    1229:  [(35, 2000), (36, 1500), (37, 800)],              # Gneiss → Pyerite, Mexallon, Isogen
    81975: [(34, 250), (37, 80)],                            # Griemeer → Tritanium, Isogen
    21:    [(35, 450), (38, 120)],                           # Hedbergite → Pyerite, Nocxium
    1231:  [(37, 240), (38, 90)],                            # Hemorphite → Isogen, Nocxium
    82163: [(34, 2000), (37, 120), (39, 60)],                # Hezorime → Tritanium, Isogen, Zydrine
    1226:  [(36, 150), (38, 50)],                            # Jaspet → Mexallon, Nocxium
    81900: [(34, 300), (35, 200), (36, 550)],                # Kylixium → Tritanium, Pyerite, Mexallon
    82016: [(34, 900), (35, 150), (38, 105)],                # Nocxite → Tritanium, Pyerite, Nocxium
    52315: [(34, 40000), (37, 3200), (39, 200)],             # Rakovene → Tritanium, Isogen, Zydrine
    52306: [(34, 40000), (38, 960), (40, 32)],               # Talassonite → Tritanium, Nocxium, Megacyte
    # Nullsec
    22:    [(35, 3200), (36, 1200), (40, 120)],              # Arkonor → Pyerite, Mexallon, Megacyte
    52316: [(34, 40000), (37, 4800), (40, 128)],             # Bezdnacine → Tritanium, Isogen, Megacyte
    1223:  [(35, 3200), (36, 1200), (39, 160)],              # Bistot → Pyerite, Mexallon, Zydrine
    1225:  [(35, 800), (36, 2000), (38, 800)],               # Crokite → Pyerite, Mexallon, Nocxium
    74533: [(40, 170)],                                      # Ducinium → Megacyte
    74529: [(39, 266)],                                      # Eifyrium → Zydrine
    11396: [(11399, 140)],                                   # Mercoxit → Morphite
    19:    [(34, 48000), (37, 1000), (38, 160), (39, 80), (40, 40)],  # Spodumain
    82205: [(34, 800), (40, 40)],                            # Ueganite → Tritanium, Megacyte
}

# Map every moon ore variant → (base_ore_id, yield_multiplier)
REPRO_VARIANTS = {}
_MOON_BASES = [45490, 45491, 45492, 45493,   # R4
               45494, 45495, 45496, 45497,   # R8
               45498, 45499, 45500, 45501,   # R16
               45502, 45503, 45504, 45506,   # R32
               45510, 45511, 45512, 45513]   # R64
# Improved variants (+15%): type IDs 46280-46319 in order
_IMPROVED = [46280, 46282, 46284, 46286,     # R4 Brimful
             46288, 46290, 46292, 46294,     # R8 Copious
             46296, 46298, 46300, 46302,     # R16 Lavish
             46304, 46306, 46308, 46310,     # R32 Replete
             46312, 46314, 46316, 46318]     # R64 Bountiful
# Excellent variants (+100%): type IDs 46281-46319 (odd offsets)
_EXCELLENT = [46281, 46283, 46285, 46287,     # R4 Glistening
              46289, 46291, 46293, 46295,     # R8 Twinkling
              46297, 46299, 46301, 46303,     # R16 Shimmering
              46305, 46307, 46309, 46311,     # R32 Glowing
              46313, 46315, 46317, 46319]     # R64 Shining
for _i, _base in enumerate(_MOON_BASES):
    REPRO_VARIANTS[_base] = (_base, 1.0)
    REPRO_VARIANTS[_IMPROVED[_i]] = (_base, 1.15)
    REPRO_VARIANTS[_EXCELLENT[_i]] = (_base, 2.0)

# Belt ore variants: II-Grade +5%, III-Grade +10%, IV-Grade +15%
for _o in BELT_ORES:
    _bases = [b for b in BELT_ORES if b["group"] == _o["group"] and "Grade" not in b["name"]]
    _base_id = _bases[0]["id"] if _bases else _o["id"]
    if "IV-Grade" in _o["name"]:
        REPRO_VARIANTS[_o["id"]] = (_base_id, 1.15)
    elif "III-Grade" in _o["name"]:
        REPRO_VARIANTS[_o["id"]] = (_base_id, 1.10)
    elif "II-Grade" in _o["name"]:
        REPRO_VARIANTS[_o["id"]] = (_base_id, 1.05)
    else:
        REPRO_VARIANTS[_o["id"]] = (_base_id, 1.0)

# All unique material type IDs used in reprocessing (for price fetching)
REPRO_MATERIAL_IDS = set()
for _formula in REPRO_FORMULAS.values():
    for _mat_id, _ in _formula:
        REPRO_MATERIAL_IDS.add(_mat_id)

# Random reprocessing for erratic ores (per 100 units, 100% yield)
# Each batch randomly produces ONE mineral from the list with variable quantity
RANDOM_REPRO_RANGES = {
    90041: [  # Prismaticite
        (34, 368000, 496800),   # Tritanium
        (35, 89464, 111830),    # Pyerite
        (36, 35420, 45540),     # Mexallon
        (37, 23920, 31280),     # Isogen
        (38, 2875, 4025),       # Nocxium
        (39, 1299, 1528),       # Zydrine
        (40, 634, 830),         # Megacyte
        (11399, 312, 624),      # Morphite
    ],
}
# Ensure all random repro minerals are in the price-fetch set
for _outcomes in RANDOM_REPRO_RANGES.values():
    for _mat_id, _, _ in _outcomes:
        REPRO_MATERIAL_IDS.add(_mat_id)

# Assign categories and combine into single ORES list
for _o in BELT_ORES:
    _o["cat"] = "belt"
for _o in MOON_ORES:
    _o["cat"] = "moon"
for _o in ERRATIC_ORES:
    _o["cat"] = "erratic"
ORES = BELT_ORES + MOON_ORES + ERRATIC_ORES

SHIPS = {
    "venture":   5000,
    "procurer":  16000,  # TODO: base 12k + Mining Barge skill bonus; hardcoded at level IV for now
    "retriever": 22000,
    "covetor":   7000,
}

# Travel time assumptions for ISK/hr calc
SECS_PER_JUMP = 66    # align + warp + gate (timed in-game)
SECS_DOCK_UNDOCK = 60  # dock, sell, undock

PORT = 8747

# ── Region hubs for cross-region reprocess travel ──────────
# Verge Vendor has no major trade hub; Cistuvaert is the user's local area.
# Dodixie (Sinq Laison) is the nearest major hub for Verge Vendor sellers.
REGION_HUBS = {
    "verge":   "Cistuvaert",
    "dodixie": "Dodixie",
    "jita":    "Jita",
    "amarr":   "Amarr",
    "hek":     "Hek",
    "rens":    "Rens",
}
_hub_system_ids = {}


def _get_hub_system_id(region_key):
    """Resolve hub system name to ID (cached in-memory)."""
    if region_key in _hub_system_ids:
        return _hub_system_ids[region_key]
    sid = search_system_id(REGION_HUBS[region_key])
    if sid:
        _hub_system_ids[region_key] = sid
    return sid


def fetch_material_prices_cached(region_id):
    """Fetch best buy prices for all reprocess materials (file-cached 5 min)."""
    prices = {}
    for mat_id in sorted(REPRO_MATERIAL_IDS):
        price, _ = fetch_best_buy(region_id, mat_id, use_cache=True)
        prices[mat_id] = price
        time.sleep(0.05)
    return prices


def fetch_material_prices_all_regions():
    """Fetch reprocess material prices across all 6 trade regions."""
    all_prices = {}
    for rinfo in REGION_LIST:
        all_prices[rinfo["key"]] = fetch_material_prices_cached(rinfo["id"])
    return all_prices


def calc_repro_value(ore_id, material_prices, repro_efficiency):
    """Calculate ISK from reprocessing 100 units of ore at given efficiency.

    Returns total ISK or None if ore has no reprocessing formula.
    """
    if ore_id not in REPRO_VARIANTS:
        return None
    base_id, multiplier = REPRO_VARIANTS[ore_id]
    if base_id not in REPRO_FORMULAS:
        return None
    total = 0
    for mat_id, base_qty in REPRO_FORMULAS[base_id]:
        qty = int(base_qty * multiplier * repro_efficiency)
        total += qty * material_prices.get(mat_id, 0)
    return total


def calc_random_repro_range(ore_id, material_prices, repro_efficiency):
    """Calculate min/max/avg ISK from reprocessing 100 units of a random-output ore.

    Each batch randomly produces ONE mineral from the possible outcomes.
    Returns (min_isk, max_isk, avg_isk) or None.
    """
    if ore_id not in RANDOM_REPRO_RANGES:
        return None
    outcomes = []
    for mat_id, min_qty, max_qty in RANDOM_REPRO_RANGES[ore_id]:
        price = material_prices.get(mat_id, 0)
        if price <= 0:
            continue
        lo = int(min_qty * repro_efficiency) * price
        hi = int(max_qty * repro_efficiency) * price
        outcomes.append((lo, hi))
    if not outcomes:
        return None
    worst = min(o[0] for o in outcomes)
    best = max(o[1] for o in outcomes)
    avg = sum((o[0] + o[1]) / 2 for o in outcomes) / len(outcomes)
    return (worst, best, avg)


def calc_best_repro_region(ore_id, all_region_mat_prices, repro_efficiency,
                           from_system_id, hold_size, ore_vol, yield_m3_min,
                           local_region_key=None, compress_in_hold=False):
    """Find best region to sell reprocessed materials for an ore.

    For the local region (where the player mines), travel is 0 — reprocess
    in station and sell to local buy orders.  Other regions require hauling
    minerals to their trade hub, so full jump count applies.

    When compress_in_hold is True, ISK/hr scoring uses the base (uncompressed)
    hold so that travel distance matters when choosing regions.

    Handles both deterministic ores (REPRO_VARIANTS) and random-output
    erratic ores (RANDOM_REPRO_RANGES).

    Returns dict with repro_isk_m3/hold/hr/jumps/region/hub, or None.
    """
    is_random = ore_id in RANDOM_REPRO_RANGES
    if not is_random and ore_id not in REPRO_VARIANTS:
        return None

    # When compressing, only consider local region — you reprocess where you
    # mine, not haul compressed ore cross-region.
    score_hold = hold_size / COMPRESSION_RATIO if compress_in_hold else hold_size

    best = None
    best_score = -1

    for rkey, mat_prices in all_region_mat_prices.items():
        if compress_in_hold and rkey != local_region_key:
            continue
        if is_random:
            rng = calc_random_repro_range(ore_id, mat_prices, repro_efficiency)
            if rng is None:
                continue
            min_isk, max_isk, avg_isk = rng
            repro_val = avg_isk  # use average for scoring
        else:
            repro_val = calc_repro_value(ore_id, mat_prices, repro_efficiency)
            if repro_val is None or repro_val <= 0:
                continue

        repro_isk_m3 = repro_val / (100 * ore_vol)
        repro_isk_hold = repro_isk_m3 * hold_size

        repro_jumps = None
        if from_system_id is not None:
            if rkey == local_region_key:
                # Local region: reprocess in station, sell locally — no travel
                repro_jumps = 0
            else:
                hub_sid = _get_hub_system_id(rkey)
                if hub_sid:
                    repro_jumps = get_jump_count(from_system_id, hub_sid)
                    if repro_jumps < 0:
                        continue  # unreachable

        repro_isk_hr = None
        if yield_m3_min > 0:
            score_isk_hold = repro_isk_m3 * score_hold
            repro_isk_hr = calc_isk_hr(score_isk_hold, score_hold, yield_m3_min, repro_jumps)
            score = repro_isk_hr or 0
        else:
            score = repro_isk_m3

        if score > best_score:
            best_score = score
            result = {
                "repro_isk_m3": repro_isk_m3,
                "repro_isk_hold": repro_isk_hold,
                "repro_isk_hr": repro_isk_hr,
                "repro_jumps": repro_jumps,
                "repro_region": REGIONS[rkey]["name"],
                "repro_hub": REGION_HUBS[rkey],
            }
            if is_random:
                result["repro_isk_m3_min"] = min_isk / (100 * ore_vol)
                result["repro_isk_m3_max"] = max_isk / (100 * ore_vol)
            best = result

    return best


def evaluate_order_range(order, from_system_id):
    """Determine if an order can be filled locally (no travel) and effective jump distance.

    Returns (sell_local, effective_jumps, label).
      sell_local: True if the player can sell to this order from their current system.
      effective_jumps: 0 if sell_local, otherwise jumps to the order's system.
      label: human-readable range description when sell_local, e.g. "region", "5 jumps".
    """
    order_range = order.get("range", "station")
    order_system_id = order.get("system_id")

    if order_range == "region":
        return True, 0, "region"

    same_system = (order_system_id == from_system_id)

    if order_range == "solarsystem":
        if same_system:
            return True, 0, "system"
        jumps = get_jump_count(from_system_id, order_system_id)
        return False, jumps, None

    if order_range == "station":
        if same_system:
            return False, 0, None  # must dock at that specific station, but 0 jumps
        jumps = get_jump_count(from_system_id, order_system_id)
        return False, jumps, None

    # Numeric range ("1" through "40")
    try:
        range_jumps = int(order_range)
    except (ValueError, TypeError):
        jumps = 0 if same_system else get_jump_count(from_system_id, order_system_id)
        return False, jumps, None

    if same_system:
        return True, 0, f"{range_jumps} jumps"

    jumps = get_jump_count(from_system_id, order_system_id)
    if jumps >= 0 and jumps <= range_jumps:
        return True, 0, f"{range_jumps} jumps"
    return False, jumps, None


# ── Core scan logic ───────────────────────────────────────────

def _empty_result(ore, order_count=0, demand=0):
    return {**ore, "best_buy": 0, "isk_m3": 0, "isk_hold": 0, "isk_hr": None,
            "order_count": order_count, "demand": demand,
            "system_id": None, "location_id": None,
            "jumps": None, "sell_local": False, "sell_local_label": None,
            "comp_buy": 0, "comp_isk_m3": 0, "comp_isk_hold": 0,
            "comp_isk_hr": None, "comp_jumps": None, "comp_system_id": None,
            "repro_isk_m3": 0, "repro_isk_hold": 0, "repro_isk_hr": None,
            "repro_jumps": None, "repro_region": None, "repro_hub": None,
            "repro_isk_m3_min": None, "repro_isk_m3_max": None,
            "buyback_isk_m3": 0, "buyback_isk_hold": 0, "buyback_isk_hr": None,
            "buyback_type": None,
            "best_path": "raw", "best_isk_m3": 0, "best_isk_hr": None,
            "best_isk_hold": 0, "best_sell_at": None, "best_jumps": None}


def _eval_best_order(buy_orders, ore, hold_size, from_system_id, yield_m3_min):
    """Evaluate raw/compressed buy orders and return best (travel-aware if from_system)."""
    if not buy_orders:
        return None
    units_per_hold = int(hold_size / ore["vol"]) if ore["vol"] > 0 else 0
    if units_per_hold <= 0:
        return None

    if from_system_id is not None:
        viable = [o for o in buy_orders if units_per_hold >= o.get("min_volume", 1)]
        if not viable:
            return None
        viable.sort(key=lambda o: o["price"], reverse=True)
        best = None
        best_score = -1
        for order in viable[:5]:
            sell_local, eff_jumps, sl_label = evaluate_order_range(order, from_system_id)
            if eff_jumps < 0:
                continue
            isk_m3 = order["price"] / ore["vol"]
            isk_hold = order["price"] * units_per_hold
            isk_hr = calc_isk_hr(isk_hold, hold_size, yield_m3_min, eff_jumps) if yield_m3_min > 0 else None
            score = isk_hr if isk_hr else isk_m3
            if score > best_score:
                best_score = score
                best = {"price": order["price"], "isk_m3": isk_m3, "isk_hold": isk_hold,
                        "isk_hr": isk_hr, "jumps": eff_jumps,
                        "system_id": order.get("system_id"), "location_id": order.get("location_id"),
                        "sell_local": sell_local, "sell_local_label": sl_label}
        return best
    else:
        best_order = max(buy_orders, key=lambda o: o["price"])
        isk_m3 = best_order["price"] / ore["vol"]
        isk_hold = best_order["price"] * units_per_hold
        isk_hr = calc_isk_hr(isk_hold, hold_size, yield_m3_min, None) if yield_m3_min > 0 else None
        return {"price": best_order["price"], "isk_m3": isk_m3, "isk_hold": isk_hold,
                "isk_hr": isk_hr, "jumps": None,
                "system_id": best_order.get("system_id"), "location_id": best_order.get("location_id"),
                "sell_local": False, "sell_local_label": None}


def scan(region_id, hold_size, show_all=False, ore_class="0",
         from_system_id=None, yield_m3_min=0,
         repro_efficiency=0, buyback_rate=0, region_key=None,
         compress_in_hold=False):
    # Filter ores by class/category
    if ore_class == "0" or ore_class == 0:
        ores = ORES
    elif ore_class == "belt":
        ores = [o for o in ORES if o["cat"] == "belt"]
    elif ore_class == "moon":
        ores = [o for o in ORES if o["cat"] == "moon"]
    elif ore_class == "erratic":
        ores = [o for o in ORES if o["cat"] == "erratic"]
    else:
        try:
            cls_int = int(ore_class)
            ores = [o for o in ORES if o["cls"] == cls_int]
        except (ValueError, TypeError):
            ores = ORES
    results = []
    travel_aware = from_system_id is not None
    jita_region_id = REGIONS["jita"]["id"]

    # ── Pre-fetch: material prices across all regions for repro ──
    all_region_mat_prices = {}
    if repro_efficiency > 0:
        all_region_mat_prices = fetch_material_prices_all_regions()

    # ── Pre-fetch: Jita buy prices for buyback ──
    jita_raw_cache = {}   # ore type_id → best buy price
    jita_comp_cache = {}  # ore type_id → best compressed buy price
    if buyback_rate > 0:
        for ore in ores:
            p, _ = fetch_best_buy(jita_region_id, ore["id"], use_cache=True)
            jita_raw_cache[ore["id"]] = p
            time.sleep(0.03)
            if ore["id"] in COMP_IDS:
                cp, _ = fetch_best_buy(jita_region_id, COMP_IDS[ore["id"]], use_cache=True)
                jita_comp_cache[ore["id"]] = cp
                time.sleep(0.03)

    for i, ore in enumerate(ores):
        orders = fetch_buy_orders(region_id, ore["id"])
        buy_orders = [o for o in orders if o.get("is_buy_order", True)] if orders else []
        order_count = len(buy_orders)
        demand = sum(o["volume_remain"] for o in buy_orders) if buy_orders else 0

        entry = _empty_result(ore, order_count, demand)

        # ── Path 1: Raw ore (skip when compressing — you sell compressed) ──
        if not compress_in_hold:
            raw = _eval_best_order(buy_orders, ore, hold_size, from_system_id, yield_m3_min)
            if raw:
                entry["best_buy"] = raw["price"]
                entry["isk_m3"] = raw["isk_m3"]
                entry["isk_hold"] = raw["isk_hold"]
                entry["isk_hr"] = raw["isk_hr"]
                entry["system_id"] = raw["system_id"]
                entry["location_id"] = raw["location_id"]
                entry["jumps"] = raw["jumps"]
                entry["sell_local"] = raw["sell_local"]
                entry["sell_local_label"] = raw["sell_local_label"]

        # ── Path 2: Compressed ore (cross-region when compressing) ──
        if ore["id"] in COMP_IDS:
            time.sleep(0.15)
            comp_ore = {**ore, "vol": COMPRESSION_RATIO * ore["vol"]}
            if compress_in_hold:
                # Cross-region: search all hubs for best compressed buy orders
                best_comp = None
                units_per_hold = int(hold_size / comp_ore["vol"]) if comp_ore["vol"] > 0 else 0
                for rkey, rinfo in REGIONS.items():
                    comp_orders = fetch_buy_orders(rinfo["id"], COMP_IDS[ore["id"]])
                    comp_buys = [o for o in comp_orders if o.get("is_buy_order", True)] if comp_orders else []
                    if not comp_buys or units_per_hold <= 0:
                        time.sleep(0.08)
                        continue
                    if rkey == region_key:
                        # Same region: normal travel-aware eval
                        comp_eval = _eval_best_order(comp_buys, comp_ore, hold_size, from_system_id, yield_m3_min)
                    else:
                        # Other region: best price + travel to hub
                        viable = [o for o in comp_buys if units_per_hold >= o.get("min_volume", 1)]
                        best_order = max(viable, key=lambda o: o["price"], default=None) if viable else None
                        if best_order is None:
                            time.sleep(0.08)
                            continue
                        isk_m3 = best_order["price"] / comp_ore["vol"]
                        isk_hold = best_order["price"] * units_per_hold
                        jumps = None
                        if from_system_id is not None:
                            hub_sid = _get_hub_system_id(rkey)
                            if hub_sid:
                                jumps = get_jump_count(from_system_id, hub_sid)
                                if jumps < 0:
                                    time.sleep(0.08)
                                    continue
                        isk_hr = calc_isk_hr(isk_hold, hold_size, yield_m3_min, jumps) if yield_m3_min > 0 else None
                        comp_eval = {"price": best_order["price"], "isk_m3": isk_m3,
                                     "isk_hold": isk_hold, "isk_hr": isk_hr, "jumps": jumps,
                                     "system_id": best_order.get("system_id"),
                                     "location_id": best_order.get("location_id")}
                    if comp_eval is None:
                        time.sleep(0.08)
                        continue
                    score = comp_eval["isk_hr"] if comp_eval["isk_hr"] else comp_eval["isk_m3"]
                    if best_comp is None or score > best_comp[1]:
                        best_comp = (comp_eval, score)
                    time.sleep(0.08)
                comp_eval = best_comp[0] if best_comp else None
            else:
                comp_orders = fetch_buy_orders(region_id, COMP_IDS[ore["id"]])
                comp_buys = [o for o in comp_orders if o.get("is_buy_order", True)] if comp_orders else []
                comp_eval = _eval_best_order(comp_buys, comp_ore, hold_size, from_system_id, yield_m3_min)
            if comp_eval:
                entry["comp_buy"] = comp_eval["price"]
                entry["comp_isk_m3"] = comp_eval["isk_m3"]
                entry["comp_isk_hold"] = comp_eval["isk_hold"]
                entry["comp_isk_hr"] = comp_eval["isk_hr"]
                entry["comp_jumps"] = comp_eval["jumps"]
                entry["comp_system_id"] = comp_eval["system_id"]

        # ── Path 3: Reprocess (cross-region) ──
        if repro_efficiency > 0:
            repro = calc_best_repro_region(
                ore["id"], all_region_mat_prices, repro_efficiency,
                from_system_id, hold_size, ore["vol"], yield_m3_min,
                local_region_key=region_key,
                compress_in_hold=compress_in_hold)
            if repro:
                entry["repro_isk_m3"] = repro["repro_isk_m3"]
                entry["repro_isk_hold"] = repro["repro_isk_hold"]
                entry["repro_isk_hr"] = repro["repro_isk_hr"]
                entry["repro_jumps"] = repro["repro_jumps"]
                entry["repro_region"] = repro["repro_region"]
                entry["repro_hub"] = repro["repro_hub"]
                entry["repro_isk_m3_min"] = repro.get("repro_isk_m3_min")
                entry["repro_isk_m3_max"] = repro.get("repro_isk_m3_max")

        # ── Path 4: Buyback (zero travel) ──
        if buyback_rate > 0:
            rate = buyback_rate / 100.0
            raw_bb = jita_raw_cache.get(ore["id"], 0) * rate
            raw_bb_m3 = raw_bb / ore["vol"] if ore["vol"] > 0 else 0
            comp_bb_m3 = 0
            bb_type = "raw"
            if ore["id"] in COMP_IDS and ore["id"] in jita_comp_cache:
                comp_bb = jita_comp_cache[ore["id"]] * rate
                comp_bb_m3 = comp_bb / (COMPRESSION_RATIO * ore["vol"]) if ore["vol"] > 0 else 0
            if comp_bb_m3 > raw_bb_m3:
                bb_isk_m3 = comp_bb_m3
                bb_type = "compressed"
            else:
                bb_isk_m3 = raw_bb_m3
            if bb_isk_m3 > 0:
                bb_isk_hold = bb_isk_m3 * hold_size
                bb_isk_hr = calc_isk_hr(bb_isk_hold, hold_size, yield_m3_min, 0) if yield_m3_min > 0 else None
                entry["buyback_isk_m3"] = bb_isk_m3
                entry["buyback_isk_hold"] = bb_isk_hold
                entry["buyback_isk_hr"] = bb_isk_hr
                entry["buyback_type"] = bb_type

        # ── Recalculate ISK/hr with base hold when compressing ──
        # With 100× hold, mining time dwarfs travel (25,000 min vs 15 min),
        # making all destinations look equal.  Use base hold for mine_min
        # so travel distance matters proportionally to a real mining session.
        if compress_in_hold and yield_m3_min > 0:
            base_hold = hold_size / COMPRESSION_RATIO
            if entry["comp_isk_m3"] > 0:
                entry["comp_isk_hr"] = calc_isk_hr(
                    entry["comp_isk_m3"] * base_hold, base_hold,
                    yield_m3_min, entry.get("comp_jumps"))
            if entry["repro_isk_m3"] > 0:
                entry["repro_isk_hr"] = calc_isk_hr(
                    entry["repro_isk_m3"] * base_hold, base_hold,
                    yield_m3_min, entry.get("repro_jumps"))
            if entry["buyback_isk_m3"] > 0:
                entry["buyback_isk_hr"] = calc_isk_hr(
                    entry["buyback_isk_m3"] * base_hold, base_hold,
                    yield_m3_min, 0)

        # ── Determine best path ──
        paths = []
        if entry["isk_m3"] > 0:
            paths.append(("raw", entry["isk_m3"], entry["isk_hold"],
                          entry.get("isk_hr"), entry.get("jumps")))
        if entry["comp_isk_m3"] > 0:
            paths.append(("compressed", entry["comp_isk_m3"], entry["comp_isk_hold"],
                          entry.get("comp_isk_hr"), entry.get("comp_jumps")))
        if entry["repro_isk_m3"] > 0:
            paths.append(("reprocess", entry["repro_isk_m3"], entry["repro_isk_hold"],
                          entry.get("repro_isk_hr"), entry.get("repro_jumps")))
        if entry["buyback_isk_m3"] > 0:
            paths.append(("buyback", entry["buyback_isk_m3"], entry["buyback_isk_hold"],
                          entry.get("buyback_isk_hr"), 0))

        if paths:
            use_hr = yield_m3_min > 0
            if use_hr:
                paths.sort(key=lambda p: p[3] or 0, reverse=True)
            else:
                paths.sort(key=lambda p: p[1], reverse=True)
            bp = paths[0]
            entry["best_path"] = bp[0]
            entry["best_isk_m3"] = bp[1]
            entry["best_isk_hold"] = bp[2]
            entry["best_isk_hr"] = bp[3]
            entry["best_jumps"] = bp[4]

            # Resolve best sell location
            if bp[0] == "raw":
                pass  # system_id already set from raw path
            elif bp[0] == "compressed":
                entry["best_sell_at"] = None  # resolved during enrich
            elif bp[0] == "reprocess":
                entry["best_sell_at"] = entry.get("repro_hub")
            elif bp[0] == "buyback":
                entry["best_sell_at"] = "Local (buyback)"

        results.append(entry)

        if i < len(ores) - 1:
            time.sleep(0.15)

    # Sort by best ISK/hr (if yield), else best ISK/m³
    if yield_m3_min > 0:
        sort_key = lambda r: r.get("best_isk_hr") or 0
    else:
        sort_key = lambda r: r.get("best_isk_m3") or 0
    results.sort(key=sort_key, reverse=True)

    if not show_all:
        best = {}
        for r in results:
            g = r["group"]
            if g not in best or sort_key(r) > sort_key(best[g]):
                best[g] = r
        results = sorted(best.values(), key=sort_key, reverse=True)

    return results


def enrich_results(results, from_system_id=None):
    for r in results:
        # Raw ore sell location
        if r["system_id"]:
            r["system_name"] = resolve_system_name(r["system_id"])
        else:
            r["system_name"] = None
        if r["location_id"]:
            r["station_name"] = resolve_station_name(r["location_id"])
        else:
            r["station_name"] = None
        if "jumps" not in r or r["jumps"] is None:
            if from_system_id and r["system_id"]:
                r["jumps"] = get_jump_count(from_system_id, r["system_id"])

        # Compressed sell location
        comp_sid = r.get("comp_system_id")
        if comp_sid:
            r["comp_system_name"] = resolve_system_name(comp_sid)
        else:
            r["comp_system_name"] = None

        # Resolve best_sell_at for display
        bp = r.get("best_path", "raw")
        if bp == "raw":
            r["best_sell_at"] = r.get("station_name") or r.get("system_name")
        elif bp == "compressed":
            r["best_sell_at"] = r.get("comp_system_name")
        elif bp == "reprocess":
            r["best_sell_at"] = r.get("repro_hub")
        # buyback already set

    return results


def calc_isk_hr(isk_hold, hold_size, yield_m3_min, jumps):
    """Estimate ISK/hr factoring in mining time and round-trip travel."""
    if yield_m3_min <= 0 or hold_size <= 0:
        return None
    mine_min = hold_size / yield_m3_min
    if jumps is not None and jumps >= 0:
        # Round trip: jumps * 2, plus dock/sell/undock overhead
        travel_min = (jumps * 2 * SECS_PER_JUMP + SECS_DOCK_UNDOCK) / 60.0
    else:
        travel_min = SECS_DOCK_UNDOCK / 60.0  # just dock/undock, 0 jumps
    cycle_min = mine_min + travel_min
    if cycle_min <= 0:
        return None
    return (isk_hold / cycle_min) * 60


# ── Web UI ────────────────────────────────────────────────────

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EVE Mining Tools</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>&#9935;</text></svg>">
<style>
  :root {
    --bg: #0d1117;
    --surface: #161b22;
    --border: #30363d;
    --text: #e6edf3;
    --dim: #8b949e;
    --accent: #f0883e;
    --green: #3fb950;
    --blue: #58a6ff;
    --red: #f85149;
    --yellow: #d29922;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    padding: 20px;
  }
  h1 {
    font-size: 1.5em;
    margin-bottom: 4px;
    color: var(--accent);
  }
  .subtitle { color: var(--dim); font-size: 0.85em; margin-bottom: 20px; }

  .controls {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    align-items: end;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px;
    margin-bottom: 20px;
  }
  .field { display: flex; flex-direction: column; gap: 4px; }
  .field label { font-size: 0.75em; color: var(--dim); text-transform: uppercase; letter-spacing: 0.05em; }
  .field select, .field input {
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 8px 12px;
    font-size: 0.9em;
    min-width: 140px;
  }
  .field select:focus, .field input:focus { outline: none; border-color: var(--accent); }
  .field input::placeholder { color: var(--dim); }
  .field .hint { font-size: 0.7em; color: var(--dim); margin-top: 2px; }
  .field input[type=number] { min-width: 100px; }

  .cb-field {
    display: flex; align-items: center; gap: 6px;
    padding-bottom: 4px;
  }
  .cb-field input { width: 16px; height: 16px; accent-color: var(--accent); }
  .cb-field label { font-size: 0.85em; color: var(--dim); cursor: pointer; }

  button#scan-btn {
    background: var(--accent);
    color: #000;
    border: none;
    border-radius: 4px;
    padding: 8px 24px;
    font-size: 0.9em;
    font-weight: 600;
    cursor: pointer;
    transition: opacity 0.15s;
    white-space: nowrap;
  }
  button#scan-btn:hover { opacity: 0.85; }
  button#scan-btn:disabled { opacity: 0.5; cursor: not-allowed; }

  #status {
    color: var(--dim);
    font-size: 0.85em;
    margin-bottom: 12px;
    min-height: 1.2em;
  }
  #status.error { color: var(--red); }

  .results-wrap {
    overflow-x: auto;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--surface);
  }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.9em;
  }
  thead { background: rgba(240,136,62,0.08); }
  th {
    text-align: left;
    padding: 10px 14px;
    font-size: 0.75em;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--dim);
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
    cursor: pointer;
    user-select: none;
  }
  th:hover { color: var(--text); }
  th.num { text-align: right; }
  th .sort-arrow { margin-left: 4px; font-size: 0.9em; }
  td {
    padding: 8px 14px;
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
  }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  tr:last-child td { border-bottom: none; }
  tr:hover { background: rgba(255,255,255,0.03); }
  tr.best-row { background: rgba(240,136,62,0.07); }
  tr.best-row td:first-child { position: relative; }
  tr.best-row td:first-child::before {
    content: '';
    position: absolute; left: 0; top: 0; bottom: 0; width: 3px;
    background: var(--accent);
  }
  .badge {
    display: inline-block;
    background: var(--accent);
    color: #000;
    font-size: 0.7em;
    font-weight: 700;
    padding: 1px 6px;
    border-radius: 3px;
    margin-left: 6px;
    vertical-align: middle;
  }
  .no-orders { color: var(--dim); }
  .jump-badge {
    display: inline-block;
    font-size: 0.8em;
    padding: 1px 6px;
    border-radius: 3px;
    font-weight: 600;
  }
  .jump-0 { background: rgba(63,185,80,0.15); color: var(--green); }
  .jump-low { background: rgba(88,166,255,0.12); color: var(--blue); }
  .jump-mid { background: rgba(240,136,62,0.12); color: var(--accent); }
  .jump-high { background: rgba(248,81,73,0.12); color: var(--red); }

  .sell-local-yes { color: var(--green); font-weight: 600; }
  .sell-local-no { color: var(--dim); }
  .sell-local-detail { color: var(--dim); font-size: 0.8em; }

  .isk-hr { color: var(--yellow); font-weight: 600; }
  .comp-better { color: var(--green); font-weight: 600; }
  .comp-worse { color: var(--dim); }
  .repro-better { color: var(--blue); font-weight: 600; }

  .price-up { color: var(--green); font-size: 0.8em; margin-left: 4px; }
  .price-down { color: var(--red); font-size: 0.8em; margin-left: 4px; }
  .price-new { color: var(--dim); font-size: 0.7em; margin-left: 4px; }

  .demand-warn {
    display: inline-block;
    background: rgba(248,81,73,0.15);
    color: var(--red);
    font-size: 0.7em;
    font-weight: 600;
    padding: 1px 5px;
    border-radius: 3px;
    margin-left: 4px;
    cursor: help;
  }

  .auto-wrap {
    display: flex; align-items: center; gap: 8px;
    padding-bottom: 4px;
  }
  .auto-wrap select {
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 4px 6px;
    font-size: 0.8em;
  }
  .countdown {
    color: var(--dim);
    font-size: 0.8em;
    margin-left: 4px;
    font-variant-numeric: tabular-nums;
  }

  .tax-cut { color: var(--dim); font-size: 0.8em; }
  .take-home { color: var(--green); font-weight: 600; }

  .sparkline { vertical-align: middle; margin-left: 6px; cursor: pointer; }
  .sparkline:hover { opacity: 0.7; }

  .chart-overlay {
    position: fixed; inset: 0;
    background: rgba(0,0,0,0.7);
    display: flex; align-items: center; justify-content: center;
    z-index: 1000;
  }
  .chart-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 16px;
    max-width: 680px;
    width: 90vw;
    position: relative;
  }
  .chart-stats {
    display: flex; flex-wrap: wrap; gap: 16px;
    padding: 12px 8px 0;
    font-size: 0.8em; color: var(--dim);
  }
  .chart-stats strong { color: var(--text); }
  .chart-close {
    margin-left: auto;
    cursor: pointer; color: var(--dim);
  }
  .chart-close:hover { color: var(--text); }
  .chart-tooltip {
    position: absolute;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 0.8em; color: var(--text);
    pointer-events: none; z-index: 1001;
    white-space: nowrap;
    line-height: 1.4;
  }

  .summary {
    margin-top: 16px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-left: 3px solid var(--accent);
    border-radius: 8px;
    padding: 16px 20px;
  }
  .summary h3 { color: var(--accent); font-size: 1em; margin-bottom: 8px; }
  .summary .stat { margin: 4px 0; font-size: 0.9em; }
  .summary .stat strong { color: var(--text); }
  .summary .stat span { color: var(--dim); }
  .summary .isk-hr-big { color: var(--yellow); font-size: 1.1em; }

  .path-badge {
    display: inline-block;
    font-size: 0.7em;
    font-weight: 700;
    padding: 2px 6px;
    border-radius: 3px;
    text-transform: uppercase;
  }
  .path-raw { background: rgba(88,166,255,0.15); color: var(--blue); }
  .path-compressed { background: rgba(63,185,80,0.15); color: var(--green); }
  .path-reprocess { background: rgba(210,153,34,0.15); color: var(--yellow); }
  .path-buyback { background: rgba(240,136,62,0.15); color: var(--accent); }

  /* Tab bar */
  .tab-bar {
    display: flex; gap: 0; margin-bottom: 20px;
    border-bottom: 1px solid var(--border);
  }
  .tab-bar button {
    background: transparent; border: none;
    border-bottom: 2px solid transparent;
    color: var(--dim); padding: 10px 20px;
    font-size: 0.95em; cursor: pointer;
    transition: color 0.15s, border-color 0.15s;
    font-family: inherit;
  }
  .tab-bar button:hover { color: var(--text); }
  .tab-bar button.active {
    color: var(--accent); border-bottom-color: var(--accent); font-weight: 600;
  }

  /* Fitter */
  .hull-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 12px; margin: 12px 0;
  }
  .stat-card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 12px 16px;
  }
  .stat-card h3 {
    font-size: 0.75em; text-transform: uppercase;
    letter-spacing: 0.05em; color: var(--dim); margin-bottom: 8px;
  }
  .stat-line { font-size: 0.85em; margin: 3px 0; }
  .stat-line .val { color: var(--accent); font-weight: 600; }
  .stat-line .dim { color: var(--dim); }

  .fitter-section { margin-bottom: 24px; }
  .fitter-section h2 {
    color: var(--accent); font-size: 1.2em; margin-bottom: 12px;
    padding-bottom: 6px; border-bottom: 1px solid var(--border);
  }
  .fitter-section h3 { color: var(--text); font-size: 0.95em; margin: 16px 0 8px; }
  .fitter-section h4 { color: var(--dim); font-size: 0.85em; margin: 12px 0 6px; }

  .fitter-char {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 10px 16px; margin-bottom: 16px;
    font-size: 0.85em; color: var(--dim);
  }
  .fitter-char .warn { color: var(--yellow); }

  .bonus-list { font-size: 0.85em; margin: 8px 0; }
  .bonus-list strong { color: var(--text); display: block; margin-top: 8px; }
  .bonus-list ul { margin: 0; padding: 0; }
  .bonus-list li { color: var(--dim); margin: 2px 0; list-style: none; padding-left: 16px; }
  .bonus-list li::before { content: '\2022'; color: var(--accent); margin-left: -12px; margin-right: 6px; }

  .unmet { opacity: 0.5; }
  .reqs-ok { color: var(--green); }
  .reqs-fail { color: var(--red); font-size: 0.8em; }

  #fitter-btn {
    background: var(--accent); color: #000; border: none; border-radius: 4px;
    padding: 8px 24px; font-size: 0.9em; font-weight: 600;
    cursor: pointer; white-space: nowrap;
  }
  #fitter-btn:hover { opacity: 0.85; }
  #fitter-btn:disabled { opacity: 0.5; cursor: not-allowed; }

  #copy-md-btn {
    background: var(--surface); border: 1px solid var(--border);
    color: var(--text); padding: 8px 20px; border-radius: 4px;
    cursor: pointer; font-size: 0.85em; margin-top: 16px;
    font-family: inherit;
  }
  #copy-md-btn:hover { border-color: var(--accent); }

  .fitter-note {
    background: rgba(210,153,34,0.1); border: 1px solid var(--yellow);
    border-radius: 8px; padding: 12px 16px; margin-bottom: 16px;
    font-size: 0.85em; color: var(--yellow);
  }

  .hidden { display: none; }

  @keyframes spin { to { transform: rotate(360deg); } }
  .spinner {
    display: inline-block;
    width: 14px; height: 14px;
    border: 2px solid var(--border);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    vertical-align: middle;
    margin-right: 6px;
  }

  .footer {
    margin-top: 24px;
    padding-top: 12px;
    border-top: 1px solid var(--border);
    color: var(--dim);
    font-size: 0.75em;
  }
</style>
</head>
<body>

<h1>&#9935; EVE Mining Tools</h1>
<p class="subtitle">Ore profitability scanner &amp; ship fitting dossier &amp; PI planner</p>

<div class="tab-bar">
  <button class="tab active" data-tab="scanner" onclick="switchTab('scanner')">Ore Scanner</button>
  <button class="tab" data-tab="fitter" onclick="switchTab('fitter')">Ship Fitter</button>
  <button class="tab" data-tab="pi" onclick="switchTab('pi')">PI Dossier</button>
</div>

<div id="tab-scanner">
<div class="controls">
  <div class="field">
    <label>Region</label>
    <select id="region"></select>
  </div>
  <div class="field">
    <label>Ship</label>
    <select id="ship">
      <option value="venture" selected>Venture (5,000 m&sup3;)</option>
      <option value="procurer">Procurer (16,000 m&sup3;)</option>
      <option value="retriever">Retriever (22,000 m&sup3;)</option>
      <option value="covetor">Covetor (7,000 m&sup3;)</option>
      <option value="custom">Custom...</option>
    </select>
  </div>
  <div class="field hidden" id="custom-hold-wrap">
    <label>Hold size (m&sup3;)</label>
    <input type="number" id="custom-hold" placeholder="5000" min="100" step="100">
  </div>
  <div class="field">
    <label>Ore class</label>
    <select id="ore-class">
      <option value="0">All ores</option>
      <optgroup label="Belt Ores">
        <option value="belt">All belt</option>
        <option value="1" selected>Highsec (Class I)</option>
        <option value="2">Lowsec (Class II)</option>
        <option value="3">Nullsec (Class III)</option>
      </optgroup>
      <optgroup label="Moon Ores">
        <option value="moon">All moon</option>
        <option value="4">R4 &mdash; Ubiquitous</option>
        <option value="5">R8 &mdash; Common</option>
        <option value="6">R16 &mdash; Uncommon</option>
        <option value="7">R32 &mdash; Rare</option>
        <option value="8">R64 &mdash; Exceptional</option>
      </optgroup>
      <optgroup label="Erratic Ores">
        <option value="erratic">Erratic (Phased Fields)</option>
      </optgroup>
    </select>
  </div>
  <div class="field">
    <label>Your system (optional)</label>
    <input type="text" id="from-system" placeholder="e.g. Cistuvaert" spellcheck="false">
  </div>
  <div class="field">
    <label>Solo yield (m&sup3;/min)</label>
    <input type="number" id="yield-rate" placeholder="80" min="1" step="1">
    <span class="hint">Unboosted laser + drones</span>
  </div>
  <div class="cb-field">
    <input type="checkbox" id="fleet-boost">
    <label for="fleet-boost">Fleet boost</label>
  </div>
  <div class="field hidden" id="boost-wrap">
    <label>Duration bonus %</label>
    <input type="number" id="boost-pct" placeholder="46.5" min="0" max="70" step="0.1">
    <span class="hint">From Orca/Porpoise burst</span>
  </div>
  <div class="field">
    <label>Sales tax %</label>
    <input type="number" id="tax-rate" placeholder="7.5" min="0" max="15" step="0.1" value="7.5">
    <span class="hint">Accounting skill reduces this</span>
  </div>
  <div class="cb-field">
    <input type="checkbox" id="compress-hold" checked>
    <label for="compress-hold">Compress in hold</label>
  </div>
  <div class="field">
    <label>Repro yield %</label>
    <input type="number" id="repro-pct" placeholder="e.g. 78" min="0" max="100" step="0.1">
    <span class="hint">Enables reprocess path</span>
  </div>
  <div class="field">
    <label>Buyback rate %</label>
    <input type="number" id="buyback-pct" placeholder="e.g. 90" min="0" max="100" step="0.1">
    <span class="hint">Jita buy &times; rate, 0 travel</span>
  </div>
  <div class="cb-field">
    <input type="checkbox" id="show-all">
    <label for="show-all">Show all variants</label>
  </div>
  <div class="cb-field">
    <input type="checkbox" id="show-paths">
    <label for="show-paths">Show all paths</label>
  </div>
  <button id="scan-btn">Scan</button>
  <div class="auto-wrap">
    <input type="checkbox" id="auto-refresh" style="width:16px;height:16px;accent-color:var(--accent);">
    <label for="auto-refresh" style="font-size:0.85em;color:var(--dim);cursor:pointer;">Auto</label>
    <select id="auto-interval">
      <option value="3">3 min</option>
      <option value="5" selected>5 min</option>
      <option value="10">10 min</option>
    </select>
    <span class="countdown" id="countdown"></span>
  </div>
</div>

<div id="status"></div>
<div id="results" class="hidden"></div>

<div class="footer">
  Prices = highest buy order in region (instant sell). Material prices cached ~5 min server-side.<br>
  ISK/hr: 66s/jump (round trip) + 1 min dock/sell/undock. Reprocess searches all 6 regions for best hub. Buyback = Jita buy &times; rate%, 0 travel.<br>
  Best Path picks raw/compressed/reprocess/buyback by highest ISK/hr (or ISK/m&sup3; if no yield entered).
</div>
</div><!-- /tab-scanner -->

<div id="tab-fitter" class="hidden">
<div id="fitter-public-note" class="fitter-note hidden">
  Not available publicly yet.
</div>
<div class="controls">
  <div class="field">
    <label>Ship name</label>
    <input type="text" id="fitter-ship" placeholder="e.g. Retriever" spellcheck="false">
  </div>
  <div class="field" style="flex:1">
    <label>Goal (for AI context)</label>
    <input type="text" id="fitter-goal" placeholder="e.g. max ISK/hr in highsec belts" style="min-width:260px" spellcheck="false">
  </div>
  <div class="field">
    <label>Region</label>
    <select id="fitter-region"></select>
  </div>
  <div class="field">
    <label>Role</label>
    <select id="fitter-role">
      <option value="auto">Auto-detect</option>
      <option value="hauler">Hauler</option>
      <option value="mining">Mining</option>
      <option value="unset">Show all</option>
    </select>
  </div>
  <button id="fitter-btn" onclick="doFitter()">Generate Dossier</button>
</div>
<div id="fitter-status"></div>
<div id="fitter-results" class="hidden"></div>
</div><!-- /tab-fitter -->

<div id="tab-pi" class="hidden">
<div id="pi-public-note" class="fitter-note hidden">
  Not available publicly yet.
</div>
<div class="controls" style="flex-wrap:wrap;gap:12px;">
  <div class="field">
    <label>Tax rate %</label>
    <input type="number" id="pi-tax" placeholder="15" min="0" max="100" step="0.5" value="15" style="width:70px">
  </div>
  <div class="field">
    <label>Hauler m&sup3;</label>
    <input type="number" id="pi-hauler" placeholder="9000" min="100" step="100" value="9000" style="width:80px">
  </div>
  <div class="field">
    <label>Max haul min/day</label>
    <input type="number" id="pi-maxhaul" placeholder="60" min="10" step="5" value="60" style="width:70px">
  </div>
  <div class="field">
    <label>Max market jumps</label>
    <input type="number" id="pi-jumps" placeholder="5" min="0" step="1" value="5" style="width:60px">
  </div>
  <button id="pi-btn" onclick="doPi()">Generate PI Dossier</button>
</div>

<details style="margin:12px 0;">
  <summary style="cursor:pointer;color:var(--dim);font-size:0.85em;">Planet Inventory &amp; Extraction Rates (click to edit)</summary>
  <div style="display:flex;gap:24px;flex-wrap:wrap;margin-top:8px;">
    <div>
      <h4 style="color:var(--dim);margin:0 0 6px;">Planet Inventory</h4>
      <div id="pi-inventory-editor" style="font-size:0.82em;"></div>
      <button onclick="savePiInventory()" style="margin-top:6px;font-size:0.8em;">Save Inventory</button>
    </div>
    <div>
      <h4 style="color:var(--dim);margin:0 0 6px;">Extraction Rates (P0/hr per 10-head ECU)</h4>
      <div id="pi-extraction-editor" style="font-size:0.82em;"></div>
      <button onclick="savePiExtraction()" style="margin-top:6px;font-size:0.8em;">Save Rates</button>
    </div>
  </div>
</details>

<div id="pi-status"></div>
<div id="pi-results" class="hidden"></div>
</div><!-- /tab-pi -->

<script>
const REGIONS = __REGIONS_JSON__;
const SHIPS = __SHIPS_JSON__;

// Populate region dropdown
const regionSel = document.getElementById('region');
REGIONS.forEach(r => {
  const opt = document.createElement('option');
  opt.value = r.key;
  opt.textContent = r.name;
  regionSel.appendChild(opt);
});

// Ship / custom hold toggle
const shipSel = document.getElementById('ship');
const customWrap = document.getElementById('custom-hold-wrap');
const customInput = document.getElementById('custom-hold');
shipSel.addEventListener('change', () => {
  customWrap.classList.toggle('hidden', shipSel.value !== 'custom');
});

// Fleet boost toggle
const fleetCheck = document.getElementById('fleet-boost');
const boostWrap = document.getElementById('boost-wrap');
fleetCheck.addEventListener('change', () => {
  boostWrap.classList.toggle('hidden', !fleetCheck.checked);
});

function getEffectiveYield() {
  const solo = parseFloat(document.getElementById('yield-rate').value) || 0;
  if (!fleetCheck.checked || !solo) return solo;
  const boostPct = parseFloat(document.getElementById('boost-pct').value) || 0;
  if (boostPct <= 0 || boostPct >= 100) return solo;
  return solo / (1 - boostPct / 100);
}

const scanBtn = document.getElementById('scan-btn');
const statusEl = document.getElementById('status');
const resultsEl = document.getElementById('results');

scanBtn.addEventListener('click', () => { doScan(); });

// Auto-refresh state
let autoTimer = null;
let countdownTimer = null;
let nextScanAt = 0;
const autoCheck = document.getElementById('auto-refresh');
const autoInterval = document.getElementById('auto-interval');
const countdownEl = document.getElementById('countdown');

// Previous scan results for price change arrows (keyed by type_id)
let prevPrices = {};

function startAutoRefresh() {
  stopAutoRefresh();
  if (!autoCheck.checked) return;
  const mins = parseInt(autoInterval.value) || 5;
  nextScanAt = Date.now() + mins * 60000;
  autoTimer = setTimeout(() => { doScan().then(startAutoRefresh); }, mins * 60000);
  countdownTimer = setInterval(updateCountdown, 1000);
  updateCountdown();
}

function stopAutoRefresh() {
  if (autoTimer) { clearTimeout(autoTimer); autoTimer = null; }
  if (countdownTimer) { clearInterval(countdownTimer); countdownTimer = null; }
  countdownEl.textContent = '';
  nextScanAt = 0;
}

function updateCountdown() {
  const remaining = Math.max(0, Math.ceil((nextScanAt - Date.now()) / 1000));
  if (remaining <= 0) { countdownEl.textContent = ''; return; }
  const m = Math.floor(remaining / 60);
  const s = remaining % 60;
  countdownEl.textContent = m + ':' + String(s).padStart(2, '0');
}

autoCheck.addEventListener('change', () => {
  if (autoCheck.checked) startAutoRefresh();
  else stopAutoRefresh();
});
autoInterval.addEventListener('change', () => {
  if (autoCheck.checked) startAutoRefresh();
});

// Load saved settings from localStorage
try {
  const saved = JSON.parse(localStorage.getItem('oreScanner') || '{}');
  if (saved.region) regionSel.value = saved.region;
  if (saved.ship) {
    shipSel.value = saved.ship;
    if (saved.ship === 'custom') customWrap.classList.remove('hidden');
  }
  if (saved.customHold) customInput.value = saved.customHold;
  if (saved.fromSystem) document.getElementById('from-system').value = saved.fromSystem;
  if (saved.yieldRate) document.getElementById('yield-rate').value = saved.yieldRate;
  if (saved.showAll) document.getElementById('show-all').checked = true;
  if (saved.reproPct) document.getElementById('repro-pct').value = saved.reproPct;
  if (saved.compressHold !== undefined) document.getElementById('compress-hold').checked = saved.compressHold;
  if (saved.fleetBoost) { fleetCheck.checked = true; boostWrap.classList.remove('hidden'); }
  if (saved.boostPct) document.getElementById('boost-pct').value = saved.boostPct;
  if (saved.oreClass) document.getElementById('ore-class').value = saved.oreClass;
  if (saved.taxRate) document.getElementById('tax-rate').value = saved.taxRate;
  if (saved.buybackPct) document.getElementById('buyback-pct').value = saved.buybackPct;
  if (saved.showPaths) document.getElementById('show-paths').checked = true;
} catch(e) {}

// Price history: { type_id: [[timestamp, price], ...], ... }
// Max 288 points per ore (~24hrs at 5-min intervals)
let priceHistory = {};
try {
  priceHistory = JSON.parse(localStorage.getItem('orePriceHistory') || '{}');
  // Migrate old format (bare numbers) to [timestamp, price] pairs
  for (const k in priceHistory) {
    if (priceHistory[k].length > 0 && typeof priceHistory[k][0] === 'number') {
      priceHistory[k] = priceHistory[k].map(p => [null, p]);
    }
  }
} catch(e) {}

function saveSettings() {
  try {
    localStorage.setItem('oreScanner', JSON.stringify({
      region: regionSel.value,
      ship: shipSel.value,
      customHold: customInput.value,
      fromSystem: document.getElementById('from-system').value,
      yieldRate: document.getElementById('yield-rate').value,
      showAll: document.getElementById('show-all').checked,
      reproPct: document.getElementById('repro-pct').value,
      compressHold: document.getElementById('compress-hold').checked,
      fleetBoost: fleetCheck.checked,
      boostPct: document.getElementById('boost-pct').value,
      oreClass: document.getElementById('ore-class').value,
      taxRate: document.getElementById('tax-rate').value,
      buybackPct: document.getElementById('buyback-pct').value,
      showPaths: document.getElementById('show-paths').checked,
    }));
  } catch(e) {}
}

async function doScan() {
  scanBtn.disabled = true;
  statusEl.className = '';
  const clsVal = document.getElementById('ore-class').value;
  const clsLabels = {'0':'all ores','belt':'all belt','1':'Highsec','2':'Lowsec','3':'Nullsec','moon':'all moon','4':'R4','5':'R8','6':'R16','7':'R32','8':'R64','erratic':'Erratic'};
  const oreCounts = {'0':169,'belt':108,'1':32,'2':42,'3':34,'moon':60,'4':12,'5':12,'6':12,'7':12,'8':12,'erratic':1};
  const clsLabel = clsLabels[clsVal] || 'ores';
  const oreCount = oreCounts[clsVal] || 168;
  const reproPct = document.getElementById('repro-pct').value;
  const buybackPct = document.getElementById('buyback-pct').value;
  const reproExtra = reproPct ? 20 : 0;  // 6 regions x materials
  const bbExtra = buybackPct ? 10 : 0;
  const compressExtra = document.getElementById('compress-hold').checked ? Math.ceil(oreCount * 0.5) : 0;  // cross-region comp search
  const estSecs = Math.ceil(oreCount * 0.4) + reproExtra + bbExtra + compressExtra;
  statusEl.innerHTML = '<span class="spinner"></span>Scanning ' + clsLabel + ' (~' + estSecs + 's, first scan slower)';
  resultsEl.classList.add('hidden');
  resultsEl.innerHTML = '';
  saveSettings();

  const params = new URLSearchParams();
  params.set('region', regionSel.value);
  if (shipSel.value === 'custom') {
    params.set('hold', customInput.value || '5000');
  } else {
    params.set('ship', shipSel.value);
  }
  const fromSys = document.getElementById('from-system').value.trim();
  if (fromSys) params.set('from', fromSys);
  const effectiveYield = getEffectiveYield();
  if (effectiveYield > 0) params.set('yield', effectiveYield.toFixed(2));
  if (clsVal !== '0') params.set('cls', clsVal);
  if (document.getElementById('compress-hold').checked) params.set('compress', '1');
  if (document.getElementById('show-all').checked) params.set('all', '1');
  if (reproPct) params.set('repro', reproPct);
  if (buybackPct) params.set('buyback', buybackPct);

  try {
    const resp = await fetch('/api/scan?' + params.toString());
    const data = await resp.json();
    if (data.error) {
      statusEl.className = 'error';
      statusEl.textContent = data.error;
      scanBtn.disabled = false;
      return;
    }
    renderResults(data);
    // Store prices for next comparison
    const newPrices = {};
    data.results.forEach(r => { newPrices[r.type_id] = r.best_buy; });
    prevPrices = newPrices;

    // Update price history (timestamped)
    const scanTime = Date.now();
    data.results.forEach(r => {
      if (r.best_buy > 0) {
        const key = String(r.type_id);
        if (!priceHistory[key]) priceHistory[key] = [];
        priceHistory[key].push([scanTime, r.best_buy]);
        if (priceHistory[key].length > 288) priceHistory[key] = priceHistory[key].slice(-288);
      }
    });
    try { localStorage.setItem('orePriceHistory', JSON.stringify(priceHistory)); } catch(e) {}

    const now = new Date();
    const ts = now.getHours().toString().padStart(2,'0') + ':' + now.getMinutes().toString().padStart(2,'0') + ':' + now.getSeconds().toString().padStart(2,'0');
    statusEl.textContent = 'Last scan: ' + ts;

    if (autoCheck.checked) startAutoRefresh();
  } catch (e) {
    statusEl.className = 'error';
    statusEl.textContent = 'Request failed: ' + e.message;
  }
  scanBtn.disabled = false;
}

function fmtIsk(v) {
  if (v >= 1e9) return (v / 1e9).toFixed(2) + 'B';
  if (v >= 1e6) return (v / 1e6).toFixed(2) + 'M';
  if (v >= 1e3) return (v / 1e3).toFixed(1) + 'k';
  return v.toFixed(2);
}
function fmtNum(v) { return v.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2}); }

function jumpBadge(j) {
  if (j === null || j === undefined || j < 0) return '';
  let cls = 'jump-0';
  if (j > 10) cls = 'jump-high';
  else if (j > 5) cls = 'jump-mid';
  else if (j > 0) cls = 'jump-low';
  return '<span class="jump-badge ' + cls + '">' + j + 'j</span>';
}

function sparkline(typeId, oreName) {
  const key = String(typeId);
  const pts = priceHistory[key];
  if (!pts || pts.length < 2) return '';
  const prices = pts.map(p => Array.isArray(p) ? p[1] : p);
  const w = 48, h = 16;
  const min = Math.min(...prices), max = Math.max(...prices);
  const range = max - min || 1;
  const coords = prices.map((v, i) => {
    const x = (i / (prices.length - 1)) * w;
    const y = h - ((v - min) / range) * (h - 2) - 1;
    return x.toFixed(1) + ',' + y.toFixed(1);
  }).join(' ');
  const trending = prices[prices.length - 1] >= prices[0];
  const color = trending ? '#3fb950' : '#f85149';
  const changePct = ((prices[prices.length-1] - prices[0]) / prices[0] * 100).toFixed(1);
  const title = oreName + ': ' + prices[prices.length-1].toFixed(2) + ' ISK (' + (changePct > 0 ? '+' : '') + changePct + '%) \u2014 click to expand';
  const eName = oreName.replace(/'/g, "\\'");
  return '<svg class="sparkline" width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '" onclick="showChart(\'' + key + '\',\'' + eName + '\')">' +
    '<title>' + title + '</title>' +
    '<polyline fill="none" stroke="' + color + '" stroke-width="1.5" points="' + coords + '"/></svg>';
}

function showChart(typeKey, oreName) {
  var pts = priceHistory[typeKey];
  if (!pts || pts.length < 2) return;

  var prices = pts.map(function(p) { return Array.isArray(p) ? p[1] : p; });
  var times = pts.map(function(p) { return Array.isArray(p) ? p[0] : null; });
  var minP = Math.min.apply(null, prices), maxP = Math.max.apply(null, prices);
  var pRange = maxP - minP || 1;
  var curP = prices[prices.length - 1], firstP = prices[0];
  var trending = curP >= firstP;
  var color = trending ? '#3fb950' : '#f85149';
  var changePct = ((curP - firstP) / firstP * 100).toFixed(1);

  var W = 640, H = 300, ml = 65, mr = 15, mt = 36, mb = 32;
  var cw = W - ml - mr, ch = H - mt - mb;

  var chartPts = prices.map(function(p, i) {
    return {
      x: ml + (i / (prices.length - 1)) * cw,
      y: mt + ch - ((p - minP) / pRange) * ch,
      price: p, time: times[i]
    };
  });

  var fmtTime = function(ts) {
    if (!ts) return '';
    var d = new Date(ts), now = new Date();
    var hh = String(d.getHours()).padStart(2,'0');
    var mm = String(d.getMinutes()).padStart(2,'0');
    if (d.toDateString() === now.toDateString()) return hh + ':' + mm;
    var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return months[d.getMonth()] + ' ' + d.getDate() + ' ' + hh + ':' + mm;
  };

  // Build SVG
  var svg = '<svg id="chart-svg" width="' + W + '" height="' + H + '" viewBox="0 0 ' + W + ' ' + H + '" style="display:block;width:100%;height:auto;">';
  svg += '<text x="' + ml + '" y="22" fill="#e6edf3" font-size="14" font-weight="600" font-family="inherit">' + oreName + '</text>';
  svg += '<text x="' + (W - mr) + '" y="22" fill="' + color + '" font-size="13" font-weight="600" font-family="inherit" text-anchor="end">' + curP.toFixed(2) + ' ISK (' + (changePct > 0 ? '+' : '') + changePct + '%)</text>';

  // Grid + price labels
  for (var i = 0; i <= 4; i++) {
    var gy = mt + (ch * i / 4);
    var gp = maxP - (pRange * i / 4);
    svg += '<line x1="' + ml + '" y1="' + gy + '" x2="' + (W - mr) + '" y2="' + gy + '" stroke="#30363d" stroke-width="1"/>';
    svg += '<text x="' + (ml - 6) + '" y="' + (gy + 4) + '" fill="#8b949e" font-size="10" font-family="inherit" text-anchor="end">' + gp.toFixed(2) + '</text>';
  }

  // Area fill + line
  var coordStr = chartPts.map(function(p) { return p.x.toFixed(1) + ',' + p.y.toFixed(1); }).join(' ');
  svg += '<defs><linearGradient id="cg" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="' + color + '" stop-opacity="0.12"/><stop offset="100%" stop-color="' + color + '" stop-opacity="0.01"/></linearGradient></defs>';
  svg += '<polygon points="' + ml + ',' + (mt + ch) + ' ' + coordStr + ' ' + (ml + cw).toFixed(1) + ',' + (mt + ch) + '" fill="url(#cg)"/>';
  svg += '<polyline fill="none" stroke="' + color + '" stroke-width="2" points="' + coordStr + '"/>';

  // Data point dots for small datasets
  if (prices.length <= 30) {
    chartPts.forEach(function(p) {
      svg += '<circle cx="' + p.x.toFixed(1) + '" cy="' + p.y.toFixed(1) + '" r="2.5" fill="' + color + '" opacity="0.5"/>';
    });
  }

  // Time labels
  if (times[0] !== null) {
    var numLabels = Math.min(6, prices.length);
    for (var j = 0; j < numLabels; j++) {
      var idx = Math.round(j * (prices.length - 1) / (numLabels - 1));
      if (times[idx]) {
        var tx = ml + (idx / (prices.length - 1)) * cw;
        svg += '<text x="' + tx + '" y="' + (H - 6) + '" fill="#8b949e" font-size="9" font-family="inherit" text-anchor="middle">' + fmtTime(times[idx]) + '</text>';
      }
    }
  }
  svg += '</svg>';

  // Modal
  var overlay = document.createElement('div');
  overlay.className = 'chart-overlay';
  overlay.addEventListener('click', function(e) { if (e.target === overlay) overlay.remove(); });

  var card = document.createElement('div');
  card.className = 'chart-card';
  card.innerHTML = svg +
    '<div class="chart-stats">' +
    '<span>High: <strong>' + maxP.toFixed(2) + '</strong></span>' +
    '<span>Low: <strong>' + minP.toFixed(2) + '</strong></span>' +
    '<span>Scans: <strong>' + prices.length + '</strong></span>' +
    (times[0] ? '<span>Since: <strong>' + fmtTime(times[0]) + '</strong></span>' : '') +
    '<span class="chart-close" onclick="this.closest(\'.chart-overlay\').remove()">&#10005; Close</span>' +
    '</div>';

  // Hover crosshair + tooltip
  var svgEl = card.querySelector('svg');
  var ns = 'http://www.w3.org/2000/svg';
  var hoverLine = document.createElementNS(ns, 'line');
  hoverLine.setAttribute('y1', mt); hoverLine.setAttribute('y2', mt + ch);
  hoverLine.setAttribute('stroke', '#8b949e'); hoverLine.setAttribute('stroke-width', '1');
  hoverLine.setAttribute('stroke-dasharray', '3,3'); hoverLine.style.display = 'none';
  svgEl.appendChild(hoverLine);
  var hoverDot = document.createElementNS(ns, 'circle');
  hoverDot.setAttribute('r', '5'); hoverDot.setAttribute('fill', color);
  hoverDot.setAttribute('stroke', '#e6edf3'); hoverDot.setAttribute('stroke-width', '2');
  hoverDot.style.display = 'none';
  svgEl.appendChild(hoverDot);

  var tooltip = document.createElement('div');
  tooltip.className = 'chart-tooltip'; tooltip.style.display = 'none';
  card.appendChild(tooltip);

  svgEl.addEventListener('mousemove', function(e) {
    var rect = svgEl.getBoundingClientRect();
    var scaleX = W / rect.width;
    var mouseX = (e.clientX - rect.left) * scaleX;
    var nearest = 0, nearestDist = Infinity;
    chartPts.forEach(function(p, idx) {
      var d = Math.abs(p.x - mouseX);
      if (d < nearestDist) { nearestDist = d; nearest = idx; }
    });
    var pt = chartPts[nearest];
    hoverLine.setAttribute('x1', pt.x.toFixed(1));
    hoverLine.setAttribute('x2', pt.x.toFixed(1));
    hoverLine.style.display = '';
    hoverDot.setAttribute('cx', pt.x.toFixed(1));
    hoverDot.setAttribute('cy', pt.y.toFixed(1));
    hoverDot.style.display = '';
    var text = '<strong>' + pt.price.toFixed(2) + ' ISK</strong>';
    if (pt.time) text += '<br>' + fmtTime(pt.time);
    tooltip.innerHTML = text;
    tooltip.style.display = 'block';
    var cardRect = card.getBoundingClientRect();
    var tipX = e.clientX - cardRect.left + 12;
    var tipY = e.clientY - cardRect.top - 10;
    if (tipX + 130 > cardRect.width) tipX -= 145;
    tooltip.style.left = tipX + 'px';
    tooltip.style.top = tipY + 'px';
  });
  svgEl.addEventListener('mouseleave', function() {
    hoverLine.style.display = 'none';
    hoverDot.style.display = 'none';
    tooltip.style.display = 'none';
  });

  overlay.appendChild(card);
  document.body.appendChild(overlay);
  var onKey = function(e) { if (e.key === 'Escape') { overlay.remove(); document.removeEventListener('keydown', onKey); } };
  document.addEventListener('keydown', onKey);
}

// Sort state
let lastSortKey = null;
let lastSortAsc = false;
let lastData = null;

function sortResults(key) {
  if (!lastData) return;
  if (lastSortKey === key) {
    lastSortAsc = !lastSortAsc;
  } else {
    lastSortKey = key;
    lastSortAsc = false; // default descending for numbers
  }
  lastData.results.sort((a, b) => {
    let va = a[key], vb = b[key];
    if (va === null || va === undefined) va = -Infinity;
    if (vb === null || vb === undefined) vb = -Infinity;
    if (typeof va === 'string') return lastSortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
    return lastSortAsc ? va - vb : vb - va;
  });
  renderResults(lastData, true);
}

function pathBadge(p) {
  const labels = {raw:'Raw', compressed:'Comp', reprocess:'Repro', buyback:'Buyback'};
  return '<span class="path-badge path-' + p + '">' + (labels[p] || p) + '</span>';
}

function renderResults(data, isResort) {
  if (!isResort) {
    lastData = data;
    lastSortAsc = false;
  }
  const rows = data.results;
  const hasBestHr = rows.some(r => r.best_isk_hr !== null && r.best_isk_hr !== undefined && r.best_isk_hr > 0);
  const hasJumps = rows.some(r => r.best_jumps !== null && r.best_jumps !== undefined);
  const showPaths = document.getElementById('show-paths').checked;
  const hasComp = rows.some(r => r.comp_isk_m3 > 0);
  const hasRepro = rows.some(r => r.repro_isk_m3 > 0);
  const hasBuyback = rows.some(r => r.buyback_isk_m3 > 0);
  if (!isResort) {
    lastSortKey = hasBestHr ? 'best_isk_hr' : 'best_isk_m3';
  }
  const holdSize = data.hold_size;
  const taxPct = parseFloat(document.getElementById('tax-rate').value) || 0;
  const taxMul = 1 - (taxPct / 100);
  const hasHistory = Object.keys(priceHistory).length > 0;

  const cols = [
    {key: null, label: '#', num: true},
    {key: 'name', label: 'Ore', num: false},
    {key: 'best_path', label: 'Path', num: false},
    {key: 'best_isk_m3', label: 'ISK/m&sup3;', num: true},
  ];
  if (showPaths) {
    cols.push({key: 'isk_m3', label: 'Raw', num: true});
    if (hasComp) cols.push({key: 'comp_isk_m3', label: 'Comp', num: true});
    if (hasRepro) cols.push({key: 'repro_isk_m3', label: 'Repro', num: true});
    if (hasBuyback) cols.push({key: 'buyback_isk_m3', label: 'Buyback', num: true});
  }
  cols.push({key: 'best_isk_hold', label: 'Full Hold', num: true});
  if (taxPct > 0) cols.push({key: 'take_home', label: 'After tax', num: true});
  if (hasBestHr) cols.push({key: 'best_isk_hr', label: 'ISK/hr', num: true});
  cols.push({key: 'best_sell_at', label: 'Sell at', num: false});
  if (hasJumps) cols.push({key: 'best_jumps', label: 'Jumps', num: true});
  cols.push({key: 'order_count', label: 'Orders', num: true});
  cols.push({key: 'demand', label: 'Demand', num: true});
  if (hasHistory) cols.push({key: null, label: 'Trend', num: false});

  let html = '<div class="results-wrap"><table><thead><tr>';
  cols.forEach(c => {
    const arrow = lastSortKey === c.key ? (lastSortAsc ? '&#9650;' : '&#9660;') : '';
    const click = c.key ? ` onclick="sortResults('${c.key}')"` : '';
    html += '<th class="' + (c.num ? 'num' : '') + '"' + click + '>' + c.label;
    if (arrow) html += '<span class="sort-arrow">' + arrow + '</span>';
    html += '</th>';
  });
  html += '</tr></thead><tbody>';

  rows.forEach((r, i) => {
    const isBest = i === 0 && (r.best_isk_m3 > 0);
    html += '<tr class="' + (isBest ? 'best-row' : '') + '">';
    html += '<td class="num">' + (r.best_isk_m3 > 0 ? i + 1 : '--') + '</td>';
    html += '<td>' + r.name + (isBest ? '<span class="badge">BEST</span>' : '') + '</td>';

    // Path badge
    html += '<td>' + (r.best_isk_m3 > 0 ? pathBadge(r.best_path) : '--') + '</td>';

    // Best ISK/m3
    let priceArrow = '';
    if (r.best_buy > 0 && prevPrices[r.type_id] !== undefined) {
      const prev = prevPrices[r.type_id];
      if (r.best_buy > prev * 1.001) priceArrow = '<span class="price-up">&#9650;</span>';
      else if (r.best_buy < prev * 0.999) priceArrow = '<span class="price-down">&#9660;</span>';
    }
    html += '<td class="num">' + (r.best_isk_m3 > 0 ? fmtNum(r.best_isk_m3) + priceArrow : '<span class="no-orders">--</span>') + '</td>';

    // Per-path columns
    if (showPaths) {
      html += '<td class="num">' + (r.isk_m3 > 0 ? fmtNum(r.isk_m3) : '<span class="no-orders">--</span>') + '</td>';
      if (hasComp) html += '<td class="num">' + (r.comp_isk_m3 > 0 ? '<span class="comp-better">' + fmtNum(r.comp_isk_m3) + '</span>' : '<span class="no-orders">--</span>') + '</td>';
      if (hasRepro) {
        if (r.repro_isk_m3_min != null && r.repro_isk_m3_max != null) {
          html += '<td class="num"><span class="repro-better" title="Random output (avg ' + fmtNum(r.repro_isk_m3) + ')">' + fmtNum(r.repro_isk_m3_min) + ' - ' + fmtNum(r.repro_isk_m3_max) + '</span></td>';
        } else {
          html += '<td class="num">' + (r.repro_isk_m3 > 0 ? '<span class="repro-better">' + fmtNum(r.repro_isk_m3) + '</span>' : '<span class="no-orders">--</span>') + '</td>';
        }
      }
      if (hasBuyback) html += '<td class="num">' + (r.buyback_isk_m3 > 0 ? fmtNum(r.buyback_isk_m3) : '<span class="no-orders">--</span>') + '</td>';
    }

    // Full hold (from best path)
    const holdVal = r.best_isk_hold || 0;
    html += '<td class="num">' + (holdVal > 0 ? fmtIsk(holdVal) : '<span class="no-orders">--</span>') + '</td>';

    if (taxPct > 0) {
      const takeHome = holdVal > 0 ? holdVal * taxMul : 0;
      r.take_home = takeHome;
      html += '<td class="num">' + (takeHome > 0 ? '<span class="take-home">' + fmtIsk(takeHome) + '</span>' : '<span class="no-orders">--</span>') + '</td>';
    }

    if (hasBestHr) {
      const hr = r.best_isk_hr || 0;
      html += '<td class="num">' + (hr > 0 ? '<span class="isk-hr">' + fmtIsk(hr) + '</span>' : '<span class="no-orders">--</span>') + '</td>';
    }

    html += '<td>' + (r.best_sell_at || r.system_name || '--') + '</td>';
    if (hasJumps) html += '<td class="num">' + jumpBadge(r.best_jumps) + '</td>';
    html += '<td class="num">' + (r.order_count || '--') + '</td>';

    let demandWarn = '';
    if (r.demand > 0 && r.vol > 0) {
      const unitsPerHold = Math.floor(holdSize / r.vol);
      if (r.demand < unitsPerHold) {
        demandWarn = '<span class="demand-warn" title="Demand is less than 1 full hold at this price">LOW</span>';
      }
    }
    html += '<td class="num">' + (r.demand ? r.demand.toLocaleString() + demandWarn : '--') + '</td>';
    if (hasHistory) html += '<td>' + sparkline(r.type_id, r.name) + '</td>';
    html += '</tr>';
  });
  html += '</tbody></table></div>';

  // Summary card
  const top = rows.length && rows[0].best_isk_m3 > 0 ? rows[0] : null;
  if (top) {
    const sellAt = top.best_sell_at || top.station_name || top.system_name || 'Unknown';
    html += '<div class="summary">';
    html += '<h3>' + top.name + ' ' + pathBadge(top.best_path) + '</h3>';
    if (top.best_isk_hr > 0) {
      html += '<div class="stat"><span class="isk-hr-big">~' + fmtIsk(top.best_isk_hr) + ' ISK/hr</span></div>';
    }
    html += '<div class="stat"><strong>' + fmtNum(top.best_isk_m3) + ' ISK/m&sup3;</strong></div>';
    const topHold = top.best_isk_hold || 0;
    if (topHold > 0) {
      html += '<div class="stat"><span>Full hold (' + holdSize.toLocaleString() + ' m&sup3;):</span> <strong>~' + fmtIsk(topHold) + ' ISK</strong></div>';
      if (taxPct > 0) {
        const th = topHold * taxMul;
        html += '<div class="stat"><span>After ' + taxPct + '% tax:</span> <strong class="take-home">~' + fmtIsk(th) + ' ISK</strong> <span class="tax-cut">(-' + fmtIsk(topHold - th) + ')</span></div>';
      }
    }
    html += '<div class="stat"><span>Sell at:</span> <strong>' + sellAt + '</strong></div>';
    if (top.best_jumps !== null && top.best_jumps !== undefined && top.best_jumps >= 0) {
      html += '<div class="stat"><span>Distance:</span> <strong>' + top.best_jumps + ' jump' + (top.best_jumps !== 1 ? 's' : '') + '</strong></div>';
    }
    html += '<div class="stat"><span>Demand:</span> <strong>' + (top.demand ? top.demand.toLocaleString() : '?') + ' units</strong> across ' + top.order_count + ' buy orders</div>';
    html += '</div>';
  }

  resultsEl.innerHTML = html;
  resultsEl.classList.remove('hidden');
}

// ── Tab switching ──
function switchTab(tab) {
  document.querySelectorAll('.tab-bar button').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-scanner').classList.add('hidden');
  document.getElementById('tab-fitter').classList.add('hidden');
  document.getElementById('tab-pi').classList.add('hidden');
  document.getElementById('tab-' + tab).classList.remove('hidden');
  document.querySelector('.tab-bar button[data-tab="' + tab + '"]').classList.add('active');
  try { localStorage.setItem('activeTab', tab); } catch(e) {}
  if (tab === 'pi' && !piConfigLoaded) loadPiConfig();
}

// Show public note if not running locally
if (location.hostname !== 'localhost' && location.hostname !== '127.0.0.1') {
  const note = document.getElementById('fitter-public-note');
  if (note) note.classList.remove('hidden');
  const piNote = document.getElementById('pi-public-note');
  if (piNote) piNote.classList.remove('hidden');
}

// Populate fitter region dropdown
const fitterRegionSel = document.getElementById('fitter-region');
if (fitterRegionSel) {
  REGIONS.forEach(r => {
    const opt = document.createElement('option');
    opt.value = r.key;
    opt.textContent = r.name;
    fitterRegionSel.appendChild(opt);
  });
}

// Load fitter settings
try {
  const fs = JSON.parse(localStorage.getItem('fitterSettings') || '{}');
  if (fs.ship) document.getElementById('fitter-ship').value = fs.ship;
  if (fs.goal) document.getElementById('fitter-goal').value = fs.goal;
  if (fs.region) fitterRegionSel.value = fs.region;
  if (fs.role) document.getElementById('fitter-role').value = fs.role;
  const savedTab = localStorage.getItem('activeTab');
  if (savedTab === 'fitter') switchTab('fitter');
  if (savedTab === 'pi') switchTab('pi');
} catch(e) {}

// ── Fitter logic ──
let fitterMarkdown = '';

function fitterStatus(msg, isError) {
  const el = document.getElementById('fitter-status');
  el.className = isError ? 'error' : '';
  el.innerHTML = msg;
}

async function doFitter() {
  const ship = document.getElementById('fitter-ship').value.trim();
  const goal = document.getElementById('fitter-goal').value.trim();
  const region = fitterRegionSel.value;
  const role = document.getElementById('fitter-role').value;

  if (!ship) { fitterStatus('Enter a ship name.', true); return; }

  const btn = document.getElementById('fitter-btn');
  btn.disabled = true;
  fitterStatus('<span class="spinner"></span>Generating dossier for <strong>' + ship + '</strong>&hellip; this takes 30-60s, first run slower');
  document.getElementById('fitter-results').classList.add('hidden');

  try {
    localStorage.setItem('fitterSettings', JSON.stringify({ship, goal, region, role}));
  } catch(e) {}

  try {
    const params = new URLSearchParams({ship, goal, region, role});
    const resp = await fetch('/api/fitter/generate?' + params.toString());
    const data = await resp.json();
    if (data.error) {
      fitterStatus(data.error, true);
    } else {
      fitterMarkdown = data.markdown || '';
      renderFitter(data);
      const ts = new Date();
      fitterStatus('Generated at ' + ts.getHours().toString().padStart(2,'0') + ':' + ts.getMinutes().toString().padStart(2,'0'));
    }
  } catch(e) {
    fitterStatus('Request failed: ' + e.message, true);
  }
  btn.disabled = false;
}

function fmtCatVal(value, fmtType) {
  if (value === null || value === undefined || value === 0) return '--';
  if (fmtType === 'pctval') return (value > 0 ? '+' : '') + value.toFixed(1) + '%';
  if (fmtType === 'flat') return Math.abs(value) >= 1000 ? value.toLocaleString(undefined,{maximumFractionDigits:0}) : (value === Math.floor(value) ? String(Math.floor(value)) : value.toFixed(1));
  if (fmtType === 'res') return ((1 - value) * 100).toFixed(1) + '%';
  if (fmtType === 'mul') return value.toFixed(3) + 'x';
  return String(value);
}

function fmtPct(res) { return ((1 - res) * 100).toFixed(0) + '%'; }

function renderFitter(data) {
  const el = document.getElementById('fitter-results');
  const s = data.ship;
  const catCols = data.category_columns || {};
  const regionLabels = data.region_labels || [];
  let h = '';

  // Character info bar
  h += '<div class="fitter-char">';
  h += '<strong>' + data.char_info.name + '</strong> &mdash; last updated ' + data.char_info.last_updated;
  if (data.skill_warnings && data.skill_warnings.length) {
    data.skill_warnings.forEach(w => { h += '<div class="warn">WARNING: ' + w + '</div>'; });
  }
  h += '</div>';

  // Hull header
  h += '<div class="fitter-section"><h2>' + s.name + ' <span style="color:var(--dim);font-size:0.7em;font-weight:400">' + s.ship_class + ' &bull; Type ' + s.type_id + '</span></h2>';

  // Stats grid
  h += '<div class="hull-grid">';

  // Slots card
  h += '<div class="stat-card"><h3>Slot Layout</h3>';
  h += '<div class="stat-line">High: <span class="val">' + s.hi_slots + '</span> <span class="dim">(' + s.turret_hardpoints + ' turret, ' + s.launcher_hardpoints + ' launcher)</span></div>';
  h += '<div class="stat-line">Mid: <span class="val">' + s.med_slots + '</span></div>';
  h += '<div class="stat-line">Low: <span class="val">' + s.low_slots + '</span></div>';
  h += '<div class="stat-line">Rig: <span class="val">' + s.rig_slots + '</span> <span class="dim">(' + s.calibration.toFixed(0) + ' calibration)</span></div>';
  h += '</div>';

  // Fitting card
  h += '<div class="stat-card"><h3>Fitting Resources</h3>';
  h += '<div class="stat-line">CPU: <span class="val">' + s.cpu_base.toFixed(0) + '</span> / <span class="val">' + s.cpu_adj.toFixed(1) + '</span> tf</div>';
  h += '<div class="stat-line">PG: <span class="val">' + s.pg_base.toFixed(0) + '</span> / <span class="val">' + s.pg_adj.toFixed(1) + '</span> MW</div>';
  h += '<div class="stat-line">Drone bay: <span class="val">' + s.drone_bay.toFixed(0) + '</span> m\u00b3</div>';
  h += '<div class="stat-line">Drone BW: <span class="val">' + s.drone_bw.toFixed(0) + '</span> Mbit/s</div>';
  h += '</div>';

  // Capacity card
  h += '<div class="stat-card"><h3>Capacity</h3>';
  if (s.cargo_skill_name) {
    h += '<div class="stat-line">Cargo: <span class="val">' + s.cargo.toLocaleString() + '</span> / <span class="val">' + s.cargo_adj.toLocaleString() + '</span> m\u00b3 <span class="dim">(' + s.cargo_skill_name + ' ' + s.cargo_skill_level + ')</span></div>';
  } else {
    h += '<div class="stat-line">Cargo: <span class="val">' + s.cargo.toLocaleString() + '</span> m\u00b3</div>';
  }
  if (s.ore_hold_base > 0) {
    h += '<div class="stat-line">Mining hold: <span class="val">' + s.ore_hold_base.toLocaleString() + '</span> / <span class="val">' + s.ore_hold_adj.toLocaleString() + '</span> m\u00b3</div>';
  }
  if (s.fleet_hangar > 0) h += '<div class="stat-line">Fleet hangar: <span class="val">' + s.fleet_hangar.toLocaleString() + '</span> m\u00b3</div>';
  if (s.fuel_bay > 0) h += '<div class="stat-line">Fuel bay: <span class="val">' + s.fuel_bay.toLocaleString() + '</span> m\u00b3</div>';
  if (data.gank_cost_per_ehp && s.total_ehp_kt) {
    const gankThreshold = s.total_ehp_kt * data.gank_cost_per_ehp;
    h += '<div class="stat-line">Gank threshold: <span class="val">' + fmtIsk(gankThreshold) + '</span> <span class="dim">(EHP_kt \u00d7 ' + data.gank_cost_per_ehp.toLocaleString() + ')</span></div>';
  }
  h += '</div>';

  // Navigation card
  h += '<div class="stat-card"><h3>Navigation</h3>';
  h += '<div class="stat-line">Mass: <span class="val">' + s.mass.toLocaleString() + '</span> kg</div>';
  h += '<div class="stat-line">Inertia: <span class="val">' + s.agility.toFixed(4) + '</span>';
  if (s.agility_adj) h += ' / <span class="val">' + s.agility_adj.toFixed(4) + '</span> skilled';
  h += '</div>';
  h += '<div class="stat-line">Align: <span class="val">' + s.align_time.toFixed(1) + 's</span>';
  if (s.align_time_adj) h += ' / <span class="val">' + s.align_time_adj.toFixed(1) + 's</span> skilled';
  h += '</div>';
  h += '<div class="stat-line">Velocity: <span class="val">' + s.max_vel.toFixed(0) + '</span> m/s</div>';
  h += '<div class="stat-line">Warp: <span class="val">' + s.warp_speed.toFixed(1) + '</span> AU/s</div>';
  h += '<div class="stat-line">Sig: <span class="val">' + s.sig_radius.toFixed(0) + '</span> m</div>';
  h += '</div>';

  h += '</div>'; // hull-grid

  // Defence
  h += '<h3>Defence</h3>';
  const sr = s.shield_res, ar = s.armor_res;
  h += '<div class="stat-line">Shield: <span class="val">' + s.shield_hp.toLocaleString() + ' HP</span> <span class="dim">(EM ' + fmtPct(sr.EM) + ' / Th ' + fmtPct(sr.Therm) + ' / Kin ' + fmtPct(sr.Kin) + ' / Ex ' + fmtPct(sr.Exp) + ')</span></div>';
  h += '<div class="stat-line">Armor: <span class="val">' + s.armor_hp.toLocaleString() + ' HP</span> <span class="dim">(EM ' + fmtPct(ar.EM) + ' / Th ' + fmtPct(ar.Therm) + ' / Kin ' + fmtPct(ar.Kin) + ' / Ex ' + fmtPct(ar.Exp) + ')</span></div>';
  h += '<div class="stat-line">Structure: <span class="val">' + s.structure_hp.toLocaleString() + ' HP</span></div>';
  if (s.total_ehp) h += '<div class="stat-line"><strong>EHP (omni):</strong> <span class="val">' + Math.round(s.total_ehp).toLocaleString() + '</span></div>';
  if (s.total_ehp_kt) h += '<div class="stat-line"><strong>EHP (Kin/Therm):</strong> <span class="val">' + Math.round(s.total_ehp_kt).toLocaleString() + '</span></div>';

  // Hull bonuses
  if (s.structured_bonuses && s.structured_bonuses.length) {
    h += '<h3>Hull Bonuses</h3><div class="bonus-list">';
    s.structured_bonuses.forEach(([section, bonuses]) => {
      h += '<strong>' + section + '</strong><ul>';
      bonuses.forEach(b => { h += '<li>' + b + '</li>'; });
      h += '</ul>';
    });
    h += '</div>';
  }

  // Hull prices
  h += '<h3>Hull Cost</h3>';
  h += '<div class="results-wrap"><table><thead><tr><th>Source</th><th class="num">Best Buy</th><th class="num">Best Sell</th></tr></thead><tbody>';
  regionLabels.forEach(label => {
    const p = data.hull_prices[label] || {};
    const buy = p.buy || 0, sell = p.sell || 0;
    h += '<tr><td>' + label + '</td><td class="num">' + fmtIsk(buy) + '</td><td class="num">' + fmtIsk(sell) + '</td></tr>';
  });
  h += '</tbody></table></div>';

  h += '</div>'; // fitter-section (hull)

  // ── Module candidates ──
  const slotLabels = {
    high: 'High Slots (' + s.hi_slots + ' slots, ' + s.turret_hardpoints + ' turret)',
    mid: 'Mid Slots (' + s.med_slots + ' slots)',
    low: 'Low Slots (' + s.low_slots + ' slots)',
    rig: 'Rig Slots (' + s.rig_slots + ' slots, ' + s.calibration.toFixed(0) + ' cal)',
  };

  const roleHidden = data.role_hidden || {};
  h += '<div class="fitter-section"><h2>Module Candidates</h2>';
  ['high','mid','low','rig'].forEach(slotType => {
    let cats = data.candidates[slotType] || [];
    if (!cats.length) return;
    // Role-based filtering (render-time only, data preserved)
    const hidden = roleHidden[slotType] || [];
    if (hidden.length) cats = cats.filter(c => !hidden.includes(c.name));
    if (!cats.length) return;
    h += '<h3>' + slotLabels[slotType] + '</h3>';
    cats.forEach(cat => {
      h += '<h4>' + cat.name + ' <span class="dim">(' + cat.candidates.length + ')</span></h4>';
      if (!cat.candidates.length) { h += '<p class="dim">No candidates.</p>'; return; }
      h += renderModTable(cat, slotType, catCols, regionLabels);
    });
  });
  h += '</div>';

  // ── Drones (skip when no drone bay) ──
  if (s.drone_bay > 0 || s.drone_bw > 0) {
  h += '<div class="fitter-section"><h2>Drones</h2>';
  h += '<div class="stat-line">Bay: <span class="val">' + s.drone_bay.toFixed(0) + '</span> m\u00b3 &bull; BW: <span class="val">' + s.drone_bw.toFixed(0) + '</span> Mbit/s</div>';
  (data.drones || []).forEach(cat => {
    h += '<h4>' + cat.name + ' <span class="dim">(' + cat.candidates.length + ')</span></h4>';
    if (!cat.candidates.length) { h += '<p class="dim">No candidates.</p>'; return; }
    h += renderDroneTable(cat, regionLabels);
  });
  h += '</div>';
  } // end drone bay check

  // Copy markdown button
  h += '<button id="copy-md-btn" onclick="copyDossier()">Copy Markdown to Clipboard</button>';

  el.innerHTML = h;
  el.classList.remove('hidden');
}

function renderModTable(cat, slotType, catCols, regionLabels) {
  const cands = cat.candidates;
  const hasYield = cands.some(c => c.yield_per_cycle);
  const isRig = slotType === 'rig';
  const hasDrawback = cands.some(c => c.drawbacks && c.drawbacks.length);
  const cols = catCols[cat.name] || [];

  let headers = ['Module','Var'];
  if (hasYield) headers.push('Yield','Cycle');
  cols.forEach(c => headers.push(c[0]));
  if (hasDrawback) headers.push('Drawback');
  headers.push('CPU','PG');
  if (isRig) headers.push('Cal');
  headers.push('Reqs');
  regionLabels.forEach(l => headers.push('Sell ' + l.split(' ')[0]));

  let t = '<div class="results-wrap"><table><thead><tr>';
  headers.forEach(hdr => { t += '<th>' + hdr + '</th>'; });
  t += '</tr></thead><tbody>';

  cands.forEach(c => {
    t += '<tr class="' + (c.reqs_met ? '' : 'unmet') + '">';
    t += '<td>' + c.name + '</td>';
    t += '<td>' + c.variant + '</td>';
    if (hasYield) {
      t += '<td class="num">' + (c.yield_per_cycle ? c.yield_per_cycle.toFixed(0) + ' m\u00b3' : '--') + '</td>';
      t += '<td class="num">' + (c.cycle_time ? c.cycle_time.toFixed(0) + 's' : '--') + '</td>';
    }
    cols.forEach(([_, attrId, fmt]) => {
      t += '<td class="num">' + fmtCatVal((c.dogma||{})[attrId], fmt) + '</td>';
    });
    if (hasDrawback) {
      const dbs = c.drawbacks || [];
      t += '<td>' + (dbs.length ? dbs.join('; ') : '\u2014') + '</td>';
    }
    t += '<td class="num">' + c.cpu.toFixed(0) + '</td>';
    t += '<td class="num">' + c.pg.toFixed(0) + '</td>';
    if (isRig) t += '<td class="num">' + (c.calibration||0).toFixed(0) + '</td>';
    t += '<td>' + (c.reqs_met ? '<span class="reqs-ok">\u2713</span>' : '<span class="reqs-fail">\u2717 ' + (c.missing_skills||[]).join(', ') + '</span>') + '</td>';
    regionLabels.forEach(l => {
      t += '<td class="num">' + fmtIsk((c.prices||{})['sell_' + l] || 0) + '</td>';
    });
    t += '</tr>';
  });
  t += '</tbody></table></div>';
  return t;
}

function renderDroneTable(cat, regionLabels) {
  const cands = cat.candidates;
  const hasYield = cands.some(c => c.yield_per_cycle);
  let headers = ['Drone','Var'];
  if (hasYield) headers.push('Yield','Cycle');
  headers.push('Vol','BW','Reqs');
  regionLabels.forEach(l => headers.push('Sell ' + l.split(' ')[0]));

  let t = '<div class="results-wrap"><table><thead><tr>';
  headers.forEach(hdr => { t += '<th>' + hdr + '</th>'; });
  t += '</tr></thead><tbody>';

  cands.forEach(c => {
    t += '<tr class="' + (c.reqs_met ? '' : 'unmet') + '">';
    t += '<td>' + c.name + '</td>';
    t += '<td>' + c.variant + '</td>';
    if (hasYield) {
      t += '<td class="num">' + (c.yield_per_cycle ? c.yield_per_cycle.toFixed(0) + ' m\u00b3' : '--') + '</td>';
      t += '<td class="num">' + (c.cycle_time ? c.cycle_time.toFixed(0) + 's' : '--') + '</td>';
    }
    t += '<td class="num">' + c.volume.toFixed(0) + ' m\u00b3</td>';
    t += '<td class="num">' + c.bandwidth.toFixed(0) + '</td>';
    t += '<td>' + (c.reqs_met ? '<span class="reqs-ok">\u2713</span>' : '<span class="reqs-fail">\u2717 ' + (c.missing_skills||[]).join(', ') + '</span>') + '</td>';
    regionLabels.forEach(l => {
      t += '<td class="num">' + fmtIsk((c.prices||{})['sell_' + l] || 0) + '</td>';
    });
    t += '</tr>';
  });
  t += '</tbody></table></div>';
  return t;
}

function copyDossier() {
  if (!fitterMarkdown) return;
  navigator.clipboard.writeText(fitterMarkdown).then(() => {
    const btn = document.getElementById('copy-md-btn');
    btn.textContent = 'Copied!';
    setTimeout(() => { btn.textContent = 'Copy Markdown to Clipboard'; }, 2000);
  });
}

// ── PI Dossier logic ──
let piConfigLoaded = false;
let piMarkdown = '';

function piStatus(msg, isError) {
  const el = document.getElementById('pi-status');
  el.className = isError ? 'error' : '';
  el.innerHTML = msg;
}

async function loadPiConfig() {
  try {
    const resp = await fetch('/api/pi/config');
    const data = await resp.json();
    if (data.planet_inventory) renderPiInventoryEditor(data.planet_inventory);
    if (data.extraction_rates) renderPiExtractionEditor(data.extraction_rates);
    if (data.config) {
      if (data.config.tax_rate) document.getElementById('pi-tax').value = (data.config.tax_rate * 100).toFixed(0);
      if (data.config.hauler_m3) document.getElementById('pi-hauler').value = data.config.hauler_m3;
      if (data.config.max_haul_minutes) document.getElementById('pi-maxhaul').value = data.config.max_haul_minutes;
      if (data.config.max_market_jumps) document.getElementById('pi-jumps').value = data.config.max_market_jumps;
    }
    piConfigLoaded = true;
  } catch(e) { piStatus('Failed to load config: ' + e.message, true); }
}

const PI_PLANET_TYPES = ['Barren','Gas','Ice','Lava','Oceanic','Plasma','Storm','Temperate'];

function renderPiInventoryEditor(inv) {
  const el = document.getElementById('pi-inventory-editor');
  let h = '<table style="border-collapse:collapse;"><thead><tr><th style="padding:2px 6px;">System</th>';
  PI_PLANET_TYPES.forEach(t => { h += '<th style="padding:2px 4px;font-size:0.8em;">' + t + '</th>'; });
  h += '<th></th></tr></thead><tbody>';
  for (const [sys, planets] of Object.entries(inv)) {
    h += '<tr><td style="padding:2px 6px;"><input type="text" class="pi-inv-sys" value="' + sys + '" style="width:80px;font-size:0.8em;"></td>';
    PI_PLANET_TYPES.forEach(t => {
      const v = planets[t] || 0;
      h += '<td style="padding:2px 2px;"><input type="number" class="pi-inv-val" data-type="' + t + '" value="' + v + '" min="0" max="20" style="width:32px;font-size:0.8em;text-align:center;"></td>';
    });
    h += '<td><button onclick="this.closest(\'tr\').remove()" style="font-size:0.7em;">X</button></td></tr>';
  }
  h += '</tbody></table>';
  h += '<button onclick="addPiInvRow()" style="font-size:0.75em;margin-top:4px;">+ Add System</button>';
  el.innerHTML = h;
}

function addPiInvRow() {
  const tbody = document.querySelector('#pi-inventory-editor tbody');
  let h = '<tr><td style="padding:2px 6px;"><input type="text" class="pi-inv-sys" value="" style="width:80px;font-size:0.8em;" placeholder="System"></td>';
  PI_PLANET_TYPES.forEach(t => {
    h += '<td style="padding:2px 2px;"><input type="number" class="pi-inv-val" data-type="' + t + '" value="0" min="0" max="20" style="width:32px;font-size:0.8em;text-align:center;"></td>';
  });
  h += '<td><button onclick="this.closest(\'tr\').remove()" style="font-size:0.7em;">X</button></td></tr>';
  tbody.insertAdjacentHTML('beforeend', h);
}

function renderPiExtractionEditor(rates) {
  const el = document.getElementById('pi-extraction-editor');
  let h = '<table style="border-collapse:collapse;"><thead><tr><th style="padding:2px 6px;">System</th>';
  PI_PLANET_TYPES.forEach(t => { h += '<th style="padding:2px 4px;font-size:0.8em;">' + t + '</th>'; });
  h += '</tr></thead><tbody>';
  // Show all systems from inventory
  const invRows = document.querySelectorAll('#pi-inventory-editor .pi-inv-sys');
  const systems = new Set();
  invRows.forEach(inp => { if(inp.value.trim()) systems.add(inp.value.trim()); });
  for (const [sys] of Object.entries(rates)) systems.add(sys);
  for (const sys of systems) {
    const sysRates = rates[sys] || {};
    h += '<tr><td style="padding:2px 6px;font-size:0.8em;">' + sys + '</td>';
    PI_PLANET_TYPES.forEach(t => {
      const v = sysRates[t] || '';
      h += '<td style="padding:2px 2px;"><input type="number" class="pi-ext-val" data-sys="' + sys + '" data-type="' + t + '" value="' + v + '" placeholder="8000" min="0" step="1000" style="width:50px;font-size:0.8em;text-align:center;"></td>';
    });
    h += '</tr>';
  }
  h += '</tbody></table>';
  h += '<p style="font-size:0.7em;color:var(--dim);margin:4px 0;">Leave blank for default (8000). Enter observed values from in-game.</p>';
  el.innerHTML = h;
}

function collectPiInventory() {
  const rows = document.querySelectorAll('#pi-inventory-editor tbody tr');
  const inv = {};
  rows.forEach(row => {
    const sys = row.querySelector('.pi-inv-sys').value.trim();
    if (!sys) return;
    inv[sys] = {};
    row.querySelectorAll('.pi-inv-val').forEach(inp => {
      const v = parseInt(inp.value) || 0;
      if (v > 0) inv[sys][inp.dataset.type] = v;
    });
  });
  return inv;
}

function collectPiExtraction() {
  const rates = {};
  document.querySelectorAll('.pi-ext-val').forEach(inp => {
    const sys = inp.dataset.sys;
    const v = parseInt(inp.value) || 0;
    if (v > 0) {
      if (!rates[sys]) rates[sys] = {};
      rates[sys][inp.dataset.type] = v;
    }
  });
  return rates;
}

async function savePiInventory() {
  const inv = collectPiInventory();
  // Capture current extraction rates before re-render
  const currentRates = collectPiExtraction();
  try {
    const resp = await fetch('/api/pi/save-inventory', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(inv)});
    const r = await resp.json();
    if (r.ok) {
      piStatus('Inventory saved.');
      // Re-render extraction editor so new systems appear
      renderPiExtractionEditor(currentRates);
    } else piStatus(r.error||'Save failed',true);
  } catch(e) { piStatus('Save failed: '+e.message,true); }
}

async function savePiExtraction() {
  const rates = collectPiExtraction();
  try {
    const resp = await fetch('/api/pi/save-extraction', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(rates)});
    const r = await resp.json();
    if (r.ok) piStatus('Extraction rates saved.'); else piStatus(r.error||'Save failed',true);
  } catch(e) { piStatus('Save failed: '+e.message,true); }
}

async function doPi() {
  const btn = document.getElementById('pi-btn');
  btn.disabled = true;
  piStatus('<span class="spinner"></span>Generating PI dossier&hellip; this takes 30-60s, first run slower');
  document.getElementById('pi-results').classList.add('hidden');

  const tax = parseFloat(document.getElementById('pi-tax').value) / 100;
  const hauler = parseFloat(document.getElementById('pi-hauler').value);
  const maxHaul = parseFloat(document.getElementById('pi-maxhaul').value);
  const maxJumps = parseInt(document.getElementById('pi-jumps').value);

  const params = new URLSearchParams({tax, hauler_m3: hauler, max_haul_minutes: maxHaul, max_market_jumps: maxJumps});
  try {
    const resp = await fetch('/api/pi/generate?' + params.toString());
    const data = await resp.json();
    if (data.error) {
      piStatus(data.error, true);
    } else {
      piMarkdown = data.markdown || '';
      renderPi(data);
      const ts = new Date();
      piStatus('Generated at ' + ts.getHours().toString().padStart(2,'0') + ':' + ts.getMinutes().toString().padStart(2,'0'));
    }
  } catch(e) {
    piStatus('Request failed: ' + e.message, true);
  }
  btn.disabled = false;
}

function renderPi(data) {
  const el = document.getElementById('pi-results');
  let h = '';

  // Character info
  h += '<div class="fitter-char"><strong>' + data.char_info.name + '</strong> &mdash; CCU ' + data.pi_skills.ccu + ', IC ' + data.pi_skills.ic + ', Planetology ' + data.pi_skills.planetology + '</div>';

  // Recommended layout
  if (data.allocated && data.allocated.length) {
    h += '<div class="fitter-section"><h2>Recommended Layout</h2>';
    let totalNet = 0;
    data.allocated.forEach(a => { totalNet += a.net_isk_hr || 0; });
    h += '<p><strong>Total:</strong> ' + fmtIsk(totalNet) + '/hr net</p>';
    h += '<div class="results-wrap"><table><thead><tr><th>#</th><th>System</th><th>Type</th><th>Role</th><th>Product</th><th class="num">Net ISK/hr</th></tr></thead><tbody>';
    let slot = 0;
    data.allocated.forEach(a => {
      if (a.planets_used && a.planets_used.length) {
        a.planets_used.forEach(p => {
          slot++;
          h += '<tr><td>' + slot + '</td><td>' + p.system + '</td><td>' + p.type + '</td><td>' + p.role + '</td><td>' + a.output_name + '</td><td class="num">' + fmtIsk(a.net_isk_hr) + '</td></tr>';
        });
      } else {
        slot++;
        h += '<tr><td>' + slot + '</td><td>--</td><td>--</td><td>' + a.layout_type + '</td><td>' + a.output_name + '</td><td class="num">' + fmtIsk(a.net_isk_hr) + '</td></tr>';
      }
    });
    h += '</tbody></table></div></div>';
  }

  // All chains by tier
  ['P1','P2','P3'].forEach(tier => {
    const chains = (data.chains||[]).filter(c => c.tier === tier);
    if (!chains.length) return;
    const tierLabel = {P1:'P1 (Self-contained)',P2:'P2 (Refined)',P3:'P3 (Specialized)'}[tier]||tier;
    h += '<div class="fitter-section"><h2>' + tierLabel + '</h2>';
    h += '<div class="results-wrap"><table><thead><tr><th>#</th><th>Product</th><th>Setup</th><th class="num">Units/hr</th><th class="num">Buy (range)</th><th>Buyer</th><th class="num">VWAP</th><th class="num">Net ISK/hr</th><th class="num">Haul</th><th>Flags</th></tr></thead><tbody>';
    chains.forEach((c, i) => {
      const setup = c.layout_type === 'p1_extractor' ? '1 planet' : c.layout_type === 'p2_selfcontained' ? '1 planet (self)' : c.layout_type === 'p2_factory' ? c.planet_count + 'p (factory)' : c.planet_count + ' planets';
      const flags = (c.flags && c.flags.length) ? c.flags.join(', ') : '--';
      const rowCls = c.viable ? '' : ' style="opacity:0.5"';
      const buyer = c.local_buyer_system ? c.local_buyer_system + ' (' + (c.local_buyer_jumps||0) + 'j)' : '--';
      h += '<tr' + rowCls + '><td>' + (i+1) + '</td><td>' + c.output_name + '</td><td>' + setup + '</td><td class="num">' + c.units_hr + '</td><td class="num">' + fmtIsk(c.local_buy_price) + '</td><td>' + buyer + '</td><td class="num">' + fmtIsk(c.local_vwap||0) + '</td><td class="num">' + fmtIsk(c.net_isk_hr) + '</td><td class="num">' + c.haul_minutes_per_day.toFixed(0) + 'm</td><td>' + flags + '</td></tr>';
    });
    h += '</tbody></table></div></div>';
  });

  // Projections
  if (data.projections && data.projections.length) {
    h += '<div class="fitter-section"><h2>Skill Projections</h2><ul>';
    data.projections.forEach(p => { h += '<li><strong>' + p.skill + '</strong>: ' + p.effect + '<br><span style="color:var(--dim);font-size:0.85em;">' + p.detail + '</span></li>'; });
    h += '</ul></div>';
  }

  // Copy markdown button
  h += '<button id="copy-pi-md-btn" onclick="copyPiMarkdown()" style="margin-top:12px;">Copy Markdown to Clipboard</button>';

  el.innerHTML = h;
  el.classList.remove('hidden');
}

function copyPiMarkdown() {
  if (!piMarkdown) return;
  navigator.clipboard.writeText(piMarkdown).then(() => {
    const btn = document.getElementById('copy-pi-md-btn');
    btn.textContent = 'Copied!';
    setTimeout(() => { btn.textContent = 'Copy Markdown to Clipboard'; }, 2000);
  });
}
</script>
</body>
</html>"""


class ScanHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        print(f"  {args[0]}")

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/" or parsed.path == "":
            page = HTML_PAGE.replace(
                "__REGIONS_JSON__", json.dumps(REGION_LIST)
            ).replace(
                "__SHIPS_JSON__", json.dumps(SHIPS)
            )
            self._send_html(page)
            return

        if parsed.path == "/api/scan":
            self._handle_scan(parse_qs(parsed.query))
            return

        if parsed.path == "/api/fitter/generate":
            self._handle_fitter(parse_qs(parsed.query))
            return

        if parsed.path == "/api/pi/config":
            self._handle_pi_config()
            return

        if parsed.path == "/api/pi/generate":
            self._handle_pi_generate(parse_qs(parsed.query))
            return

        self.send_error(404)

    def _handle_fitter(self, qs):
        import fit_dossier
        ship_name = qs.get("ship", [None])[0]
        if not ship_name or not ship_name.strip():
            self._send_json({"error": "Missing 'ship' parameter."}, 400)
            return
        goal = qs.get("goal", [""])[0]
        region_key = qs.get("region", ["verge"])[0]
        role = qs.get("role", ["auto"])[0]
        if region_key not in REGIONS:
            self._send_json({"error": f"Unknown region: {region_key}"}, 400)
            return

        print(f"  Fitter: generating dossier for {ship_name.strip()} (role={role})...")
        try:
            data = fit_dossier.generate_dossier_data(
                ship_name.strip(), goal=goal, region_key=region_key, role=role,
            )
        except Exception as e:
            self._send_json({"error": f"Dossier generation failed: {e}"}, 500)
            return
        print(f"  Fitter: done.")
        self._send_json(data)

    def _handle_pi_config(self):
        import pi_dossier
        try:
            cfg = pi_dossier.load_pi_config()
            inv = pi_dossier.load_planet_inventory()
            rates = pi_dossier.load_extraction_rates()
            self._send_json({
                "config": cfg,
                "planet_inventory": inv,
                "extraction_rates": rates,
            })
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_pi_generate(self, qs):
        import pi_dossier
        overrides = {}
        if "tax" in qs:
            try: overrides["tax_rate"] = float(qs["tax"][0])
            except ValueError: pass
        if "hauler_m3" in qs:
            try: overrides["hauler_m3"] = float(qs["hauler_m3"][0])
            except ValueError: pass
        if "max_haul_minutes" in qs:
            try: overrides["max_haul_minutes"] = float(qs["max_haul_minutes"][0])
            except ValueError: pass
        if "max_market_jumps" in qs:
            try: overrides["max_market_jumps"] = int(qs["max_market_jumps"][0])
            except ValueError: pass

        print(f"  PI: generating dossier...")
        try:
            data = pi_dossier.generate_pi_dossier_data(
                overrides=overrides if overrides else None)
        except Exception as e:
            self._send_json({"error": f"PI dossier generation failed: {e}"}, 500)
            return
        print(f"  PI: done.")
        self._send_json(data)

    def do_POST(self):
        parsed = urlparse(self.path)
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len) if content_len > 0 else b""

        if parsed.path == "/api/pi/save-inventory":
            self._handle_pi_save_inventory(body)
            return

        if parsed.path == "/api/pi/save-extraction":
            self._handle_pi_save_extraction(body)
            return

        self.send_error(404)

    def _handle_pi_save_inventory(self, body):
        import pi_dossier
        try:
            data = json.loads(body.decode())
            pi_dossier.save_planet_inventory(data)
            self._send_json({"ok": True})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_pi_save_extraction(self, body):
        import pi_dossier
        try:
            data = json.loads(body.decode())
            pi_dossier.save_extraction_rates(data)
            self._send_json({"ok": True})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_scan(self, qs):
        region_key = qs.get("region", ["verge"])[0]
        if region_key not in REGIONS:
            self._send_json({"error": f"Unknown region: {region_key}"}, 400)
            return

        region = REGIONS[region_key]

        if "hold" in qs:
            try:
                hold_size = int(qs["hold"][0])
            except ValueError:
                hold_size = 5000
        elif "ship" in qs:
            ship_key = qs["ship"][0]
            hold_size = SHIPS.get(ship_key, 5000)
        else:
            hold_size = 5000

        compress_in_hold = "compress" in qs
        if compress_in_hold:
            hold_size = hold_size * COMPRESSION_RATIO

        show_all = "all" in qs

        ore_class = "0"
        if "cls" in qs:
            ore_class = qs["cls"][0]

        repro_efficiency = 0
        if "repro" in qs:
            try:
                repro_efficiency = float(qs["repro"][0]) / 100.0
            except ValueError:
                repro_efficiency = 0

        buyback_rate = 0
        if "buyback" in qs:
            try:
                buyback_rate = float(qs["buyback"][0])
            except ValueError:
                buyback_rate = 0

        yield_m3_min = 0
        if "yield" in qs:
            try:
                yield_m3_min = float(qs["yield"][0])
            except ValueError:
                yield_m3_min = 0

        from_system_id = None
        from_name = qs.get("from", [None])[0]
        if from_name and from_name.strip():
            from_system_id = search_system_id(from_name.strip())
            if from_system_id is None:
                self._send_json({
                    "error": f"System '{from_name}' not found. Check spelling (exact name, e.g. 'Cistuvaert')."
                }, 400)
                return

        results = scan(region["id"], hold_size, show_all=show_all, ore_class=ore_class,
                       from_system_id=from_system_id, yield_m3_min=yield_m3_min,
                       repro_efficiency=repro_efficiency,
                       buyback_rate=buyback_rate, region_key=region_key,
                       compress_in_hold=compress_in_hold)
        results = enrich_results(results, from_system_id=from_system_id)

        def _r(v):
            return round(v, 2) if v is not None else None

        out = []
        for r in results:
            entry = {
                "name": r["name"],
                "type_id": r["id"],
                "group": r["group"],
                "vol": r["vol"],
                "best_buy": _r(r["best_buy"]),
                "isk_m3": _r(r["isk_m3"]),
                "isk_hold": _r(r["isk_hold"]),
                "isk_hr": _r(r.get("isk_hr")),
                "order_count": r["order_count"],
                "demand": r["demand"],
                "system_name": r.get("system_name"),
                "station_name": r.get("station_name"),
                "jumps": r.get("jumps"),
                "sell_local": r.get("sell_local", False),
                "sell_local_label": r.get("sell_local_label"),
                # Compressed
                "comp_buy": _r(r.get("comp_buy", 0)),
                "comp_isk_m3": _r(r.get("comp_isk_m3", 0)),
                "comp_isk_hold": _r(r.get("comp_isk_hold", 0)),
                "comp_isk_hr": _r(r.get("comp_isk_hr")),
                "comp_jumps": r.get("comp_jumps"),
                "comp_system_name": r.get("comp_system_name"),
                # Reprocess
                "repro_isk_m3": _r(r.get("repro_isk_m3", 0)),
                "repro_isk_hold": _r(r.get("repro_isk_hold", 0)),
                "repro_isk_hr": _r(r.get("repro_isk_hr")),
                "repro_jumps": r.get("repro_jumps"),
                "repro_hub": r.get("repro_hub"),
                "repro_region": r.get("repro_region"),
                "repro_isk_m3_min": _r(r.get("repro_isk_m3_min")),
                "repro_isk_m3_max": _r(r.get("repro_isk_m3_max")),
                # Buyback
                "buyback_isk_m3": _r(r.get("buyback_isk_m3", 0)),
                "buyback_isk_hold": _r(r.get("buyback_isk_hold", 0)),
                "buyback_isk_hr": _r(r.get("buyback_isk_hr")),
                "buyback_type": r.get("buyback_type"),
                # Best path
                "best_path": r.get("best_path", "raw"),
                "best_isk_m3": _r(r.get("best_isk_m3", 0)),
                "best_isk_hold": _r(r.get("best_isk_hold", 0)),
                "best_isk_hr": _r(r.get("best_isk_hr")),
                "best_jumps": r.get("best_jumps"),
                "best_sell_at": r.get("best_sell_at"),
            }
            out.append(entry)

        self._send_json({
            "region": region["name"],
            "hold_size": hold_size,
            "results": out,
        })


def kill_existing(port):
    """If the port is already in use, the old server is still running. Kill it."""
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(1)
        sock.connect(("127.0.0.1", port))
        sock.close()
        # Port is in use -- find and kill the process holding it
        import subprocess
        if sys.platform == "win32":
            result = subprocess.run(
                ["powershell", "-Command",
                 f"(Get-NetTCPConnection -LocalPort {port} -ErrorAction SilentlyContinue).OwningProcess"],
                capture_output=True, text=True, timeout=5
            )
            for pid in set(result.stdout.strip().splitlines()):
                pid = pid.strip()
                if pid and pid.isdigit() and int(pid) != 0:
                    subprocess.run(
                        ["powershell", "-Command", f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue"],
                        capture_output=True, timeout=5
                    )
        else:
            try:
                subprocess.run(["fuser", "-k", f"{port}/tcp"],
                               capture_output=True, timeout=5)
            except FileNotFoundError:
                print(f"  WARNING: Port {port} in use. Stop the existing process first.")
                sys.exit(1)
        time.sleep(0.5)
    except (socket.timeout, ConnectionRefusedError, OSError):
        pass  # Port is free
    finally:
        sock.close()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="EVE Mining Tools — ore scanner & ship fitter")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bind address (default: 127.0.0.1, use 0.0.0.0 for public)")
    parser.add_argument("--port", type=int, default=PORT,
                        help=f"Port (default: {PORT})")
    parser.add_argument("--no-browser", action="store_true",
                        help="Don't auto-open browser on start")
    args = parser.parse_args()

    kill_existing(args.port)

    server = http.server.ThreadingHTTPServer((args.host, args.port), ScanHandler)
    url = f"http://{'localhost' if args.host == '127.0.0.1' else args.host}:{args.port}"
    print(f"\n  EVE Mining Tools running at {url}")
    print(f"  Press Ctrl+C to stop.\n")

    if not args.no_browser and args.host == "127.0.0.1":
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
