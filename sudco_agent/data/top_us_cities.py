"""Top US cities and top Texas cities by approximate population, for use
with `agent sweep`.

Populations are approximate (2020–2023 estimates) — they're used for ranking
display only, not for any logic. Names match how Foursquare's `near=` param
expects them. Texas cities that already appear in the US top-100 are kept in
both lists; the combined accessor deduplicates by (name, state).

GREATER_HOUSTON_CITIES is a separate, denser list of Houston-metro suburbs
and CDPs that aren't large enough to crack the statewide top-100 but matter
when sweeping the Houston region directly. It overlaps freely with the other
two lists; the combined accessor handles the dedup.
"""
from __future__ import annotations

# Format: (rank_within_list, city, state, approx_population)
TOP_US_CITIES: list[tuple[int, str, str, int]] = [
    (1, "New York", "NY", 8336817),
    (2, "Los Angeles", "CA", 3979576),
    (3, "Chicago", "IL", 2693976),
    (4, "Houston", "TX", 2320268),
    (5, "Phoenix", "AZ", 1680992),
    (6, "Philadelphia", "PA", 1584064),
    (7, "San Antonio", "TX", 1547253),
    (8, "San Diego", "CA", 1423851),
    (9, "Dallas", "TX", 1343573),
    (10, "San Jose", "CA", 1021795),
    (11, "Austin", "TX", 978908),
    (12, "Jacksonville", "FL", 911507),
    (13, "Fort Worth", "TX", 909585),
    (14, "Columbus", "OH", 898553),
    (15, "Charlotte", "NC", 885708),
    (16, "Indianapolis", "IN", 876384),
    (17, "San Francisco", "CA", 881549),
    (18, "Seattle", "WA", 753675),
    (19, "Denver", "CO", 727211),
    (20, "Washington", "DC", 705749),
    (21, "Nashville", "TN", 692587),
    (22, "Oklahoma City", "OK", 655057),
    (23, "El Paso", "TX", 681728),
    (24, "Boston", "MA", 692600),
    (25, "Portland", "OR", 654741),
    (26, "Las Vegas", "NV", 651319),
    (27, "Detroit", "MI", 670031),
    (28, "Memphis", "TN", 651073),
    (29, "Louisville", "KY", 617638),
    (30, "Baltimore", "MD", 593490),
    (31, "Milwaukee", "WI", 590157),
    (32, "Albuquerque", "NM", 560513),
    (33, "Tucson", "AZ", 548073),
    (34, "Fresno", "CA", 542107),
    (35, "Sacramento", "CA", 524943),
    (36, "Mesa", "AZ", 518012),
    (37, "Kansas City", "MO", 508090),
    (38, "Atlanta", "GA", 506811),
    (39, "Long Beach", "CA", 462628),
    (40, "Colorado Springs", "CO", 478221),
    (41, "Raleigh", "NC", 474069),
    (42, "Miami", "FL", 467963),
    (43, "Virginia Beach", "VA", 449974),
    (44, "Omaha", "NE", 478192),
    (45, "Oakland", "CA", 433031),
    (46, "Minneapolis", "MN", 429954),
    (47, "Tulsa", "OK", 401190),
    (48, "Arlington", "TX", 398854),
    (49, "Tampa", "FL", 399700),
    (50, "New Orleans", "LA", 390144),
    (51, "Wichita", "KS", 389938),
    (52, "Cleveland", "OH", 381009),
    (53, "Bakersfield", "CA", 380874),
    (54, "Aurora", "CO", 379289),
    (55, "Anaheim", "CA", 350365),
    (56, "Honolulu", "HI", 345064),
    (57, "Santa Ana", "CA", 332318),
    (58, "Riverside", "CA", 328101),
    (59, "Corpus Christi", "TX", 326586),
    (60, "Lexington", "KY", 323780),
    (61, "Henderson", "NV", 320189),
    (62, "Stockton", "CA", 312697),
    (63, "Saint Paul", "MN", 308096),
    (64, "Cincinnati", "OH", 303940),
    (65, "St. Louis", "MO", 300576),
    (66, "Pittsburgh", "PA", 300286),
    (67, "Greensboro", "NC", 296710),
    (68, "Lincoln", "NE", 289102),
    (69, "Anchorage", "AK", 288000),
    (70, "Plano", "TX", 287677),
    (71, "Orlando", "FL", 287442),
    (72, "Irvine", "CA", 287401),
    (73, "Newark", "NJ", 282011),
    (74, "Toledo", "OH", 272779),
    (75, "Durham", "NC", 278993),
    (76, "Chula Vista", "CA", 274492),
    (77, "Fort Wayne", "IN", 270402),
    (78, "Jersey City", "NJ", 262075),
    (79, "St. Petersburg", "FL", 265098),
    (80, "Laredo", "TX", 262491),
    (81, "Madison", "WI", 259680),
    (82, "Chandler", "AZ", 261165),
    (83, "Buffalo", "NY", 255284),
    (84, "Lubbock", "TX", 258862),
    (85, "Scottsdale", "AZ", 258069),
    (86, "Reno", "NV", 255601),
    (87, "Glendale", "AZ", 252381),
    (88, "Gilbert", "AZ", 254114),
    (89, "Winston-Salem", "NC", 247945),
    (90, "North Las Vegas", "NV", 251974),
    (91, "Norfolk", "VA", 244076),
    (92, "Chesapeake", "VA", 244835),
    (93, "Garland", "TX", 239928),
    (94, "Irving", "TX", 239798),
    (95, "Hialeah", "FL", 233394),
    (96, "Fremont", "CA", 230504),
    (97, "Boise", "ID", 235684),
    (98, "Richmond", "VA", 230436),
    (99, "Baton Rouge", "LA", 220236),
    (100, "Spokane", "WA", 228989),
]

TOP_TX_CITIES: list[tuple[int, str, str, int]] = [
    (1, "Houston", "TX", 2320268),
    (2, "San Antonio", "TX", 1547253),
    (3, "Dallas", "TX", 1343573),
    (4, "Austin", "TX", 978908),
    (5, "Fort Worth", "TX", 909585),
    (6, "El Paso", "TX", 681728),
    (7, "Arlington", "TX", 398854),
    (8, "Corpus Christi", "TX", 326586),
    (9, "Plano", "TX", 287677),
    (10, "Laredo", "TX", 262491),
    (11, "Lubbock", "TX", 258862),
    (12, "Garland", "TX", 239928),
    (13, "Irving", "TX", 239798),
    (14, "Frisco", "TX", 224000),
    (15, "Amarillo", "TX", 200000),
    (16, "McKinney", "TX", 200000),
    (17, "Grand Prairie", "TX", 195000),
    (18, "Brownsville", "TX", 187000),
    (19, "Killeen", "TX", 158000),
    (20, "Pasadena", "TX", 152000),
    (21, "Mesquite", "TX", 150000),
    (22, "McAllen", "TX", 142000),
    (23, "Waco", "TX", 140000),
    (24, "Carrollton", "TX", 134000),
    (25, "Midland", "TX", 132000),
    (26, "Denton", "TX", 132000),
    (27, "Round Rock", "TX", 130000),
    (28, "Abilene", "TX", 125000),
    (29, "Pearland", "TX", 122000),
    (30, "College Station", "TX", 120000),
    (31, "The Woodlands", "TX", 116000),
    (32, "Lewisville", "TX", 113000),
    (33, "Beaumont", "TX", 113000),
    (34, "Sugar Land", "TX", 110000),
    (35, "Tyler", "TX", 106000),
    (36, "League City", "TX", 105000),
    (37, "Allen", "TX", 105000),
    (38, "Wichita Falls", "TX", 104000),
    (39, "Edinburg", "TX", 100000),
    (40, "San Angelo", "TX", 100000),
    (41, "New Braunfels", "TX", 90000),
    (42, "Conroe", "TX", 90000),
    (43, "Mission", "TX", 84000),
    (44, "Bryan", "TX", 84000),
    (45, "Longview", "TX", 81000),
    (46, "Cedar Park", "TX", 80000),
    (47, "Temple", "TX", 80000),
    (48, "Baytown", "TX", 80000),
    (49, "Pharr", "TX", 78000),
    (50, "Flower Mound", "TX", 78000),
    (51, "Missouri City", "TX", 75000),
    (52, "Georgetown", "TX", 75000),
    (53, "Mansfield", "TX", 73000),
    (54, "North Richland Hills", "TX", 70000),
    (55, "Pflugerville", "TX", 70000),
    (56, "San Marcos", "TX", 70000),
    (57, "Leander", "TX", 67000),
    (58, "Rowlett", "TX", 67000),
    (59, "Victoria", "TX", 65000),
    (60, "Harlingen", "TX", 65000),
    (61, "Wylie", "TX", 60000),
    (62, "Euless", "TX", 60000),
    (63, "Port Arthur", "TX", 56000),
    (64, "DeSoto", "TX", 56000),
    (65, "Galveston", "TX", 53000),
    (66, "Texas City", "TX", 51000),
    (67, "Cedar Hill", "TX", 50000),
    (68, "Bedford", "TX", 50000),
    (69, "Grapevine", "TX", 50000),
    (70, "Burleson", "TX", 50000),
    (71, "Rockwall", "TX", 50000),
    (72, "Keller", "TX", 47000),
    (73, "Schertz", "TX", 45000),
    (74, "Sherman", "TX", 45000),
    (75, "Huntsville", "TX", 45000),
    (76, "Haltom City", "TX", 46000),
    (77, "Coppell", "TX", 42000),
    (78, "Hurst", "TX", 40000),
    (79, "Lancaster", "TX", 40000),
    (80, "Friendswood", "TX", 39000),
    (81, "Texarkana", "TX", 36000),
    (82, "Del Rio", "TX", 36000),
    (83, "Lufkin", "TX", 35000),
    (84, "Weatherford", "TX", 32000),
    (85, "Nacogdoches", "TX", 32000),
    (86, "Cleburne", "TX", 31000),
    (87, "Greenville", "TX", 28000),
    (88, "Eagle Pass", "TX", 28000),
    (89, "Lake Jackson", "TX", 27000),
    (90, "Big Spring", "TX", 26000),
    (91, "Denison", "TX", 25000),
    (92, "Paris", "TX", 25000),
    (93, "Kerrville", "TX", 24000),
    (94, "Marshall", "TX", 23000),
    (95, "Plainview", "TX", 22000),
    (96, "Stephenville", "TX", 21000),
    (97, "Mount Pleasant", "TX", 16000),
    (98, "Hereford", "TX", 14000),
    (99, "Brenham", "TX", 17000),
    (100, "Corsicana", "TX", 25000),
]

# Greater Houston metro — Houston itself plus its suburbs and notable CDPs.
# Includes places too small for the statewide top-100 (e.g. Montgomery,
# Tomball) that are still worthwhile sweep targets when fishing locally.
# Sorted by approximate population descending. Overlaps with TOP_TX_CITIES
# are fine — `top(region="all")` and `top(region="hou")` dedup by name+state.
GREATER_HOUSTON_CITIES: list[tuple[int, str, str, int]] = [
    (1, "Houston", "TX", 2320268),
    (2, "Pasadena", "TX", 152000),
    (3, "Cypress", "TX", 135000),
    (4, "Pearland", "TX", 122000),
    (5, "The Woodlands", "TX", 116000),
    (6, "Sugar Land", "TX", 110000),
    (7, "League City", "TX", 105000),
    (8, "Conroe", "TX", 90000),
    (9, "Atascocita", "TX", 88000),
    (10, "Baytown", "TX", 80000),
    (11, "Missouri City", "TX", 75000),
    (12, "Spring", "TX", 62000),
    (13, "Galveston", "TX", 53000),
    (14, "Texas City", "TX", 51000),
    (15, "Channelview", "TX", 45000),
    (16, "Friendswood", "TX", 39000),
    (17, "Rosenberg", "TX", 38000),
    (18, "La Porte", "TX", 36000),
    (19, "Deer Park", "TX", 33000),
    (20, "Alvin", "TX", 26000),
    (21, "Katy", "TX", 26000),
    (22, "Sienna", "TX", 23000),
    (23, "Dickinson", "TX", 21000),
    (24, "Cinco Ranch", "TX", 18000),
    (25, "Bellaire", "TX", 17000),
    (26, "Stafford", "TX", 17000),
    (27, "Lake Jackson", "TX", 27000),
    (28, "Humble", "TX", 16000),
    (29, "West University Place", "TX", 15000),
    (30, "Seabrook", "TX", 14000),
    (31, "Tomball", "TX", 12500),
    (32, "Webster", "TX", 12000),
    (33, "Richmond", "TX", 12000),
    (34, "Brenham", "TX", 17000),
    (35, "Crosby", "TX", 3200),
    (36, "Magnolia", "TX", 3000),
    (37, "Montgomery", "TX", 2000),
]


def _key(entry: tuple[int, str, str, int]) -> tuple[str, str]:
    return (entry[1].lower(), entry[2].upper())


def _merge_dedup(*lists: list[tuple[int, str, str, int]]) -> list[tuple[int, str, str, int]]:
    """Concatenate lists, dedup by (name, state), re-sort by population desc,
    and re-rank from 1."""
    seen: set[tuple[str, str]] = set()
    merged: list[tuple[int, str, str, int]] = []
    for src in lists:
        for entry in src:
            k = _key(entry)
            if k in seen:
                continue
            seen.add(k)
            merged.append(entry)
    merged.sort(key=lambda e: e[3], reverse=True)
    return [(i + 1, e[1], e[2], e[3]) for i, e in enumerate(merged)]


def top(n: int = 50, *, region: str = "us") -> list[tuple[int, str, str, int]]:
    """Return the top N cities for a region.

    region:
      "us"  — top US cities (default)
      "tx"  — top Texas cities
      "hou" — Greater Houston metro (Houston + suburbs + notable CDPs)
      "all" — combined US + Texas + Houston-metro, deduped by (name, state),
              sorted by population.
    """
    region = region.lower()
    if region == "us":
        source = TOP_US_CITIES
    elif region == "tx":
        source = TOP_TX_CITIES
    elif region == "hou":
        source = _merge_dedup(GREATER_HOUSTON_CITIES)
    elif region == "all":
        source = _merge_dedup(TOP_US_CITIES, TOP_TX_CITIES, GREATER_HOUSTON_CITIES)
    else:
        raise ValueError(f"unknown region: {region!r} (expected 'us'|'tx'|'hou'|'all')")
    return source[: max(0, min(n, len(source)))]


def find(name: str, state: str | None = None) -> tuple[int, str, str, int] | None:
    """Look up a single city by name (case-insensitive)."""
    name_l = name.lower()
    state_u = state.upper() if state else None
    for entry in [*TOP_US_CITIES, *TOP_TX_CITIES, *GREATER_HOUSTON_CITIES]:
        if entry[1].lower() == name_l and (state_u is None or entry[2].upper() == state_u):
            return entry
    return None
