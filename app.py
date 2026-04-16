import os
import time
import threading
from datetime import datetime, date
from flask import Flask, render_template, jsonify
import requests

app = Flask(__name__)

ATG_BASE = "https://www.atg.se/services/racinginfo/v1/api"
HEADERS = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
CACHE_TTL = 3 * 60 * 60  # 3 timmar i sekunder

_cache = {"data": None, "timestamp": 0, "last_updated": None, "error": None}
_lock = threading.Lock()
_refresh_thread_started = False


def format_odds(raw):
    """Omvandlar ATG-odds (ex 531 → 5.31) till decimal."""
    if not raw or raw == 0:
        return "-"
    return f"{raw / 100:.2f}"


def format_pct(numerator, denominator):
    if not denominator or denominator == 0:
        return "-"
    return f"{(numerator / denominator * 100):.0f}%"


def driver_stats(driver):
    """Returnerar (seger%25, top3%25, seger%26, top3%26) som strängar."""
    result = {}
    stats = driver.get("statistics", {}).get("years", {})
    for year in ["2025", "2026"]:
        y = stats.get(year, {})
        starts = y.get("starts", 0)
        pl = y.get("placement", {})
        wins = pl.get("1", 0)
        p1 = pl.get("1", 0)
        p2 = pl.get("2", 0)
        p3 = pl.get("3", 0)
        top3 = p1 + p2 + p3
        result[f"win_pct_{year}"] = format_pct(wins, starts)
        result[f"top3_pct_{year}"] = format_pct(top3, starts)
        result[f"starts_{year}"] = starts
    return result


def get_recent_records(horse):
    """Hämtar de senaste rekorden (form) från häststatistik."""
    records = []
    stats = horse.get("statistics", {})
    years_data = stats.get("years", {})

    # Gå igenom 2026 och 2025 i ordning
    for year in ["2026", "2025"]:
        for rec in years_data.get(year, {}).get("records", []):
            place = rec.get("place", "?")
            sm = rec.get("startMethod", "")
            dist = rec.get("distance", "")
            t = rec.get("time", {})
            time_str = f"{t.get('minutes',1)}:{t.get('seconds','??'):02}.{t.get('tenths','?')}" if t else "-"
            start_type = "V" if sm == "volte" else "A"
            dist_short = {"short": "K", "medium": "M", "long": "L"}.get(dist, dist)
            records.append({
                "year": year,
                "place": place if place != 0 else "0/disk",
                "start_type": start_type,
                "distance": dist_short,
                "time": time_str,
            })
        if len(records) >= 3:
            break

    return records[:3]


def fetch_todays_races():
    """Hämtar alla lopp med voltstart och startplats 1 för idag."""
    today = date.today().isoformat()
    try:
        resp = requests.get(f"{ATG_BASE}/calendar/day/{today}", headers=HEADERS, timeout=15)
        resp.raise_for_status()
        calendar = resp.json()
    except Exception as e:
        return [], f"Kunde inte hämta kalender: {e}"

    tracks = {t["id"]: t for t in calendar.get("tracks", [])}
    # Bara SE-travbanor med mergedPools (har vinnare/plats odds)
    se_trot_races = []
    for track in calendar.get("tracks", []):
        if track.get("countryCode") != "SE" or track.get("sport") != "trot":
            continue
        for race in track.get("races", []):
            if race.get("mergedPools"):
                se_trot_races.append({"race_id": race["id"], "track": track["name"]})

    rows = []
    for item in se_trot_races:
        race_id = item["race_id"]
        track_name = item["track"]
        try:
            resp = requests.get(
                f"{ATG_BASE}/games/vinnare_{race_id}",
                headers=HEADERS,
                timeout=15
            )
            if resp.status_code != 200:
                continue
            game_data = resp.json()
        except Exception:
            continue

        races_list = game_data.get("races", [])
        if not races_list:
            continue
        race = races_list[0]

        # Bara voltstart
        if race.get("startMethod") != "volte":
            continue

        race_number = race.get("number")
        race_time = race.get("startTime", "")
        try:
            dt = datetime.fromisoformat(race_time)
            time_str = dt.strftime("%H:%M")
        except Exception:
            time_str = race_time

        # Filtrera startplats 1
        for start in race.get("starts", []):
            if start.get("scratched"):
                continue
            start_nr = start.get("number")
            if start_nr != 1:
                continue

            horse = start.get("horse", {})
            driver = start.get("driver", {})
            pools = start.get("pools", {})

            win_odds = format_odds(pools.get("vinnare", {}).get("odds"))
            place_odds_raw = pools.get("plats", {}).get("minOdds")
            place_odds = format_odds(place_odds_raw)

            driver_name = f"{driver.get('firstName', '')} {driver.get('lastName', '')}".strip()
            dstats = driver_stats(driver)
            recent = get_recent_records(horse)

            avg_odds_raw = horse.get("statistics", {}).get("life", {}).get("lastFiveStarts", {})
            # lastFiveStarts is nested inside statistics > life in game data
            horse_stats = horse.get("statistics", {})
            last5 = horse_stats.get("lastFiveStarts", {})
            avg_odds = last5.get("averageOdds", None)
            avg_odds_str = f"{avg_odds / 100:.2f}" if avg_odds else "-"

            rows.append({
                "track": track_name,
                "race_number": race_number,
                "race_time": time_str,
                "horse_name": horse.get("name", "?"),
                "start_nr": start_nr,
                "win_odds": win_odds,
                "place_odds": place_odds,
                "driver_name": driver_name,
                "win_pct_2025": dstats["win_pct_2025"],
                "top3_pct_2025": dstats["top3_pct_2025"],
                "win_pct_2026": dstats["win_pct_2026"],
                "top3_pct_2026": dstats["top3_pct_2026"],
                "driver_starts_2025": dstats["starts_2025"],
                "driver_starts_2026": dstats["starts_2026"],
                "recent_records": recent,
                "avg_odds_last5": avg_odds_str,
            })

    # Sortera på starttid, sedan loppnummer, sedan startnummer
    rows.sort(key=lambda r: (r["race_time"], r["race_number"], r["start_nr"]))
    return rows, None


def refresh_cache():
    rows, error = fetch_todays_races()
    with _lock:
        _cache["data"] = rows
        _cache["error"] = error
        _cache["timestamp"] = time.time()
        _cache["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_cached_data():
    with _lock:
        age = time.time() - _cache["timestamp"]
        if _cache["data"] is None or age > CACHE_TTL:
            pass
        else:
            return _cache["data"], _cache.get("error"), _cache["last_updated"]
    # Cache miss – hämta ny data
    refresh_cache()
    with _lock:
        return _cache["data"], _cache.get("error"), _cache["last_updated"]


def background_refresh():
    """Bakgrundstråd som uppdaterar cachen var 3:e timme."""
    while True:
        time.sleep(CACHE_TTL)
        refresh_cache()


def ensure_started():
    """Initierar cache och bakgrundsuppdatering en gång per process."""
    global _refresh_thread_started
    with _lock:
        needs_refresh = _cache["data"] is None
        if _refresh_thread_started:
            start_thread = False
        else:
            _refresh_thread_started = True
            start_thread = True

    if needs_refresh:
        refresh_cache()

    if start_thread:
        t = threading.Thread(target=background_refresh, daemon=True)
        t.start()


@app.route("/")
def index():
    ensure_started()
    rows, error, last_updated = get_cached_data()
    next_update_ts = (_cache["timestamp"] + CACHE_TTL) * 1000  # ms för JavaScript
    return render_template(
        "index.html",
        rows=rows or [],
        error=error,
        last_updated=last_updated,
        next_update_ts=int(next_update_ts),
        today=date.today().strftime("%Y-%m-%d"),
    )


@app.route("/api/data")
def api_data():
    ensure_started()
    rows, error, last_updated = get_cached_data()
    return jsonify({"rows": rows or [], "error": error, "last_updated": last_updated})


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    ensure_started()
    refresh_cache()
    with _lock:
        return jsonify({"ok": True, "last_updated": _cache["last_updated"]})


if __name__ == "__main__":
    ensure_started()
    port = int(os.environ.get("PORT", "5000"))
    app.run(debug=False, host="0.0.0.0", port=port)
