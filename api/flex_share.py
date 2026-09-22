"""Ulises's Flexmls hot sheet, read from his public shared link.

He keeps a saved search in Flexmls and shares it; that share is public (no
login), always current, and returns the listings as server-rendered cards when
asked with X-Requested-With. Each card carries a JSON blob (address, price,
beds, baths, status) plus its photos and the listing office, which is kept
because IDX display requires crediting the listing brokerage.

Errors raise; spark_sync catches them and keeps serving the last good pull.
"""
import html
import json
import re

SHARE = "https://my.flexmls.com/Ulises1/search/shared_links/EdiAl"
UA = "Mozilla/5.0 (RG Automations listing sync)"
MAX_PAGES = 10          # 10 listings a page; a hot sheet is a few pages at most

_CARD = re.compile(r'(?=<div id="\d{26}" data-standard-status=)')
_EVENTS = {             # the card's event label -> ribbon (EN, ES)
    "new listing": ("Just Listed", "Recién Publicada"),
    "price change": ("Price Change", "Cambio de Precio"),
    "price reduced": ("Price Reduced", "Precio Reducido"),
    "back on market": ("Back on Market", "De Nuevo en Venta"),
}
_STATUS_ES = {"Active": "Activa", "Pending": "Pendiente",
              "Active Under Contract": "Bajo Contrato", "Sold": "Vendida"}


def _text(block: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", block)))


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse(page_html: str, share: str = SHARE) -> list:
    """One page of share cards -> our listing shape (same keys spark_client uses)."""
    out = []
    for block in _CARD.split(page_html)[1:]:
        m = re.search(r'data-listing="([^"]+)"', block)
        if not m:
            continue
        try:
            d = json.loads(html.unescape(m.group(1)))
        except ValueError:
            continue
        price = _num(d.get("CurrentPrice") or d.get("ListPrice"))
        addr = (d.get("StreetAddress") or "").strip()
        if not addr or not price:
            continue
        t = _text(block)
        imgs = re.search(r'data-image-carousel-images="([^"]+)"', block)
        try:
            img = (json.loads(html.unescape(imgs.group(1))) or [""])[0] if imgs else ""
        except ValueError:
            img = ""
        if not img:
            one = re.search(r'<img src="(https://cdn\.resize\.sparkplatform\.com[^"]+)"', block)
            img = one.group(1) if one else ""
        sub = re.search(r"Subdivision\s+(.+?)\s+Zip Code", t)
        office = re.search(r"List Office Name:\s*(.+?)(?:\s+List Office URL:|\s+Last Modified|$)", t)
        ev = re.search(r'class="label label-[a-z-]+">([^<]+)<', block)
        event = html.unescape(ev.group(1)).split("·")[0].strip() if ev else ""
        status = d.get("MlsStatus") or "Active"
        tag = _EVENTS.get(event.lower()) or (status, _STATUS_ES.get(status, status))
        key = d.get("ListingKey") or ""
        out.append({
            "id": key,
            "mls": d.get("ListingId") or "",
            # "1720 FIREHOUSE Drive" -> "1720 Firehouse Drive"; keeps "N", "TX"
            "address": " ".join(w.title() if w.isupper() and len(w) > 2 else w for w in addr.split()),
            "area": (sub.group(1).strip() if sub else "") or d.get("City") or "",
            "city": d.get("City") or "",
            "postal": str(d.get("PostalCode") or ""),
            "price": price,
            "beds": _num(d.get("BedsTotal")),
            "baths": _num(d.get("BathsTotal")),
            "sqft": None,                       # not on the share's summary cards
            "status": status,
            "img": img,
            "office": office.group(1).strip() if office else "",
            "url": f"{share}/listings/{key}" if key else share,
            "public_remarks": "",
            "hot_tag": tag[0], "hot_tag_es": tag[1],
            "note": "", "note_es": "",
        })
    return out


def fetch(share: str = SHARE, max_pages: int = MAX_PAGES) -> list:
    """Every listing on the shared hot sheet, newest first as Flexmls orders them."""
    import httpx
    seen, rows = set(), []
    with httpx.Client(timeout=30, follow_redirects=True,
                      headers={"User-Agent": UA, "X-Requested-With": "XMLHttpRequest"}) as c:
        for page in range(1, max_pages + 1):
            r = c.get(f"{share}/listings", params={"list_view": "summary", "page": page})
            r.raise_for_status()
            got = [x for x in parse(r.text, share) if x["id"] not in seen]
            if not got:
                break
            seen.update(x["id"] for x in got)
            rows.extend(got)
    return rows
