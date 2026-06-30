"""
Price & availability monitor for the two Klimagerät products.

Run:
    python monitor.py            # check all products, notify on any change
    python monitor.py --summary  # also send a full current-status report
    python monitor.py --test     # send a test notification and exit
    python monitor.py --quiet    # check + update state, but never notify

State is persisted in state.json (only meaningful fields, so it changes -- and
the GitHub Action only commits -- when something real changes).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

import notify
from sources import OBTAINABLE, check_source

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
STATE_PATH = ROOT / "state.json"

STATUS_LABEL = {
    "online": "online verfügbar",
    "preorder": "vorbestellbar",
    "store_only": "nur im Markt",
    "out": "nicht verfügbar",
    "unknown": "unbekannt",
}


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def fmt_price(p) -> str:
    return f"{p:.2f} EUR".replace(".", ",") if isinstance(p, (int, float)) else "-"


def evaluate_product(product: dict, timeout: int) -> dict:
    """Check every source and aggregate into one product snapshot."""
    # prices below this floor are treated as bogus (typo / bait listing):
    # shown to you, but ignored for "cheapest price" and alerts.
    floor = product.get("min_price")

    def plausible(r) -> bool:
        return not (r.price is not None and floor is not None and r.price < floor)

    # Mydealz "deal feeds" are tracked separately from the price/stock aggregate.
    price_specs = [s for s in product["sources"] if s.get("type") != "mydealz"]
    feed_specs = [s for s in product["sources"] if s.get("type") == "mydealz"]

    results = [check_source(s, timeout) for s in price_specs]
    obtainable = [r for r in results if r.obtainable and plausible(r)]
    priced = [r for r in obtainable if r.price is not None]

    best = min(priced, key=lambda r: r.price) if priced else None
    best_price = best.price if best else None
    best_source = best.label if best else None
    best_url = best.url if best else None
    sellers = len(obtainable)

    # overall status = the best status any plausible source reports
    rank = {"online": 3, "preorder": 2, "store_only": 1, "out": 0, "unknown": -1}
    status = max((r.status for r in results if plausible(r)),
                 key=lambda s: rank[s], default="unknown")

    snapshot = {
        "status": status,
        "best_price": best_price,
        "best_source": best_source,
        "best_url": best_url,
        "primary_url": results[0].url if results else None,
        "sellers": sellers,
        "sources": {
            r.label: {
                "status": r.status,
                "price": r.price,
                "note": r.note,
                "error": r.error,
                "url": r.url,
                "implausible": not plausible(r),
            }
            for r in results
        },
    }

    # community deal feeds (Mydealz)
    if feed_specs:
        deals, seen, feed_info = [], set(), []
        for spec in feed_specs:
            fr = check_source(spec, timeout)
            active = [d for d in fr.deals if not d["expired"]]
            feed_info.append({"label": fr.label, "error": fr.error, "active": len(active)})
            for d in fr.deals:
                if d["id"] not in seen:
                    seen.add(d["id"])
                    deals.append(d)
        deals.sort(key=lambda d: d["expired"])      # active first
        snapshot["deals"] = deals[:25]
        snapshot["deal_feeds"] = feed_info

    return snapshot


def click_link(snap: dict) -> str:
    """Best single URL to open from a notification for this product."""
    return snap.get("best_url") or snap.get("primary_url") or ""


def diff_events(name: str, prev: dict | None, now: dict, target_price) -> list[str]:
    """Compare previous vs current snapshot -> list of human alert lines."""
    events: list[str] = []
    now_price = now["best_price"]
    now_sellers = now["sellers"]

    if prev is None:
        return events  # first time we see this product -> baseline only

    prev_price = prev.get("best_price")
    prev_sellers = prev.get("sellers", 0)

    # back in stock
    if prev_sellers == 0 and now_sellers > 0:
        events.append(f"🟢 Wieder verfügbar bei {now_sellers} Anbieter(n) "
                      f"– ab {fmt_price(now_price)} ({now['best_source']})")
    # more sellers
    elif now_sellers > prev_sellers > 0:
        events.append(f"🛒 Mehr Anbieter: {prev_sellers} → {now_sellers} "
                      f"– ab {fmt_price(now_price)}")

    # price drop (only when both known and it actually went down)
    if (prev_price is not None and now_price is not None and now_price < prev_price):
        events.append(f"💶 Preis gefallen: {fmt_price(prev_price)} → "
                      f"{fmt_price(now_price)} ({now['best_source']})")

    # crossed below the target price
    if (target_price is not None and now_price is not None
            and now_sellers > 0 and now_price <= target_price):
        crossed = (prev_price is None or prev_price > target_price or prev_sellers == 0)
        if crossed:
            events.append(f"🎯 Unter Zielpreis ({fmt_price(target_price)}): "
                          f"jetzt {fmt_price(now_price)} ({now['best_source']})")

    # NEW community deal posted (Mydealz) -> alert on active, not-seen-before deals
    prev_ids = {d["id"] for d in prev.get("deals", [])}
    for d in now.get("deals", []):
        if not d["expired"] and d["id"] not in prev_ids:
            price = f" für {fmt_price(d['price'])}" if d["price"] else ""
            events.append(f"🔥 Neuer Mydealz-Deal{price}: {d['title']}\n   {d['url']}")

    return events


def product_report(name: str, snap: dict) -> str:
    lines = [f"{name}",
             f"  Status: {STATUS_LABEL.get(snap['status'], snap['status'])}"
             f" | günstigster Preis: {fmt_price(snap['best_price'])}"
             f"{' bei ' + snap['best_source'] if snap['best_source'] else ''}"
             f" | Anbieter: {snap['sellers']}"]
    for label, info in snap["sources"].items():
        bits = [STATUS_LABEL.get(info["status"], info["status"])]
        if info["price"] is not None:
            bits.append(fmt_price(info["price"]))
        if info["note"]:
            bits.append(info["note"])
        if info["error"]:
            bits.append(f"⚠ {info['error']}")
        if info.get("implausible"):
            bits.append("⚠ Preis unplausibel – ignoriert")
        line = f"    - {label}: {', '.join(bits)}"
        if info.get("url"):
            line += f"\n      {info['url']}"
        lines.append(line)

    if "deal_feeds" in snap:
        errs = [f["error"] for f in snap["deal_feeds"] if f["error"]]
        active = [d for d in snap.get("deals", []) if not d["expired"]]
        if errs:
            lines.append(f"    - Mydealz: ⚠ {errs[0]}")
        elif active:
            lines.append("    - Mydealz (aktive Community-Deals):")
            for d in active[:5]:
                p = f" – {fmt_price(d['price'])}" if d["price"] else ""
                lines.append(f"      🔥 {d['title']}{p}\n         {d['url']}")
        else:
            lines.append("    - Mydealz: keine aktiven Deals")
    return "\n".join(lines)


def main() -> int:
    # Windows consoles default to cp1252 and choke on emoji in our reports.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--summary", action="store_true",
                    help="send a full status report even if nothing changed")
    ap.add_argument("--test", action="store_true",
                    help="send a test notification and exit")
    ap.add_argument("--quiet", action="store_true",
                    help="update state but never notify")
    args = ap.parse_args()

    if args.test:
        notify.notify(
            "✅ Klima-Monitor Test",
            "If you can read this, e-mail and/or ntfy are working.",
            priority="default", tags="white_check_mark")
        return 0

    config = load_config()
    timeout = int(config.get("check", {}).get("timeout", 25))
    prev_state = load_state()
    first_run = not prev_state

    new_state: dict = {}
    all_events: list[str] = []
    reports: list[str] = []
    click_url = ""

    for product in config["products"]:
        name = product["name"]
        target = product.get("target_price")
        snap = evaluate_product(product, timeout)
        new_state[name] = snap

        report = product_report(name, snap)
        reports.append(report)
        print(report + "\n")

        events = diff_events(name, prev_state.get(name), snap, target)
        if events:
            link = click_link(snap)
            if link and not click_url:
                click_url = link          # whole-notification tap target
            block = f"### {name}\n" + "\n".join(events)
            if link:
                block += f"\n👉 Direkt zum Angebot: {link}"
            block += "\n\n" + report
            all_events.append(block)

    save_state(new_state)

    if args.quiet:
        return 0

    # Decide what (if anything) to send
    if all_events:
        body = "\n\n".join(all_events) + "\n\n(Automatischer Klima-Monitor)"
        notify.notify("🔔 Klima-Monitor: Änderung erkannt!", body,
                      priority="high", tags="rotating_light", click=click_url)
    elif first_run:
        body = ("Monitor ist aktiv. Aktueller Stand:\n\n"
                + "\n\n".join(reports)
                + "\n\nDu wirst benachrichtigt, sobald sich Preis oder "
                  "Verfügbarkeit ändern.")
        first = next(iter(new_state.values()), {})
        notify.notify("✅ Klima-Monitor gestartet", body,
                      priority="default", tags="white_check_mark",
                      click=click_link(first))
    elif args.summary:
        first = next(iter(new_state.values()), {})
        notify.notify("📋 Klima-Monitor: Status", "\n\n".join(reports),
                      priority="low", tags="clipboard", click=click_link(first))
    else:
        print("No changes - no notification sent.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
