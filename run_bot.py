#!/usr/bin/env python3
"""
Deal Bot - finds cheap flights + cruise deals and pushes them to your phone.

Engine 1 (feeds):  watches deal sites where humans post mistake fares.
Engine 2 (prices): flags fares far below their own recent average.

Everything you'd want to change lives in config.yaml.
"""

import hashlib
import json
import os
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests
import yaml

STATE_PATH = Path("state/seen.json")
USER_AGENT = {"User-Agent": "Mozilla/5.0 (compatible; deal-bot/1.0)"}
MIN_HISTORY = 6  # data points needed before we trust an "average"
HTML_TAG = re.compile(r"<[^>]+>")
PRICE_RE = re.compile(r"\$\s?([\d,]{2,7})")


# ---------------------------------------------------------------- setup
def load_config():
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    cfg.setdefault("notify", {})
    # GitHub Secrets override whatever is in the config file
    cfg["notify"]["ntfy_topic"] = os.getenv("NTFY_TOPIC") or cfg["notify"].get("ntfy_topic")
    cfg["notify"]["telegram_token"] = os.getenv("TELEGRAM_TOKEN")
    cfg["notify"]["telegram_chat"] = os.getenv("TELEGRAM_CHAT")
    return cfg


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    return {"seen": [], "prices": {}}


def save_state(state):
    STATE_PATH.parent.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def notify(cfg, title, message, url=None, high=False, tags=None):
    """Send a push notification. Returns True if anything was delivered."""
    tags = tags or ["airplane"]
    n = cfg.get("notify", {})
    sent = False

    # --- ntfy.sh (free, no account) ---
    if n.get("ntfy_topic"):
        payload = {
            "topic": n["ntfy_topic"],
            "title": title[:200],
            "message": message[:3500],
            "priority": 4 if high else 3,
            "tags": tags,
        }
        if url:
            payload["click"] = url
        try:
            requests.post("https://ntfy.sh/", json=payload, timeout=20)
            sent = True
        except Exception as e:
            print("  ntfy error:", e)

    # --- Telegram (optional, off by default) ---
    if n.get("telegram_token") and n.get("telegram_chat"):
        text = f"<b>{title}</b>\n{message}"
        if url:
            text += f'\n<a href="{url}">Open deal</a>'
        try:
            requests.post(
                f"https://api.telegram.org/bot{n['telegram_token']}/sendMessage",
                json={"chat_id": n["telegram_chat"], "text": text,
                      "parse_mode": "HTML"},
                timeout=20,
            )
            sent = True
        except Exception as e:
            print("  telegram error:", e)

    return sent


# -------------------------------------------------------------- scoring
def strip_html(s):
    return re.sub(r"\s+", " ", HTML_TAG.sub(" ", s or "")).strip()


def score_item(text, cfg, extra_keywords=()):
    """Rate an item 0-100. Higher = more likely to be a real deal."""
    t = text.lower()

    if any(k.lower() in t for k in cfg.get("kill_keywords", [])):
        return 0

    s = 30
    for k in list(cfg.get("boost_keywords", [])) + list(extra_keywords):
        if k.lower() in t:
            s += 12
    if any(a.lower() in t for a in cfg.get("home_airports", [])):
        s += 25
    if "business" in t or "first class" in t:
        s += 10

    for raw in PRICE_RE.findall(text):
        try:
            price = int(raw.replace(",", ""))
        except ValueError:
            continue
        if 40 <= price <= cfg.get("max_alert_price", 400):
            s += 20
        if price < 200:
            s += 10
        break

    return min(s, 100)


# ---------------------------------------------------------- engine one
def run_feeds(cfg, state):
    """Watch deal feeds. Free, no API key, runs every cycle."""
    first_run = len(state["seen"]) == 0
    threshold = 75 if first_run else cfg.get("min_feed_score", 55)
    count = 0

    groups = [
        ("flight", cfg.get("feeds", []), ()),
        ("cruise", cfg.get("cruise_feeds", []), tuple(cfg.get("cruise_keywords", []))),
    ]

    for _label, feeds, extra in groups:
        for feed in feeds:
            try:
                parsed = feedparser.parse(feed["url"], request_headers=USER_AGENT)
            except Exception as e:
                print(f"  [skip] {feed['name']}: {e}")
                continue

            print(f"  [{feed['name']}] {len(parsed.entries)} entries")

            for entry in parsed.entries[:40]:
                raw_id = entry.get("id") or entry.get("link") or entry.get("title", "")
                uid = hashlib.sha1(raw_id.encode()).hexdigest()

                if uid in state["seen"]:
                    continue
                state["seen"].append(uid)

                title = strip_html(entry.get("title", ""))
                body = strip_html(entry.get("summary", ""))[:500]
                link = entry.get("link", "")

                s = score_item(f"{title} {body}", cfg, extra) + feed.get("weight", 0) // 2

                if s >= threshold:
                    notify(cfg, f"[{s}] {feed['name']}", f"{title}\n\n{body}",
                           url=link, high=(s >= 85),
                           tags=["rotating_light"] if s >= 85 else ["airplane"])
                    count += 1
                    print(f"    ALERT ({s}) {title}")

    state["seen"] = state["seen"][-3000:]
    return count


# ---------------------------------------------------------- engine two
def run_prices(cfg, state):
    """Check prices on your routes and flag big drops. Needs an API token."""
    token = os.getenv("TRAVELPAYOUTS_TOKEN")
    if not token:
        print("  (no TRAVELPAYOUTS_TOKEN set - skipping price engine)")
        return 0

    hits = 0
    for origin in cfg.get("home_airports", []):
        for dest in cfg.get("watch_routes", []):
            if origin == dest:
                continue
            for month in cfg.get("months", []):
                key = f"{origin}-{dest}-{month}"
                try:
                    r = requests.get(
                        "https://api.travelpayouts.com/aviasales/v3/prices_for_dates",
                        params={"origin": origin, "destination": dest,
                                "departure_at": month, "one_way": "true",
                                "currency": "usd", "sorting": "price",
                                "limit": 10, "token": token,
                                "show_to_affiliates": "true"},
                        timeout=25,
                    )
                    r.raise_for_status()
                    data = r.json()
                except Exception as e:
                    print(f"  [skip] {key}: {e}")
                    continue

                if not data.get("success"):
                    continue

                prices = [int(o["price"]) for o in data.get("data", []) if o.get("price")]
                if not prices:
                    continue

                best = min(prices)
                history = state["prices"].setdefault(key, [])

                # Check BEFORE recording, so the average is clean history
                if len(history) >= MIN_HISTORY:
                    median = statistics.median(history)
                    if median > 0:
                        drop = (median - best) / median

                        if drop >= cfg.get("mistake_fare_threshold", 0.45):
                            notify(cfg, f"MISTAKE FARE? {origin} -> {dest}",
                                   f"${best} (normally around ${median:.0f}, "
                                   f"{drop:.0%} below)\nDeparting {month}",
                                   high=True, tags=["rotating_light"])
                            hits += 1
                            print(f"    !! {key} ${best} vs median ${median:.0f}")

                        elif (drop >= cfg.get("drop_threshold", 0.25)
                              and best <= cfg.get("max_alert_price", 400)):
                            notify(cfg, f"Good deal {origin} -> {dest}",
                                   f"${best} (normally around ${median:.0f}, "
                                   f"{drop:.0%} below)\nDeparting {month}")
                            hits += 1

                history.append(best)
                state["prices"][key] = history[-200:]

    return hits


# ----------------------------------------------------------------- main
def main():
    cfg = load_config()
    state = load_state()

    print("=== FEED ENGINE ===")
    feed_alerts = run_feeds(cfg, state)

    # Price checks use up API calls, so only run them once an hour.
    # The feed engine is free, so it runs every single cycle.
    print("=== PRICE ENGINE ===")
    if datetime.now(timezone.utc).minute < 8:
        price_alerts = run_prices(cfg, state)
    else:
        print("  (not the top of the hour - skipping)")
        price_alerts = 0

    save_state(state)
    print(f"\nFinished {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. "
          f"{feed_alerts} feed alerts, {price_alerts} price alerts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
