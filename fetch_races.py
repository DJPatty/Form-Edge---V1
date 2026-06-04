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

# Optional: FormFav enrichment (career/distance/condition/form-trend stats).
FORMFAV_KEY   = os.environ.get("FORMFAV_API_KEY")
FF_BASE       = "https://api.formfav.com/v1"
FF_RESERVE    = 60     # keep this many daily calls in reserve (never spend the last 60)
FF_CACHE_DAYS = 4      # reuse a cached horse profile for this many days (no API call)


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


# ---- FormFav enrichment (cache-first + budget-aware) ----
def clean_name(n):
    """Betfair sometimes prefixes a number ('1. Via Sistina') or suffixes a country ('(NZ)')."""
    if not n:
        return ""
    n = re.sub(r"^\s*\d+\.\s*", "", n)        # drop leading "1. "
    n = re.sub(r"\s*\([A-Z]{2,3}\)\s*$", "", n)  # drop trailing "(NZ)"
    return n.strip()


def _ff_remaining(resp, prev):
    try:
        v = resp.headers.get("X-RateLimit-Remaining-Day")
        return int(v) if v is not None else prev
    except Exception:
        return prev


def enrich_formfav(sb, race_rows, runner_rows):
    if not FORMFAV_KEY:
        return
    # 1) preload the cache by name (no API calls) — reuse profiles fetched recently
    cache = {}
    try:
        rows = sb.table("formfav_cache").select("runner_id,name,stats,fetched_at").execute().data or []
    except Exception as e:
        log(f"FormFav cache read failed: {e}")
        rows = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=FF_CACHE_DAYS)
    for r in rows:
        try:
            fa = datetime.fromisoformat(str(r["fetched_at"]).replace("Z", "+00:00"))
        except Exception:
            fa = None
        if r.get("name") and fa and fa > cutoff:
            cache[r["name"].strip().lower()] = {"id": r["runner_id"], "stats": r["stats"]}

    # 2) enrich soonest-jumping races first so the races you'll look at get covered
    start_by_race = {x["id"]: (x.get("start_time") or "9999") for x in race_rows}
    code_by_race = {x["id"]: x.get("code") for x in race_rows}
    order = sorted(runner_rows, key=lambda rr: start_by_race.get(rr["race_id"], "9999"))

    headers = {"X-API-Key": FORMFAV_KEY}
    remaining = None
    seen, enriched, calls = {}, 0, 0

    for rr in order:
        nm = clean_name(rr.get("name"))
        if not nm or len(nm) < 3:
            continue
        key = nm.lower()
        prof = seen.get(key) or cache.get(key)
        if prof:                                  # cached — 0 API calls
            rr["formfav_id"] = prof.get("id")
            rr["formfav"] = prof.get("stats")
            continue
        if remaining is not None and remaining <= FF_RESERVE:   # protect the reserve
            continue
        code = "harness" if code_by_race.get(rr["race_id"]) == "harness" else "gallops"
        try:
            s = requests.get(f"{FF_BASE}/stats/runner/search",
                             params={"q": nm, "race_code": code, "country": "au"},
                             headers=headers, timeout=20)
            remaining = _ff_remaining(s, remaining)
            if s.status_code == 429:
                log("FormFav rate limit reached — pausing enrichment for this run.")
                break
            s.raise_for_status()
            results = (s.json() or {}).get("results") or []
            calls += 1
            if not results:
                seen[key] = {"id": None, "stats": None}
                continue
            rid = results[0]["runnerId"]
            st = requests.get(f"{FF_BASE}/stats/runner/{rid}", headers=headers, timeout=20)
            remaining = _ff_remaining(st, remaining)
            if st.status_code == 429:
                log("FormFav rate limit reached — pausing enrichment for this run.")
                break
            st.raise_for_status()
            stats = st.json()
            calls += 1
            seen[key] = {"id": rid, "stats": stats}
            rr["formfav_id"] = rid
            rr["formfav"] = stats
            try:
                sb.table("formfav_cache").upsert(
                    {"runner_id": rid, "name": nm, "stats": stats,
                     "fetched_at": datetime.now(timezone.utc).isoformat()},
                    on_conflict="runner_id").execute()
            except Exception as e:
                log(f"FormFav cache write failed {rid}: {e}")
            enriched += 1
        except Exception as e:
            log(f"FormFav enrich failed for {nm}: {e}")
            seen[key] = {"id": None, "stats": None}

    cached_hits = sum(1 for rr in runner_rows if rr.get("formfav") is not None) - enriched
    log(f"FormFav: {enriched} fetched, ~{max(0,cached_hits)} from cache, {calls} API calls, remaining≈{remaining}")


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
            "place_market_id": pmid,
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
                "formfav_id": None,
                "formfav": None,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                # NOTE: win_strike / dist_suit etc. left out so hand-entered values survive.
            })

    enrich_formfav(sb, race_rows, runner_rows)

    sb.table("races").upsert(race_rows, on_conflict="id").execute()
    sb.table("runners").upsert(runner_rows, on_conflict="race_id,selection_id").execute()
    log(f"Wrote {len(race_rows)} races and {len(runner_rows)} runners to Supabase.")

    # 4b. Capture results for races that have now settled.
    settle_results(sb, token)

    # AI reads are now generated on demand by server.py when you tap "Get the read".
    # (The generate_ai_views function below is kept but no longer called automatically.)


# ---- 4b. Results capture: ask Betfair who won, store winner + placed set ----
def settle_results(sb, token):
    """For races we already have that aren't resulted yet and have started,
    query Betfair for the settled WIN (winner) and PLACE (placed set) markets."""
    try:
        rows = sb.table("races").select("id,place_market_id,start_time,result") \
                 .is_("result", "null").execute().data or []
    except Exception:
        # place_market_id column may not exist; fall back to id only
        rows = sb.table("races").select("id,start_time,result").is_("result", "null").execute().data or []

    now = datetime.now(timezone.utc)
    pending = []
    for r in rows:
        st = r.get("start_time")
        try:
            started = st and datetime.fromisoformat(st.replace("Z", "+00:00")) < now
        except Exception:
            started = True
        if started:
            pending.append(r)
    if not pending:
        return

    # Map win market id -> place market id (if we stored it)
    win_ids = [r["id"] for r in pending]
    place_of = {r["id"]: r.get("place_market_id") for r in pending}
    place_ids = [v for v in place_of.values() if v]

    def book(ids):
        out = {}
        for i in range(0, len(ids), 25):
            chunk = ids[i:i + 25]
            body = {"marketIds": chunk, "priceProjection": {"priceData": []}}
            resp = requests.post(f"{BETTING}/listMarketBook/", json=body,
                                 headers=bf_headers(token), proxies=PROXIES, timeout=30)
            resp.raise_for_status()
            for mb in resp.json():
                out[mb["marketId"]] = mb
        return out

    wins = book(win_ids)
    places = book(place_ids) if place_ids else {}

    made = 0
    for r in pending:
        mb = wins.get(r["id"])
        if not mb or mb.get("status") != "CLOSED":
            continue  # not settled yet
        winner = None
        for run in mb.get("runners", []):
            if run.get("status") == "WINNER":
                winner = run.get("selectionId"); break
        if winner is None:
            continue
        placed = []
        pm = places.get(place_of.get(r["id"]))
        if pm and pm.get("status") == "CLOSED":
            placed = [run.get("selectionId") for run in pm.get("runners", [])
                      if run.get("status") == "WINNER"]
        result = {"winner": winner, "placed": placed,
                  "settled_at": now.isoformat()}
        sb.table("races").update({"result": result}).eq("id", r["id"]).execute()
        made += 1
    if made:
        log(f"Recorded results for {made} settled race(s).")


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
