#!/usr/bin/env python3
"""
resort_sniper.py - finds 55%+ off ultra-luxury all-inclusive resorts.

Pipeline:
    RSS feeds
      -> new posts
      -> find resort name + price + night count + party size
      -> normalise to $ per person per night
      -> compare against a reference rate for that resort
      -> alert when the discount clears your threshold

Configure under the "resorts:" key in config.yaml.
State lives in state/resorts.json so you never see the same deal twice.
"""

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests
import yaml

STATE = Path("state/resorts.json")
UA = {"User-Agent": "Mozilla/5.0 (compatible; resort-sniper/1.0)"}
HTML = re.compile(r"<[^>]+>")


# --------------------------------------------------------------------------
# DEFAULTS - used when config.yaml has no "resorts:" section.
# Reference rates are per PERSON, per NIGHT, in USD, at typical booked rates
# (not rack rate). Tune these to your own research.
# --------------------------------------------------------------------------
DEFAULT_REFERENCE = {
    "le blanc": 550,
    "grand velas": 650,
    "excellence playa mujeres": 380,
    "excellence riviera": 360,
    "excellence punta cana": 380,
    "excellence oyster bay": 400,
    "unico 20": 420,
    "unico riviera": 420,
    "xcaret arte": 500,
    "xcaret mexico": 430,
    "zoetry": 450,
    "impressions isla mujeres": 450,
    "impressions moxche": 430,
    "secrets moxche": 350,
    "secrets playa mujeres": 320,
    "atelier playa mujeres": 400,
    "valentin imperial": 300,
    "trs coral": 320,
    "trs yucatan": 310,
    "iberostar grand": 400,
    "live aqua": 300,
    "hard rock": 280,
    "palafitos": 400,
    "waldorf astoria cancun": 500,
    "banyan tree mayakoba": 600,
    "sandals royal": 480,
    "sandals grande": 480,
    "sandals ochi": 460,
    "beaches turks": 520,
    "curtain bluff": 800,
    "jade mountain": 950,
    "hermitage bay": 850,
    "bodyholiday": 500,
    "spice island": 700,
    "calabash": 450,
    "galley bay": 500,
    "carlisle bay": 600,
    "palm island": 700,
    "petit st vincent": 900,
    "east winds": 500,
    "ladera": 800,
    "coco plum": 500,
    "hyatt zilara": 400,
    "hyatt ziva": 340,
}

DEFAULT_LUXURY = [
    "ultra luxury", "ultra-luxury", "ultra all inclusive", "five star",
    "5-star", "5 star", "adults only", "adults-only", "butler service",
    "swim-up suite", "swim up suite", "overwater", "plunge pool",
    "michelin", "rooftop pool", "private pool suite", "gourmet",
    "le blanc", "grand velas", "excellence", "unico", "xcaret", "zoetry",
    "impressions", "secrets", "atelier", "valentin", "trs ", "iberostar grand",
    "sandals", "beaches", "curtain bluff", "jade mountain", "hermitage",
    "bodyholiday", "spice island", "calabash", "galley bay", "carlisle bay",
    "palm island", "petit st vincent", "east winds", "ladera",
]

DEFAULT_FEEDS = [
    {"name": "Secret Flying Hotels",
     "url": "https://www.secretflying.com/hotel-deals/feed/", "weight": 10},
    {"name": "Fly4Free",
     "url": "https://www.fly4free.com/feed/", "weight": 8},
    {"name": "Holiday Pirates",
     "url": "https://www.holidaypirates.com/feed/", "weight": 8},
    {"name": "Reddit r/AllInclusiveResorts",
     "url": "https://www.reddit.com/r/AllInclusiveResorts/new/.rss", "weight": 6},
    {"name": "Reddit r/traveldeals",
     "url": "https://www.reddit.com/r/traveldeals/new/.rss", "weight": 5},
    {"name": "Reddit AI deal search",
     "url": "https://www.reddit.com/r/travel/search.rss?q=all+inclusive+deal&restrict_sr=1&sort=new",
     "weight": 6},
]


# --------------------------------------------------------------------------
# Text parsing
# --------------------------------------------------------------------------
CUR = {"$": 1.0, "usd": 1.0, "€": 1.10, "eur": 1.10, "£": 1.28, "gbp": 1.28}

PRICE_RE = re.compile(
    r"([$€£])\s?(\d[\d,]{1,6})|\b(usd|eur|gbp)\s?(\d[\d,]{1,6})", re.I)
NIGHTS_RE = re.compile(r"\b(\d{1,2})\s*[-\s]?\s*(?:nights?|nts?)\b", re.I)
NIGHTS_N_RE = re.compile(r"\b(\d{1,2})\s?n\b", re.I)
PAX_RE = re.compile(
    r"\b(\d)\s*(?:adults?|people|persons?|pax|guests?)\b", re.I)
PPN_RE = re.compile(
    r"(per\s+person\s+per\s+night|pppn|p\.?p\.?\s+per\s+night"
    r"|per\s+night\s+per\s+person)", re.I)
PP_RE = re.compile(r"(per\s+person|per\s+adult|\bpp\b|p\.p\.)", re.I)
ROOM_RE = re.compile(
    r"(per\s+room|per\s+couple|for\s+(?:two|2)\b|double\s+occupancy)", re.I)


def strip_html(s):
    return re.sub(r"\s+", " ", HTML.sub(" ", s or "")).strip()


def parse_deal(text):
    """
    Return (usd_per_person_per_night, detail) or (None, reason).
    """
    t = text.lower()

    prices = []
    for m in PRICE_RE.finditer(text):
        sym = (m.group(1) or m.group(3) or "").lower()
        num = m.group(2) or m.group(4) or ""
        try:
            val = float(num.replace(",", ""))
        except ValueError:
            continue
        val *= CUR.get(sym, 1.0)
        if 50 <= val <= 40000:
            prices.append(val)

    if not prices:
        return None, "no price found"
    price = min(prices)

    m = NIGHTS_RE.search(t) or NIGHTS_N_RE.search(t)
    if not m:
        return None, "no night count"
    nights = int(m.group(1))
    if not (2 <= nights <= 30):
        return None, f"{nights}n outside 2-30"

    if PPN_RE.search(t):
        pax, divide_nights = 1, False
    elif PP_RE.search(t):
        pax, divide_nights = 1, True
    else:
        pm = PAX_RE.search(t)
        pax = int(pm.group(1)) if pm else (2 if ROOM_RE.search(t) else 2)
        divide_nights = True

    if not (1 <= pax <= 4):
        pax = 2

    ppn = price / pax / (nights if divide_nights else 1)
    detail = f"${price:,.0f} / {pax} pax / {nights}n = ${ppn:,.0f} pp/night"
    return ppn, detail


def match_resort(blob, reference):
    """Longest matching resort name wins, so 'excellence playa mujeres'
    beats a bare 'excellence'."""
    t = blob.lower()
    best = None
    for name in reference:
        if name in t and (best is None or len(name) > len(best)):
            best = name
    return best


# --------------------------------------------------------------------------
# Config / state
# --------------------------------------------------------------------------
def load_config():
    cfg = {}
    p = Path("config.yaml")
    if p.exists():
        try:
            cfg = yaml.safe_load(p.read_text()) or {}
        except Exception as e:
            print(f"[warn] config.yaml unreadable: {e}")

    r = cfg.setdefault("resorts", {})
    r.setdefault("min_discount", 0.55)
    r.setdefault("min_pp_per_night", 60)
    r.setdefault("max_pp_per_night", 900)
    r.setdefault("default_luxury_reference", 420)
    r.setdefault("reference_prices", DEFAULT_REFERENCE)
    r.setdefault("luxury_keywords", DEFAULT_LUXURY)
    r.setdefault("feeds", DEFAULT_FEEDS)
    r.setdefault("kill_keywords", [
        "cruise", "flight only", "flights only", "villa", "airbnb",
        "timeshare", "points transfer",
    ])

    r["ntfy_topic"] = os.getenv("NTFY_TOPIC") or r.get("ntfy_topic")
    r["telegram_token"] = os.getenv("TELEGRAM_TOKEN")
    r["telegram_chat"] = os.getenv("TELEGRAM_CHAT")
    return r


def load_state():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            pass
    return {"seen": [], "hits": 0}


def save_state(state):
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2))


def notify(cfg, title, message, url=None, high=False):
    sent = False
    if cfg.get("ntfy_topic"):
        payload = {
            "topic": cfg["ntfy_topic"],
            "title": title[:200],
            "message": message[:3500],
            "priority": 4 if high else 3,
            "tags": ["desert_island"] if high else ["hotel"],
        }
        if url:
            payload["click"] = url
        try:
            requests.post("https://ntfy.sh/", json=payload, timeout=20)
            sent = True
        except Exception as e:
            print(f"  ntfy error: {e}")

    if cfg.get("telegram_token") and cfg.get("telegram_chat"):
        text = f"<b>{title}</b>\n{message}"
        if url:
            text += f'\n<a href="{url}">Open deal</a>'
        try:
            requests.post(
                f"https://api.telegram.org/bot{cfg['telegram_token']}/sendMessage",
                json={"chat_id": cfg["telegram_chat"], "text": text,
                      "parse_mode": "HTML"}, timeout=20)
            sent = True
        except Exception as e:
            print(f"  telegram error: {e}")

    return sent


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    cfg = load_config()
    state = load_state()

    reference = {k.lower(): float(v) for k, v in cfg["reference_prices"].items()}
    luxury = [k.lower() for k in cfg["luxury_keywords"]]
    kills = [k.lower() for k in cfg["kill_keywords"]]
    threshold = float(cfg["min_discount"])
    default_ref = float(cfg["default_luxury_reference"])

    print(f"Threshold: {threshold:.0%} off | "
          f"{len(reference)} resorts tracked | {len(cfg['feeds'])} feeds")

    evaluated = 0
    alerts = 0

    for feed in cfg["feeds"]:
        try:
            parsed = feedparser.parse(feed["url"], request_headers=UA)
        except Exception as e:
            print(f"[skip] {feed['name']}: {e}")
            continue

        print(f"[{feed['name']}] {len(parsed.entries)} entries")

        for entry in parsed.entries[:40]:
            raw_id = entry.get("id") or entry.get("link") or entry.get("title", "")
            uid = hashlib.sha1(raw_id.encode()).hexdigest()
            if uid in state["seen"]:
                continue
            state["seen"].append(uid)

            title = strip_html(entry.get("title", ""))
            body = strip_html(entry.get("summary", ""))[:900]
            blob = f"{title} {body}"
            low = blob.lower()

            if any(k in low for k in kills):
                continue

            resort = match_resort(blob, reference)
            if resort:
                ref = reference[resort]
            elif any(k in low for k in luxury):
                resort = "unnamed luxury AI"
                ref = default_ref
            else:
                continue

            ppn, detail = parse_deal(blob)
            if ppn is None:
                print(f"  ? {resort}: {detail} | {title[:60]}")
                continue

            if not (cfg["min_pp_per_night"] <= ppn <= cfg["max_pp_per_night"]):
                continue

            evaluated += 1
            discount = 1 - (ppn / ref)

            flag = "ALERT" if discount >= threshold else "     "
            print(f"  {flag} {resort} @ {discount:+.0%} vs ${ref:,.0f} "
                  f"| {detail}")

            if discount >= threshold:
                alerts += 1
                high = discount >= 0.70
                notify(
                    cfg,
                    f"{discount:.0%} OFF {resort.title()}",
                    f"{title}\n\n"
                    f"Effective rate: {detail}\n"
                    f"Reference for this resort: ${ref:,.0f} pp/night\n"
                    f"Discount: {discount:.0%}"
                    + ("\n\nThis is a monster. Move fast." if high else ""),
                    url=entry.get("link"),
                    high=high,
                )

    state["seen"] = state["seen"][-4000:]
    state["hits"] = state.get("hits", 0) + alerts
    save_state(state)

    print(f"\n{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC | "
          f"{evaluated} priced deals evaluated, {alerts} alerts sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
