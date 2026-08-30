"""Server-side listing inventory Sofia can talk about on calls.

MIRROR of the site's listings.js (keep in sync by hand until the Spark API
key lands — then replace LISTINGS with a cached /v1/my/listings fetch and
delete the hand-maintained data; the tool endpoint's contract stays the same).
All sample data — fictional homes.
"""
LISTINGS = [
    {
        "address": "6412 Camino Coronado", "area": "Upper Valley", "city": "El Paso",
        "price": 489000, "beds": 4, "baths": 3, "sqft": 2850, "status": "Active",
        "highlights": "Upper Valley, mature trees, chef's kitchen, 3-car garage, new listing this week.",
    },
    {
        "address": "1523 Cimarron Ridge Dr", "area": "West Side (Cimarron)", "city": "El Paso",
        "price": 385000, "beds": 4, "baths": 2.5, "sqft": 2400, "status": "Active",
        "hot": "Open house Saturday — listed 3 days ago, already 2 showings booked",
        "highlights": "Cimarron community, mountain views, open house this Saturday 12-3pm.",
    },
    {
        "address": "3308 Tierra Nocturna Ave", "area": "East Side", "city": "El Paso",
        "price": 265000, "beds": 3, "baths": 2, "sqft": 1780, "status": "Active",
        "hot": "Price dropped $14,000 this week — seller is motivated",
        "highlights": "Great starter home, refrigerated air, close to Loop 375 and schools.",
    },
    {
        "address": "912 Rim Rd", "area": "Kern Place", "city": "El Paso",
        "price": 549000, "beds": 5, "baths": 3.5, "sqft": 3300, "status": "Active",
        "highlights": "Historic Kern Place, city views, remodeled 2024, walk to UTEP.",
    },
    {
        "address": "14208 Desert Sage Ct", "area": "Horizon City", "city": "Horizon City",
        "price": 232000, "beds": 3, "baths": 2, "sqft": 1650, "status": "Active",
        "hot": "Just listed — under $240k in Horizon, these move fast",
        "highlights": "Under $240k, built 2021, low-maintenance yard, cul-de-sac.",
    },
    {
        "address": "7625 Franklin Summit Dr", "area": "Northeast", "city": "El Paso",
        "price": 415000, "beds": 4, "baths": 3, "sqft": 2600, "status": "Active",
        "highlights": "Franklin Mountains views, 15 min to Fort Bliss, covered patio.",
    },
]


def search(area=None, max_price=None, min_beds=None, address=None, hot_only=False):
    """Filter listings; loose case-insensitive matching. Returns list of dicts.

    hot_only mirrors the site's "Hot Homes" strip — the agent's hot sheet.
    """
    out = []
    for l in LISTINGS:
        if hot_only and not l.get("hot"):
            continue
        if address and address.strip():
            a = address.lower()
            if not any(tok in l["address"].lower() for tok in a.split() if len(tok) > 2):
                continue
        if area and area.strip():
            q = area.lower()
            if q not in l["area"].lower() and q not in l["city"].lower():
                continue
        if max_price:
            try:
                if l["price"] > float(max_price) * (1.10):  # 10% stretch
                    continue
            except (TypeError, ValueError):
                pass
        if min_beds:
            try:
                if l["beds"] < int(min_beds):
                    continue
            except (TypeError, ValueError):
                pass
        out.append(l)
    return out
