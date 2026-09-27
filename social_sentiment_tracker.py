"""
social-sentiment-tracker
--------------------------
Polls a public X (Twitter) account's recent tweets and uses Gemini to flag
any mentions of tickers, coin names, or trending internet culture references,
logging the analysis to CSV and JSON for later review.

This is a READ-ONLY analytics tool. It does not place trades, does not touch
any wallet, and does not post or reply on your behalf. Output is meant to be
reviewed by a person, not acted on automatically.

Important limitations (read before using):
- X's free API tier only allows occasional polling with strict rate limits.
  A true low-latency stream of one account's tweets requires a paid API tier
  (Basic/Pro, currently ~$100+/month at X). This script uses polling and is
  built to respect free-tier limits by checking infrequently (default: every
  15 minutes). Lower CHECK_INTERVAL_SECONDS only if you have a paid tier.
- Gemini's output here is a descriptive analysis, not financial advice. Do
  not treat "trending" flags as buy/sell signals.

Setup before running:
    1. Get X API credentials (Bearer Token) at https://developer.x.com
       -> create a project/app -> generate a Bearer Token.
    2. Get a free Gemini API key at https://aistudio.google.com/apikey
    3. Fill in X_BEARER_TOKEN, TARGET_USERNAME and GEMINI_API_KEY below
       (or set them as environment variables).

Run:
    python social_sentiment_tracker.py
The script runs in an endless loop. Stop it with Ctrl+C.
"""

import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

# ======================= CONFIG =======================
X_BEARER_TOKEN = os.environ.get("X_BEARER_TOKEN", "PASTE_YOUR_X_BEARER_TOKEN_HERE")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "PASTE_YOUR_GEMINI_API_KEY_HERE")

TARGET_USERNAME = "elonmusk"        # X handle to watch, without the @
MAX_TWEETS_PER_CHECK = 5            # how many recent tweets to pull each check
CHECK_INTERVAL_SECONDS = 900        # 15 min; lower only with a paid X API tier

GEMINI_MODEL = "gemini-flash-latest"

SEEN_FILE = "seen_tweets.json"      # tracks tweet IDs already analyzed
JSON_LOG_FILE = "sentiment_log.json"
CSV_LOG_FILE = "sentiment_log.csv"
# ========================================================

X_API_BASE = "https://api.x.com/2"
GEMINI_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
)

ANALYSIS_PROMPT = """Analyze this tweet as a social-media researcher, not a financial advisor.

Tweet: "{text}"

Respond ONLY as compact JSON with these fields:
- "mentions_ticker_or_meme": true/false (does it reference a coin, ticker, or a phrase that internet meme culture could turn into one?)
- "candidate_terms": a list of any specific words/phrases that could become meme references (empty list if none)
- "tone": one word describing the tone (e.g. "joking", "serious", "cryptic", "neutral")
- "summary": one plain-language sentence describing what the tweet is about

This is for descriptive research only. Do not include any buy/sell/investment language.
"""


def load_seen_ids() -> set:
    """Loads the set of tweet IDs already analyzed."""
    if not os.path.exists(SEEN_FILE):
        return set()
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (json.JSONDecodeError, OSError):
        return set()


def save_seen_ids(seen_ids: set) -> None:
    """Saves the set of analyzed tweet IDs, keeping only the most recent 1000."""
    trimmed = list(seen_ids)[-1000:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(trimmed, f)


def fetch_recent_tweets(username: str, max_results: int) -> list[dict]:
    """Fetches the user's most recent tweets via the X API v2."""
    headers = {"Authorization": f"Bearer {X_BEARER_TOKEN}"}

    # Look up the numeric user ID from the handle first.
    user_resp = requests.get(f"{X_API_BASE}/users/by/username/{username}", headers=headers, timeout=10)
    if user_resp.status_code == 429:
        raise RuntimeError("X API rate limit hit while looking up the user. Wait before retrying.")
    user_resp.raise_for_status()
    user_id = user_resp.json()["data"]["id"]

    params = {"max_results": max(5, min(max_results, 100)), "tweet.fields": "created_at"}
    tweets_resp = requests.get(
        f"{X_API_BASE}/users/{user_id}/tweets", headers=headers, params=params, timeout=10
    )
    if tweets_resp.status_code == 429:
        raise RuntimeError("X API rate limit hit while fetching tweets. Wait before retrying.")
    tweets_resp.raise_for_status()

    return tweets_resp.json().get("data", [])


def analyze_with_gemini(tweet_text: str) -> dict:
    """Sends the tweet text to Gemini and parses its JSON analysis. Falls back to a
    neutral placeholder if the request or parsing fails."""
    fallback = {
        "mentions_ticker_or_meme": False,
        "candidate_terms": [],
        "tone": "unknown",
        "summary": "Analysis unavailable.",
    }

    url = GEMINI_URL_TEMPLATE.format(model=GEMINI_MODEL, key=GEMINI_API_KEY)
    payload = {"contents": [{"parts": [{"text": ANALYSIS_PROMPT.format(text=tweet_text)}]}]}

    try:
        response = requests.post(url, json=payload, timeout=20)
        response.raise_for_status()
        raw_text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        # Gemini sometimes wraps JSON in markdown code fences; strip those if present.
        cleaned = raw_text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(cleaned)
    except (requests.RequestException, KeyError, IndexError, json.JSONDecodeError) as e:
        print(f"Gemini analysis failed, logging a neutral placeholder: {e}", file=sys.stderr)
        return fallback


def append_to_json_log(entry: dict) -> None:
    """Appends one analysis entry to the JSON log file."""
    log = []
    if os.path.exists(JSON_LOG_FILE):
        try:
            with open(JSON_LOG_FILE, "r", encoding="utf-8") as f:
                log = json.load(f)
        except (json.JSONDecodeError, OSError):
            log = []
    log.append(entry)
    with open(JSON_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)


def append_to_csv_log(entry: dict) -> None:
    """Appends one analysis entry as a row in the CSV log file."""
    file_exists = os.path.exists(CSV_LOG_FILE)
    with open(CSV_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(entry.keys()))
        if not file_exists:
            writer.writeheader()
        writer.writerow(entry)


def check_once() -> None:
    """One check: fetch recent tweets, analyze any new ones, log the results."""
    seen_ids = load_seen_ids()
    tweets = fetch_recent_tweets(TARGET_USERNAME, MAX_TWEETS_PER_CHECK)

    new_tweets = [t for t in tweets if t["id"] not in seen_ids]
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {len(tweets)} tweets fetched, {len(new_tweets)} new.")

    for tweet in new_tweets:
        analysis = analyze_with_gemini(tweet["text"])
        entry = {
            "tweet_id": tweet["id"],
            "created_at": tweet.get("created_at", ""),
            "text": tweet["text"].replace("\n", " "),
            "mentions_ticker_or_meme": analysis.get("mentions_ticker_or_meme", False),
            "candidate_terms": "; ".join(analysis.get("candidate_terms", [])),
            "tone": analysis.get("tone", ""),
            "summary": analysis.get("summary", ""),
            "logged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        append_to_json_log(entry)
        append_to_csv_log(entry)
        print(f"Logged tweet {tweet['id']}: {entry['summary']}")
        seen_ids.add(tweet["id"])

    save_seen_ids(seen_ids)


def config_is_valid() -> bool:
    """Checks that required config values were filled in before starting."""
    placeholders = ("PASTE_YOUR_X_BEARER_TOKEN_HERE", "PASTE_YOUR_GEMINI_API_KEY_HERE")
    if X_BEARER_TOKEN in placeholders or GEMINI_API_KEY in placeholders:
        print("Set X_BEARER_TOKEN and GEMINI_API_KEY at the top of this file before running.", file=sys.stderr)
        return False
    return True


def main() -> None:
    if not config_is_valid():
        sys.exit(1)

    print(f"Watching @{TARGET_USERNAME} every {CHECK_INTERVAL_SECONDS} sec. Press Ctrl+C to stop.")
    print("This tool logs descriptive analysis only — it does not trade or post automatically.")

    while True:
        try:
            check_once()
        except RuntimeError as e:
            print(f"{e}", file=sys.stderr)
        except requests.RequestException as e:
            print(f"Network error while contacting the X API: {e}", file=sys.stderr)

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
