"""
Fetching + parsing for each supported source type.

Every parser returns a SourceResult with a *tri-state* status so that a blocked
or failed request is never mistaken for "out of stock":

    online      -> buyable online right now
    preorder    -> can be ordered (pre-order / backorder)
    store_only  -> only available in a physical store (e.g. OBI InStoreOnly)
    out         -> definitely not available
    unknown     -> we could not tell (blocked, network error, parse failure)

Only `online`, `preorder` and `store_only` count as "obtainable".
"""

from __future__ import annotations

import html as _html
import json
import re
from dataclasses import dataclass, field
from typing import Optional

import requests

# Statuses that mean "you can get the product"
OBTAINABLE = {"online", "preorder", "store_only"}

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

# schema.org availability tail -> our status
_SCHEMA_AVAIL = {
    "instock": "online",
    "onlineonly": "online",
    "limitedavailability": "online",
    "instoreonly": "store_only",
    "preorder": "preorder",
    "backorder": "preorder",
    "outofstock": "out",
    "soldout": "out",
    "discontinued": "out",
}


@dataclass
class SourceResult:
    label: str
    url: str
    status: str = "unknown"          # one of the tri-state values above
    price: Optional[float] = None    # EUR
    currency: str = "EUR"
    note: str = ""                   # short human-readable detail
    error: str = ""                  # set when the fetch/parse failed
    deals: list = field(default_factory=list)  # community deal posts (Mydealz)

    @property
    def obtainable(self) -> bool:
        return self.status in OBTAINABLE


def _get(url: str, timeout: int = 25) -> requests.Response:
    return requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)


def _looks_blocked(html: str, status_code: int) -> bool:
    if status_code in (403, 429, 503):
        return True
    low = html[:5000].lower()
    return any(s in low for s in (
        "just a moment", "/cdn-cgi/challenge", "sichere verbindung wird",
        "captcha", "are you a robot", "zugriff verweigert", "access denied",
        "sicherheitspr",  # "Sicherheitsprüfung"
    ))


# --------------------------------------------------------------------------- #
# Parser: schema.org JSON-LD  (OBI, bestell.bar, werkzeugbedarf, most shops)
# --------------------------------------------------------------------------- #
def _iter_jsonld(html: str):
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.S | re.I,
    ):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
        except Exception:
            continue
        if isinstance(data, dict) and "@graph" in data:
            yield from (x for x in data["@graph"] if isinstance(x, dict))
        elif isinstance(data, list):
            yield from (x for x in data if isinstance(x, dict))
        elif isinstance(data, dict):
            yield data


def _norm_avail(value) -> str:
    if not value:
        return "unknown"
    tail = str(value).rstrip("/").split("/")[-1].split("#")[-1].lower()
    return _SCHEMA_AVAIL.get(tail, "unknown")


def _to_price(value) -> Optional[float]:
    if value in (None, "", "null"):
        return None
    s = str(value).strip()
    # handle "1.499,00" (de) and "1499.00" (en)
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    s = re.sub(r"[^0-9.]", "", s)
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def parse_jsonld(result: SourceResult, html: str) -> SourceResult:
    best_status = "unknown"
    best_price = None
    currency = "EUR"
    rank = {"online": 3, "preorder": 2, "store_only": 1, "out": 0, "unknown": -1}
    for node in _iter_jsonld(html):
        offers = node.get("offers")
        if not offers:
            continue
        for off in (offers if isinstance(offers, list) else [offers]):
            if not isinstance(off, dict):
                continue
            status = _norm_avail(off.get("availability"))
            price = _to_price(off.get("price") or off.get("lowPrice"))
            currency = off.get("priceCurrency") or currency
            # keep the most-available offer, and the lowest price seen
            if rank.get(status, -1) > rank.get(best_status, -1):
                best_status = status
            if price is not None and (best_price is None or price < best_price):
                best_price = price
    if best_price is None:
        best_price = _fallback_price(html)   # shops that don't put price in JSON-LD
    if best_status == "unknown" and best_price is None:
        result.error = "no JSON-LD offer found"
        return result
    result.status = best_status
    result.price = best_price
    result.currency = currency
    return result


def _fallback_price(html: str) -> "float | None":
    """Find a visible price when JSON-LD has none (OpenGraph / WooCommerce / itemprop)."""
    patterns = [
        r'property=["\']product:price:amount["\']\s+content=["\']([\d.,]+)["\']',
        r'Aktueller Preis ist:\s*([\d.,]+)',                       # WooCommerce sale price
        r'itemprop=["\']price["\']\s+content=["\']([\d.,]+)["\']',
        r'class="price"[^>]*>(?:(?!</p>).)*?woocommerce-Price-amount[^<]*<bdi>\s*([\d.,]+)',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.S | re.I)
        if m:
            p = _to_price(m.group(1))
            if p:
                return p
    return None


# --------------------------------------------------------------------------- #
# Parser: Shopify product JSON  (tado store, any *.myshopify-based store)
# --------------------------------------------------------------------------- #
def fetch_shopify(result: SourceResult, timeout: int) -> SourceResult:
    url = result.url.split("?")[0].rstrip("/") + ".js"
    r = _get(url, timeout)
    if r.status_code != 200:
        result.error = f"HTTP {r.status_code}"
        return result
    try:
        data = r.json()
    except Exception as e:
        result.error = f"bad JSON: {e}"
        return result
    variants = data.get("variants") or []
    available_prices = [v["price"] / 100.0 for v in variants if v.get("available")]
    all_prices = [v["price"] / 100.0 for v in variants if v.get("price") is not None]
    if available_prices:
        result.status = "online"
        result.price = round(min(available_prices), 2)
    else:
        result.status = "out"
        result.price = round(min(all_prices), 2) if all_prices else None
    return result


# --------------------------------------------------------------------------- #
# Parser: Geizhals / heise Preisvergleich  (aggregates ~all shops in one page)
# Prices are JS-rendered, but the "any offers at all?" signal is static & solid.
# --------------------------------------------------------------------------- #
def parse_geizhals(result: SourceResult, html: str) -> SourceResult:
    m = re.search(r'id=["\']pricerange-no-offers["\']\s+class=["\']([^"\']*)["\']', html)
    if m is not None:
        # element present: hidden => there ARE offers; visible => none
        has_offers = "hidden" in m.group(1)
        result.status = "online" if has_offers else "out"
    elif "Derzeit keine Angebote" in html:
        result.status = "out"
    else:
        result.error = "could not locate offer marker"
        return result

    if result.status == "online":
        # cheapest current offer lives in the "Aktueller Preisbereich" block
        pm = (re.search(r'id=["\']pricerange-min["\']>\s*<span[^>]*gh_price[^>]*>[^0-9]*([\d.]+,\d{2})', html)
              or re.search(r'id=["\']pricerange-for["\'][^>]*>\s*um\s*<strong>\s*<span[^>]*gh_price[^>]*>[^0-9]*([\d.]+,\d{2})', html))
        if pm:
            result.price = _to_price(pm.group(1))
        oc = re.search(r'(\d+)\s*Angebote?\b', html)
        result.note = f"{oc.group(1)} Angebote (alle Shops)" if oc else "Angebote vorhanden"
    else:
        result.note = "keine Angebote"
    return result


# --------------------------------------------------------------------------- #
# Parser: Mydealz community deal feed (search results page)
# Returns a list of deal posts; the monitor alerts on NEW active ones.
# Optional spec keys:  match  = regex the deal title must contain
#                      exclude = regex that disqualifies a deal title
# --------------------------------------------------------------------------- #
def fetch_mydealz(result: SourceResult, spec: dict, timeout: int) -> SourceResult:
    r = _get(result.url, timeout)
    low = r.text[:4000].lower()
    if r.status_code != 200 or any(s in low for s in
                                   ("just a moment", "cf-chl", "attention required")):
        result.error = f"blocked (HTTP {r.status_code})"
        return result

    mre = re.compile(spec["match"], re.I) if spec.get("match") else None
    xre = re.compile(spec["exclude"], re.I) if spec.get("exclude") else None

    deals, seen = [], set()
    for a in re.findall(r"<article\b(.*?)</article>", r.text, re.S):
        lm = re.search(r'href="(?:https://www\.mydealz\.de)?(/deals/[^"#?]+)"', a)
        tm = re.search(r'class="[^"]*thread-title[^"]*"[^>]*>(.*?)</a>', a, re.S)
        if not lm or not tm:
            continue
        path = lm.group(1)
        if path in seen:
            continue
        title = _html.unescape(re.sub(r"<[^>]+>", "", tm.group(1))).strip()
        if (mre and not mre.search(title)) or (xre and xre.search(title)):
            continue
        seen.add(path)
        pm = re.search(r'class="[^"]*thread-price[^"]*"[^>]*>(.*?)</span>', a, re.S)
        ptxt = _html.unescape(re.sub(r"<[^>]+>", "", pm.group(1))) if pm else ""
        pnum = (re.search(r"(\d[\d.]*,\d{2}|\d[\d.]+|\d+)", ptxt)
                or re.search(r"(?:für|fuer|nur|ab)\s*(\d[\d.]*(?:,\d{2})?)\s*€", title, re.I)
                or re.search(r"(\d[\d.]*(?:,\d{2})?)\s*€", title))
        deals.append({
            "id": path,
            "title": title[:120],
            "price": _to_price(pnum.group(1)) if pnum else None,
            "url": "https://www.mydealz.de" + path,
            "expired": "thread--expired" in a,
        })

    deals.sort(key=lambda d: d["expired"])          # active first
    result.deals = deals[:25]
    active = [d for d in result.deals if not d["expired"]]
    result.status = "online" if active else "out"
    result.note = f"{len(active)} aktive Deals" if active else "keine aktiven Deals"
    return result


PARSERS = {
    "jsonld": parse_jsonld,
    "geizhals": parse_geizhals,
}


def check_source(spec: dict, timeout: int = 25) -> SourceResult:
    """Fetch + parse one configured source. Never raises."""
    stype = spec.get("type", "jsonld")
    result = SourceResult(label=spec.get("label", spec.get("url", "?")), url=spec["url"])
    try:
        if stype == "shopify":
            return fetch_shopify(result, timeout)
        if stype == "mydealz":
            return fetch_mydealz(result, spec, timeout)
        r = _get(result.url, timeout)
        if _looks_blocked(r.text, r.status_code):
            result.error = f"blocked (HTTP {r.status_code})"
            return result
        if r.status_code != 200:
            result.error = f"HTTP {r.status_code}"
            return result
        parser = PARSERS.get(stype)
        if parser is None:
            result.error = f"unknown source type '{stype}'"
            return result
        return parser(result, r.text)
    except requests.RequestException as e:
        result.error = f"network error: {type(e).__name__}"
        return result
    except Exception as e:  # parsing never kills the run
        result.error = f"{type(e).__name__}: {e}"
        return result
