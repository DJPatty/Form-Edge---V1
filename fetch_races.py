"""
Form Edge — Betfair -> Supabase fetch script
---------------------------------------------
Runs once, pulls today's Australian horse races + delayed odds from Betfair,
and upserts them into your Supabase `races` and `runners` tables.

Schedule it on Railway (e.g. every 30 min). It's idempotent — safe to run
as often as you like. It refreshes feed facts (odds, jockey, weight, barrier,
form) but never touches the rating fields you fill in by hand.

Set these as environment variables in Railway (NOT in the code):
  BETFAIR_APP_KEY        <- your -Delay app key
  BETFAIR_USERNAME       <- your Betfair login email
  BETFAIR_PASSWORD       <- your Betfair password
  BETFAIR_CERT           <- base64 of client-2048.crt (from cert_base64.txt)
  BETFAIR_KEY            <- base64 of client-2048.key (from key_base64.txt)
  BETFAIR_PROXY          <- http://user:pass@host:port  (an Australian proxy)
  SUPABASE_URL           <- Project Settings -> API -> Project URL
  SUPABASE_SERVICE_KEY   <- Project Settings -> API -> service_role key (secret!)
"""

import os
import re
import sys
import base64
import tempfile
import requests
from datetime import datetime, timedelta, timezone
from supabase import create_client

# ---- config from environment ----
APP_KEY   = os.environ["BETFAIR_APP_KEY"]
USERNAME  = os.environ["BETFAIR_USERNAME"]
PASSWORD  = os.environ["BETFAIR_PASSWORD"]
SB_URL    = os.environ["SUPABASE_URL"]
SB_KEY    = os.environ["SUPABASE_SERVICE_KEY"]
CERT_PEM  = base64.b64decode(os.environ["BETFAIR_CERT"]).decode()  # base64 of client-2048.crt
KEY_PEM   = base64.b64decode(os.environ["BETFAIR_KEY"]).decode()   # base64 of client-2048.key

LOGIN_URL = "https://identitysso-cert.betfair.com/api/certlogin"
BETTING   = "https://api.betfair.com/exchange/betting/rest/v1.0"

# Optional Australian proxy so Betfair sees an AU origin. If unset, calls go direct.
_PROXY    = os.environ.get("BETFAIR_PROXY")
PROXIES   = {"http": _PROXY, "https": _PROXY} if _PROXY else None


def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


# ---- 1. log in with the certificate, get a session token ----
def login():
    crt = tempfile.NamedTemporaryFile("w", suffix=".crt", delete=False)
    crt.write(CERT_PEM); crt.close()
    key = tempfile.NamedTemporaryFile("w", suffix=".key", delete=False)
    key.write(KEY_PEM); key.close()

    r = requests.post(
        LOGIN_URL,
        data={"username": USERNAME, "password": PASSWORD},
        cert=(crt.name, key.name),
        headers={
            "X-Application": APP_KEY,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        proxies=PROXIES,
        timeout=20,
    )
    r.raise_for_status()
    body = r.json()
    if body.get("loginStatus") != "SUCCESS":
        raise RuntimeError(f"Login failed: {body}")
    log("Logged in to Betfair (certificate).")
    return body["sessionToken"]


def bf_headers(token):
    return {
        "X-Application": APP_KEY,
        "X-Authentication": token,
        "Content-Type": "application/json",
    }


# ---- 2. list today's AU horse-racing WIN markets ----
def list_markets(token):
    now = datetime.now(timezone.utc)
    body = {
        "filter": {
            "eventTypeIds": ["7"],            # 7 = Horse Racing
            "marketCountries": ["AU"],
            "marketTypeCodes": ["WIN"],
            "marketStartTime": {
                "from": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "to": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        },
        "marketProjection": [
            "EVENT", "MARKET_START_TIME", "MARKET_DESCRIPTION",
            "RUNNER_DESCRIPTION", "RUNNER_METADATA",
        ],
        "maxResults": 200,
        "sort": "FIRST_TO_START",
    }
    r = requests.post(f"{BETTING}/listMarketCatalogue/", json=body,
                      headers=bf_headers(token), proxies=PROXIES, timeout=30)
    r.raise_for_status()
    markets = r.json()
    log(f"Found {len(markets)} AU win markets.")
    return markets


# ---- 3. get best-back odds for those markets (in batches) ----
def get_prices(token, market_ids):
    prices = {}
    for i in range(0, len(market_ids), 25):
        chunk = market_ids[i:i + 25]
        body = {
            "marketIds": chunk,
            "priceProjection": {"priceData": ["EX_BEST_OFFERS"]},
        }
        r = requests.post(f"{BETTING}/listMarketBook/", json=body,
                          headers=bf_headers(token), proxies=PROXIES, timeout=30)
        r.raise_for_status()
        for mb in r.json():
            for run in mb.get("runners", []):
                backs = run.get("ex", {}).get("availableToBack", [])
                if backs:
                    prices[(mb["marketId"], run["selectionId"])] = backs[0]["price"]
    return prices


# ---- helpers to pull fields out of Betfair's data ----
def parse_int(text, pattern):
    if not text:
        return None
    m = re.search(pattern, text)
    return int(m.group(1)) if m else None


def to_num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---- 4. transform + write to Supabase ----
def main():
    token = login()
    markets = list_markets(token)
    if not markets:
        log("No markets right now — nothing to do.")
        return

    prices = get_prices(token, [m["marketId"] for m in markets])
    sb = create_client(SB_URL, SB_KEY)

    race_rows, runner_rows = [], []
    for m in markets:
        mid = m["marketId"]
        name = m.get("marketName", "")
        event = m.get("event", {})
        race_rows.append({
            "id": mid,
            "track": event.get("venue") or event.get("name"),
            "race_number": parse_int(name, r"R(\d+)"),
            "distance": parse_int(name, r"(\d+)m"),
            "start_time": m.get("marketStartTime"),
            "status": "open",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        })
        for run in m.get("runners", []):
            meta = run.get("metadata", {}) or {}
            runner_rows.append({
                "race_id": mid,
                "selection_id": run["selectionId"],
                "name": run.get("runnerName"),
                "barrier": parse_int(meta.get("STALL_DRAW"), r"(\d+)"),
                "weight": to_num(meta.get("WEIGHT_VALUE")),
                "jockey": meta.get("JOCKEY_NAME"),
                "form": meta.get("FORM"),
                "days_since": parse_int(meta.get("DAYS_SINCE_LAST_RUN"), r"(\d+)"),
                "odds": prices.get((mid, run["selectionId"])),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                # NOTE: win_strike / jockey_strike / dist_suit etc. are deliberately
                # left out so your hand-entered values survive every refresh.
            })

    sb.table("races").upsert(race_rows, on_conflict="id").execute()
    sb.table("runners").upsert(runner_rows, on_conflict="race_id,selection_id").execute()
    log(f"Wrote {len(race_rows)} races and {len(runner_rows)} runners to Supabase.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ERROR: {e}")
        sys.exit(1)
