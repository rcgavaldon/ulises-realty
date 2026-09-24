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


_OH_TIME = re.compile(r'<div class="listing-detail-event-time">\s*([^<]+?)\s*</div>')
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december"]
_ES = {"monday": "lunes", "tuesday": "martes", "wednesday": "miércoles", "thursday": "jueves",
       "friday": "viernes", "saturday": "sábado", "sunday": "domingo",
       "january": "enero", "february": "febrero", "march": "marzo", "april": "abril", "may": "mayo",
       "june": "junio", "july": "julio", "august": "agosto", "september": "septiembre",
       "october": "octubre", "november": "noviembre", "december": "diciembre"}


def open_house_times(page_html: str, today=None) -> list:
    """Upcoming open houses on a listing_detail page, e.g.
    'Saturday, September 26, 12:00pm - 4:00pm'. Dates before `today` are dropped."""
    from datetime import date
    out = []
    for raw in _OH_TIME.findall(page_html or ""):
        s = " ".join(re.sub(r"[^0-9A-Za-z ,:.\-–]", "", html.unescape(raw)).split())
        m = re.match(r"[A-Za-z]+, ([A-Za-z]+) (\d{1,2})\b", s)
        if m and today and m.group(1).lower() in _MONTHS:
            try:
                d = date(today.year, _MONTHS.index(m.group(1).lower()) + 1, int(m.group(2)))
                if (today - d).days > 180:          # a January date seen in December
                    d = date(today.year + 1, d.month, d.day)
                if d < today:
                    continue
            except ValueError:
                pass
        if s:
            out.append(s)
    return out


def es_time(s: str) -> str:
    """'Saturday, September 26, 12:00pm - 4:00pm' -> 'sábado, 26 de septiembre, 12:00pm - 4:00pm'"""
    m = re.match(r"([A-Za-z]+), ([A-Za-z]+) (\d{1,2}),? (.*)", s)
    if not m:
        return s
    day, mon = _ES.get(m.group(1).lower(), m.group(1)), _ES.get(m.group(2).lower(), m.group(2))
    return f"{day}, {m.group(3)} de {mon}, {m.group(4)}"


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
        # a card can carry several labels: "Open House" is a flag, the other
        # one ("New Listing", "Price Change") is the ribbon
        labels = [html.unescape(x).split("·")[0].strip()
                  for x in re.findall(r'class="label label-[a-z-]+">([^<]+)<', block)]
        open_house = any(x.lower() == "open house" for x in labels)
        event = next((x for x in labels if x.lower() != "open house"), "")
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
            "open_house": open_house,           # upcoming open house; the day/time isn't on the card
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
        # Open house day + hours: only on the detail page a click opens. A few
        # homes at most; any failure keeps the flag and just skips the times.
        from datetime import datetime
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("America/Denver")).date()
        for x in rows:
            if not x.get("open_house") or not x.get("id"):
                continue
            try:
                d = c.get(f"{share}/listing_detail/{x['id']}")
                d.raise_for_status()
                times = open_house_times(d.text, today)
                if times:
                    x["open_house_times"] = times
                    x["open_house_times_es"] = [es_time(t) for t in times]
                elif "listing-detail-event-time" in d.text:
                    x["open_house"] = False         # every date listed has passed
            except Exception:
                pass
    return rows
