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
  ANTHROPIC_API_KEY      <- (optional) enables the AI race reads
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

# Optional: AI race reads (cached per race). If unset, the app just shows no read.
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
AI_MODEL      = "claude-haiku-4-5-20251001"


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
    window = {
        "from": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    def call(types, projection, mx):
        body = {
            "filter": {
                "eventTypeIds": ["7"],            # 7 = Horse Racing
                "marketCountries": ["AU"],
                "marketTypeCodes": types,
                "marketStartTime": window,
            },
            "marketProjection": projection,
            "maxResults": mx,
            "sort": "FIRST_TO_START",
        }
        r = requests.post(f"{BETTING}/listMarketCatalogue/", json=body,
                          headers=bf_headers(token), proxies=PROXIES, timeout=30)
        r.raise_for_status()
        return r.json()

    # WIN markets carry the heavy runner metadata (jockey, weight, form, cloth)
    win = call(["WIN"], ["EVENT", "MARKET_START_TIME", "MARKET_DESCRIPTION",
                         "RUNNER_DESCRIPTION", "RUNNER_METADATA"], 200)
    # PLACE markets only need enough to match to a race + read prices later
    place = call(["PLACE"], ["EVENT", "MARKET_START_TIME", "MARKET_DESCRIPTION"], 200)
    log(f"Found {len(win)} win and {len(place)} place markets.")
    return win + place


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


def discipline(*names):
    """Harness races carry 'pace' or 'trot' in the name; everything else is gallops."""
    blob = " ".join(n.lower() for n in names if n)
    return "harness" if (" pace" in blob or "trot" in blob or blob.endswith("pace")) else "gallops"


# ---- 4. transform + write to Supabase ----
def main():
    token = login()
    markets = list_markets(token)
    if not markets:
        log("No markets right now — nothing to do.")
        return

    prices = get_prices(token, [m["marketId"] for m in markets])
    sb = create_client(SB_URL, SB_KEY)

    # separate WIN (the races) from PLACE (extra odds), match place to win by event+jump time
    def mtype(m):
        return (m.get("description") or {}).get("marketType")
    win_mkts = [m for m in markets if mtype(m) == "WIN"]
    place_mid = {}
    for pm in markets:
        if mtype(pm) == "PLACE":
            key = ((pm.get("event") or {}).get("id"), pm.get("marketStartTime"))
            place_mid[key] = pm["marketId"]

    race_rows, runner_rows = [], []
    for m in win_mkts:
        mid = m["marketId"]
        name = m.get("marketName", "")
        event = m.get("event", {})
        pkey = ((event or {}).get("id"), m.get("marketStartTime"))
        pmid = place_mid.get(pkey)
        race_rows.append({
            "id": mid,
            "track": event.get("venue") or event.get("name"),
            "race_number": parse_int(name, r"R(\d+)"),
            "distance": parse_int(name, r"(\d+)m"),
            "code": discipline(name, event.get("name")),
            "start_time": m.get("marketStartTime"),
            "status": "open",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        })
        for run in m.get("runners", []):
            meta = run.get("metadata", {}) or {}
            sel = run["selectionId"]
            runner_rows.append({
                "race_id": mid,
                "selection_id": sel,
                "name": run.get("runnerName"),
                "cloth": parse_int(meta.get("CLOTH_NUMBER"), r"(\d+)"),
                "barrier": parse_int(meta.get("STALL_DRAW"), r"(\d+)"),
                "weight": to_num(meta.get("WEIGHT_VALUE")),
                "jockey": meta.get("JOCKEY_NAME"),
                "form": meta.get("FORM"),
                "days_since": parse_int(meta.get("DAYS_SINCE_LAST_RUN"), r"(\d+)"),
                "odds": prices.get((mid, sel)),
                "place_odds": prices.get((pmid, sel)) if pmid else None,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                # NOTE: win_strike / dist_suit etc. left out so hand-entered values survive.
            })

    sb.table("races").upsert(race_rows, on_conflict="id").execute()
    sb.table("runners").upsert(runner_rows, on_conflict="race_id,selection_id").execute()
    log(f"Wrote {len(race_rows)} races and {len(runner_rows)} runners to Supabase.")

    # AI reads are now generated on demand by server.py when you tap "Get the read".
    # (The generate_ai_views function below is kept but no longer called automatically.)


# ---- 5. AI race reads (cached, cheap, server-side) ----
def generate_ai_views(sb, race_rows, runner_rows):
    if not ANTHROPIC_KEY:
        return
    existing = sb.table("races").select("id").not_.is_("ai_view", "null").execute()
    have = {r["id"] for r in (existing.data or [])}
    now = datetime.now(timezone.utc)
    by_race = {}
    for r in runner_rows:
        by_race.setdefault(r["race_id"], []).append(r)

    made = 0
    for race in race_rows:
        mid = race["id"]
        if mid in have:                       # already has a read — don't pay again
            continue
        try:
            st = datetime.fromisoformat(race["start_time"].replace("Z", "+00:00")) if race.get("start_time") else None
        except Exception:
            st = None
        if st and (st - now).total_seconds() > 4 * 3600:   # only races within 4h, to save cost
            continue
        rs = sorted([x for x in by_race.get(mid, []) if x.get("odds")], key=lambda x: x["odds"])[:8]
        if len(rs) < 2:
            continue
        lines = "\n".join(f"- {x['name']} ${x['odds']:.2f}, form {x.get('form') or 'NA'}, {x.get('jockey') or 'NA'}" for x in rs)
        prompt = ("You are a fun but honest Australian racing form analyst. In TWO short sentences, give a punter's read on "
                  "this race: name the market favourite as the likely winner and one each-way chance. Keep it light, use at "
                  "most one emoji, never guarantee a result, and do not give betting advice.\n\n"
                  f"{race.get('track')} R{race.get('race_number')} {race.get('distance')}m:\n{lines}")
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
                json={"model": AI_MODEL, "max_tokens": 160, "messages": [{"role": "user", "content": prompt}]},
                timeout=30,
            )
            resp.raise_for_status()
            txt = "".join(b.get("text", "") for b in resp.json().get("content", [])).strip()
            if txt:
                sb.table("races").update({"ai_view": txt}).eq("id", mid).execute()
                made += 1
        except Exception as e:
            log(f"AI read skipped for {mid}: {e}")
    if made:
        log(f"Generated {made} AI race reads.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ERROR: {e}")
        sys.exit(1)
