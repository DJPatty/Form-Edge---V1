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
import time
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
FF_RESERVE    = 500    # keep this many of the 20,000 daily calls in reserve
FF_CACHE_DAYS = 10     # reuse a cached horse profile for this many days (no API call)
FF_TOP_N      = 20     # enrich up to this many runners by market per race (covers ~whole field; quota is ample)
FF_MAX_CALLS_PER_RUN = 700  # per-run budget — big, because 20k/day is far more than we need (paced, so it won't melt the API)
FF_EVERY_HOURS = 3          # how often FormFav enrichment ACTUALLY runs (odds still refresh every run)
FF_HORIZON_HOURS = 12       # only enrich races jumping within this many hours (focuses quota on a big card)
FF_PACE_SEC   = 0.15        # FormFav confirmed NO per-second limit; light pace just to be polite
FF_RETRIES    = 4           # retries per call on timeout/5xx (cold-start: server spinning up)
FF_BACKOFF_SEC = 6          # base backoff between retries (grows: 6s, 12s, 18s...) to let a cold server warm
FF_RUN_MARKER  = -1         # reserved formfav_cache.runner_id used to remember the last enrichment time


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
    base = {"eventTypeIds": ["7"], "marketCountries": ["AU"], "marketStartTime": window}

    def cat(body):
        r = requests.post(f"{BETTING}/listMarketCatalogue/", json=body,
                          headers=bf_headers(token), proxies=PROXIES, timeout=30)
        if not r.ok:
            log(f"listMarketCatalogue HTTP {r.status_code}: {(r.text or '')[:300]}")
        r.raise_for_status()
        return r.json()

    def enumerate_ids(types):
        # light projection -> safe to ask for up to 1000 markets at once
        res = cat({"filter": {**base, "marketTypeCodes": types},
                   "marketProjection": ["MARKET_START_TIME"],
                   "maxResults": 1000, "sort": "FIRST_TO_START"})
        return [m["marketId"] for m in res]

    def fetch_meta(ids, projection, chunk):
        # heavy projections must be pulled in small chunks or Betfair returns TOO_MUCH_DATA (400)
        out = []
        for i in range(0, len(ids), chunk):
            part = ids[i:i + chunk]
            out += cat({"filter": {"marketIds": part},
                        "marketProjection": projection,
                        "maxResults": len(part), "sort": "FIRST_TO_START"})
        return out

    # WIN markets carry the heavy runner metadata (jockey, weight, form, cloth) -> chunk by 50
    win_ids = enumerate_ids(["WIN"])
    win = fetch_meta(win_ids, ["EVENT", "MARKET_START_TIME", "MARKET_DESCRIPTION",
                               "RUNNER_DESCRIPTION", "RUNNER_METADATA"], 50)
    # PLACE markets only need enough to match to a race -> light, chunk by 100
    place_ids = enumerate_ids(["PLACE"])
    place = fetch_meta(place_ids, ["EVENT", "MARKET_START_TIME", "MARKET_DESCRIPTION"], 100)
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


def _ff_reset_secs(resp):
    """Best-effort: seconds until the FormFav rate-limit window resets, read from headers.
    Handles Retry-After (seconds or HTTP-date) and common Reset headers (seconds-until or unix epoch)."""
    h = resp.headers
    ra = h.get("Retry-After")
    if ra:
        try:
            return max(0, int(float(ra)))
        except ValueError:
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(ra)
                return max(0, int((dt - datetime.now(timezone.utc)).total_seconds()))
            except Exception:
                pass
    for k in ("RateLimit-Reset", "X-RateLimit-Reset", "X-RateLimit-Reset-Day",
              "RateLimit-Reset-Day", "X-RateLimit-Reset-Seconds"):
        v = h.get(k)
        if v:
            try:
                n = int(float(v))
            except ValueError:
                continue
            if n > 1_000_000_000:   # looks like a unix epoch timestamp
                return max(0, n - int(datetime.now(timezone.utc).timestamp()))
            return max(0, n)        # seconds-until-reset
    return None


def _fmt_secs(s):
    if s is None:
        return "unknown"
    h, m = s // 3600, (s % 3600) // 60
    return f"{s}s (~{h}h {m}m)" if h else f"{s}s (~{m}m)"


# Stats are fetched from the confirmed path /stats/runner/{id}. (Endpoint discovery removed —
# we verified [0] is correct; the 404s were first-starters, the 500s/timeouts were cold starts.)
_ff_stats_builder = lambda rid, rt, c: f"{FF_BASE}/stats/runner/{rid}"


def ff_get(url, headers, params=None, label=""):
    """GET with cold-start-aware retry. FormFav's server can spin down and the first hits
    time out / 500 while it warms up, so we retry with growing backoff (no per-second limit
    exists, only the 20k/day cap). Returns the Response, or None if every attempt failed."""
    for attempt in range(FF_RETRIES):
        try:
            r = requests.get(url, headers=headers, params=params, timeout=20)
        except Exception as e:
            wait = FF_BACKOFF_SEC * (attempt + 1)
            if attempt < FF_RETRIES - 1:
                log(f"FormFav {label} timeout/err (try {attempt+1}/{FF_RETRIES}) — warming, retry in {wait}s")
                time.sleep(wait); continue
            log(f"FormFav {label} failed after {FF_RETRIES} tries: {type(e).__name__}")
            return None
        # 5xx during cold start -> wait and retry; everything else (200/404/429/etc) -> return
        if r.status_code in (500, 502, 503, 504) and attempt < FF_RETRIES - 1:
            wait = FF_BACKOFF_SEC * (attempt + 1)
            log(f"FormFav {label} HTTP {r.status_code} (try {attempt+1}/{FF_RETRIES}) — warming, retry in {wait}s")
            time.sleep(wait); continue
        return r
    return None


def ff_stats(rid, rt, c, headers):
    global _ff_stats_builder
    return ff_get(_ff_stats_builder(rid, rt, c), headers, label=f"stats {rid}")


def formfav_due(sb):
    """True if FormFav enrichment hasn't run within FF_EVERY_HOURS. Uses a reserved
    marker row in formfav_cache (runner_id = -1) so no extra table is needed."""
    if not FORMFAV_KEY:
        return False
    if os.environ.get("FORMFAV_FORCE") == "1":
        log("FormFav: FORMFAV_FORCE=1 set — forcing enrichment this run (remove the var after testing).")
        return True
    try:
        rows = (sb.table("formfav_cache").select("fetched_at")
                  .eq("runner_id", FF_RUN_MARKER).limit(1).execute().data) or []
        if not rows:
            return True
        last = datetime.fromisoformat(str(rows[0]["fetched_at"]).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - last) >= timedelta(hours=FF_EVERY_HOURS)
    except Exception as e:
        log(f"FormFav due-check failed ({e}) — running enrichment to be safe.")
        return True


def mark_formfav_run(sb):
    try:
        sb.table("formfav_cache").upsert(
            {"runner_id": FF_RUN_MARKER, "name": "__formfav_last_run__", "stats": {},
             "fetched_at": datetime.now(timezone.utc).isoformat()},
            on_conflict="runner_id").execute()
    except Exception as e:
        log(f"FormFav run-marker write failed: {e}")


def enrich_formfav(sb, race_rows, runner_rows, allow_api=True):
    if not FORMFAV_KEY:
        log("FormFav: FORMFAV_API_KEY is NOT set in this environment — skipping enrichment. "
            "Set it in Railway -> Variables, then redeploy.")
        # Note: with no key we can't even read cache meaningfully; leave rows as-is.
        return
    if allow_api:
        log(f"FormFav: key present (len={len(FORMFAV_KEY)}), API enrichment active via {FF_BASE}")
    else:
        log(f"FormFav: cache-only this run (API enrichment runs every {FF_EVERY_HOURS}h); applying stored profiles.")
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

    # Only spend API calls on the top FF_TOP_N by market in each race — skip the no-hopers.
    # And only on races jumping within FF_HORIZON_HOURS — a 100-race Saturday won't fit one
    # day's quota, so focus on what's coming up; far races get enriched on a later run.
    # (Cached profiles still get applied to everyone for free below.)
    horizon = (datetime.now(timezone.utc) + timedelta(hours=FF_HORIZON_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    by_race = {}
    for rr in runner_rows:
        st = start_by_race.get(rr["race_id"], "9999")
        if st < now_iso or st > horizon:        # past, or too far away to bother now
            continue
        by_race.setdefault(rr["race_id"], []).append(rr)
    eligible = set()
    for rid, rs in by_race.items():
        ranked = sorted([x for x in rs if x.get("odds")], key=lambda x: x["odds"])[:FF_TOP_N]
        for x in ranked:
            eligible.add((x["race_id"], x["selection_id"]))
    if allow_api:
        log(f"FormFav: {len(by_race)} races within {FF_HORIZON_HOURS}h, {len(eligible)} runners eligible for API this cycle.")

    headers = {"X-API-Key": FORMFAV_KEY}
    remaining = None
    seen, enriched, calls = {}, 0, 0
    probed = False
    api_blocked = not allow_api   # cache hits ALWAYS apply below; this only gates API calls

    for rr in order:
        nm = clean_name(rr.get("name"))
        if not nm or len(nm) < 3:
            continue
        key = nm.lower()
        prof = seen.get(key) or cache.get(key)
        if prof:                                  # cached — 0 API calls, always applied
            rr["formfav_id"] = prof.get("id")
            rr["formfav"] = prof.get("stats")
            continue
        if api_blocked:                           # quota/auth dead — skip API, but keep looping for cache hits
            continue
        if (rr["race_id"], rr["selection_id"]) not in eligible:   # outsider — don't spend a call
            continue
        if calls >= FF_MAX_CALLS_PER_RUN:         # per-run budget spent; rest will come next run/cache
            continue
        if remaining is not None and remaining <= FF_RESERVE:   # protect the reserve
            continue
        code = "harness" if code_by_race.get(rr["race_id"]) == "harness" else "gallops"
        try:
            time.sleep(FF_PACE_SEC)
            s = ff_get(f"{FF_BASE}/stats/runner/search", headers,
                       params={"q": nm, "race_code": code, "country": "au"}, label=f"search '{nm}'")
            if s is None:                       # all retries failed (server still cold) — skip, try next cycle
                seen[key] = {"id": None, "stats": None}
                continue
            remaining = _ff_remaining(s, remaining)
            if not probed:   # one-time diagnostic so the logs show exactly what FormFav returns
                rl = {k: v for k, v in s.headers.items()
                      if "ratelimit" in k.lower() or "retry-after" in k.lower()}
                snippet = (s.text or "")[:140].replace("\n", " ")
                log(f"FormFav probe: GET search -> HTTP {s.status_code}; remaining≈{remaining}; "
                    f"reset≈{_fmt_secs(_ff_reset_secs(s))}; rate-headers={rl}; body[:140]={snippet}")
                probed = True
            if s.status_code in (401, 403):
                log(f"FormFav AUTH FAILED (HTTP {s.status_code}) — key rejected. No more API calls this run.")
                api_blocked = True
                continue
            if s.status_code == 429:
                log(f"FormFav DAILY quota exhausted (HTTP 429, remaining≈{remaining}). "
                    f"Resets in ~{_fmt_secs(_ff_reset_secs(s))}. Stopping API for this run.")
                api_blocked = True
                continue
            if s.status_code != 200:
                log(f"FormFav search HTTP {s.status_code} for '{nm}' — skipping.")
                seen[key] = {"id": None, "stats": None}
                continue
            results = (s.json() or {}).get("results") or []
            calls += 1
            if not results:
                seen[key] = {"id": None, "stats": None}
                continue
            rid = results[0].get("runnerId") or results[0].get("id")
            if not rid:
                log(f"FormFav: search result for '{nm}' had no runnerId — API shape may have changed: {str(results[0])[:140]}")
                seen[key] = {"id": None, "stats": None}
                continue
            rt = results[0].get("raceType") or "R"
            ctry = results[0].get("country") or "au"
            starts = results[0].get("totalStarts")
            if not results[0].get("lastRaceDate") and (starts in (None, 0)):
                # first-starter / no race history -> FormFav has no stats page (always 404). Don't waste a call.
                seen[key] = {"id": rid, "stats": None}
                continue
            time.sleep(FF_PACE_SEC)
            st = ff_stats(rid, rt, ctry, headers)
            if st is None:
                seen[key] = {"id": rid, "stats": None}
                continue
            remaining = _ff_remaining(st, remaining)
            if st.status_code in (401, 403):
                log(f"FormFav AUTH FAILED on stats call (HTTP {st.status_code}) — no more API calls this run.")
                api_blocked = True
                continue
            if st.status_code == 429:
                log(f"FormFav DAILY quota exhausted on stats call — resets in ~{_fmt_secs(_ff_reset_secs(st))}. Stopping API.")
                api_blocked = True
                continue
            if st.status_code != 200:
                log(f"FormFav stats HTTP {st.status_code} for runnerId {rid} — skipping (transient or no stats).")
                seen[key] = {"id": rid, "stats": None}
                continue
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
            log(f"FormFav enrich failed for {nm}: {type(e).__name__}: {e}")
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

    # FormFav: ALWAYS apply cached profiles (free, no API) so we never wipe formfav on save.
    # Only the live API fetching is gated to every few hours, so the 1-2 min odds cron
    # doesn't burn the daily quota.
    due = formfav_due(sb)
    enrich_formfav(sb, race_rows, runner_rows, allow_api=due)
    if due:
        mark_formfav_run(sb)

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
