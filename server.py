"""
Form Edge — on-demand AI read service (Flask, for Railway)
----------------------------------------------------------
A tiny web service. The app's "Get the read" button POSTs a race_id here;
this generates a Haiku read, caches it in Supabase, and returns it.
Keeps your Anthropic key safe on the server (never in the web page).

Run on Railway as a WEB service (not cron):
  start command:  python server.py
  then Settings -> Networking -> Generate Domain to get its public URL.

Environment variables (Railway -> Variables):
  ANTHROPIC_API_KEY     <- your Anthropic key
  SUPABASE_URL          <- Project URL
  SUPABASE_SERVICE_KEY  <- service_role key (secret)
"""

import os
import requests
from flask import Flask, request, jsonify
from supabase import create_client

SB_URL        = os.environ["SUPABASE_URL"]
SB_KEY        = os.environ["SUPABASE_SERVICE_KEY"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
AI_MODEL      = "claude-haiku-4-5-20251001"   # Haiku 4.5

app = Flask(__name__)
sb = create_client(SB_URL, SB_KEY)


def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"   # lock to your Netlify domain later if you like
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return resp


@app.route("/read", methods=["POST", "OPTIONS"])
def read():
    if request.method == "OPTIONS":
        return cors(app.make_response(("", 204)))

    body = request.get_json(force=True, silent=True) or {}
    rid = str(body.get("race_id", ""))
    force = bool(body.get("force"))
    if not rid:
        return cors(jsonify(error="missing race_id")), 400

    race = sb.table("races").select("*").eq("id", rid).limit(1).execute()
    if not race.data:
        return cors(jsonify(error="race not found")), 404
    r0 = race.data[0]

    # already generated? return the cached one (free)
    if r0.get("ai_view") and not force:
        return cors(jsonify(ai_view=r0["ai_view"], cached=True))

    runners = sb.table("runners").select("*").eq("race_id", rid).execute().data or []
    rs = sorted([x for x in runners if x.get("odds")], key=lambda x: x["odds"])[:8]
    if len(rs) < 2:
        return cors(jsonify(error="not enough priced runners")), 422

    lines = "\n".join(f"- {x['name']} ${x['odds']:.2f}, form {x.get('form') or 'NA'}, {x.get('jockey') or 'NA'}" for x in rs)
    prompt = ("You are a fun but honest Australian racing form analyst. In TWO short sentences, give a punter's read on this "
              "race: name the market favourite as the likely winner and one each-way chance. Keep it light, use at most one "
              "emoji, never guarantee a result, and do not give betting advice.\n\n"
              f"{r0.get('track')} R{r0.get('race_number')} {r0.get('distance')}m:\n{lines}")
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
            json={"model": AI_MODEL, "max_tokens": 160, "messages": [{"role": "user", "content": prompt}]},
            timeout=30,
        )
        resp.raise_for_status()
        txt = "".join(b.get("text", "") for b in resp.json().get("content", [])).strip()
    except Exception as e:
        return cors(jsonify(error=str(e))), 502

    if txt:
        sb.table("races").update({"ai_view": txt}).eq("id", rid).execute()
    return cors(jsonify(ai_view=txt, cached=False))


@app.route("/")
def health():
    return "Form Edge read service is running."


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
