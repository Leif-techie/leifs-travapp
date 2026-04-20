import os
import sqlite3
import time
import threading
from datetime import datetime, date
from flask import Flask, render_template, jsonify
import requests

app = Flask(__name__)

ATG_BASE = "https://www.atg.se/services/racinginfo/v1/api"
HEADERS = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
CACHE_TTL = 3 * 60 * 60  # 3 timmar i sekunder

_data_dir = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(_data_dir, "race_history.db")
ANALYSIS_START = "2026-04-21"
ANALYSIS_END = "2026-05-17"
WEEK_RANGES = [
    {"label": "Vecka 1", "end": "2026-04-27", "report": "Tis 28 apr kl 08:00"},
    {"label": "Vecka 2", "end": "2026-05-04", "report": "Tis 5 maj kl 08:00"},
    {"label": "Vecka 3", "end": "2026-05-11", "report": "Tis 12 maj kl 08:00"},
    {"label": "Vecka 4", "end": "2026-05-17", "report": "Mån 18 maj kl 08:00"},
]

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
    """Returnerar seger%, top3%, starter och råvärde för top3% per år."""
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
        result[f"top3_raw_{year}"] = round(top3 / starts * 100, 1) if starts > 0 else None
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
            trainer = horse.get("trainer", {})
            pools = start.get("pools", {})

            win_odds = format_odds(pools.get("vinnare", {}).get("odds"))
            place_odds_raw = pools.get("plats", {}).get("minOdds")
            place_odds = format_odds(place_odds_raw)

            result = start.get("result", {})
            result_place = result.get("place") if result else None
            kt = result.get("kmTime", {}) if result else {}
            if kt and kt.get("seconds") is not None:
                result_time = f"{kt.get('minutes', 1)}:{kt.get('seconds', 0):02}.{kt.get('tenths', 0)}"
            else:
                result_time = None
            final_odds = result.get("finalOdds") if result else None
            result_win_odds = f"{final_odds:.2f}" if final_odds else "-"
            place_odds_result_raw = pools.get("plats", {}).get("odds")
            result_place_odds = format_odds(place_odds_result_raw)
            result_galloped = result.get("galloped", False) if result else False

            driver_name = f"{driver.get('firstName', '')} {driver.get('lastName', '')}".strip()
            trainer_name = f"{trainer.get('firstName', '')} {trainer.get('lastName', '')}".strip()
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
                "race_id": race_id,
                "horse_name": horse.get("name", "?"),
                "start_nr": start_nr,
                "win_odds": win_odds,
                "place_odds": place_odds,
                "driver_name": driver_name,
                "trainer_name": trainer_name,
                "win_pct_2025": dstats["win_pct_2025"],
                "top3_pct_2025": dstats["top3_pct_2025"],
                "win_pct_2026": dstats["win_pct_2026"],
                "top3_pct_2026": dstats["top3_pct_2026"],
                "driver_starts_2025": dstats["starts_2025"],
                "driver_starts_2026": dstats["starts_2026"],
                "top3_raw_2025": dstats["top3_raw_2025"],
                "top3_raw_2026": dstats["top3_raw_2026"],
                "recent_records": recent,
                "avg_odds_last5": avg_odds_str,
                "result_place": result_place,
                "result_time": result_time,
                "result_win_odds": result_win_odds,
                "result_place_odds": result_place_odds,
                "result_galloped": result_galloped,
            })

    # Sortera på starttid, sedan loppnummer, sedan startnummer
    rows.sort(key=lambda r: (r["race_time"], r["race_number"], r["start_nr"]))
    return rows, None


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS race_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            race_date TEXT NOT NULL,
            track TEXT,
            race_number INTEGER,
            race_time TEXT,
            horse_name TEXT,
            driver_name TEXT,
            driver_top3_2025 REAL,
            driver_top3_2026 REAL,
            driver_starts_2025 INTEGER,
            driver_starts_2026 INTEGER,
            result_place INTEGER,
            paid_win_odds REAL,
            paid_place_odds REAL,
            result_galloped INTEGER,
            UNIQUE(race_date, track, race_number, horse_name)
        )
    """)
    conn.commit()
    conn.close()


def _to_float(v):
    if v is None or v == "-":
        return None
    try:
        return float(v)
    except Exception:
        return None


def store_completed_races(rows, race_date):
    """Sparar avslutade lopp (med resultat) till historikdatabasen."""
    if not rows:
        return 0
    conn = sqlite3.connect(DB_PATH)
    stored = 0
    for r in rows:
        if r.get("result_place") is None:
            continue
        try:
            conn.execute("""
                INSERT OR IGNORE INTO race_history
                (race_date, track, race_number, race_time, horse_name, driver_name,
                 driver_top3_2025, driver_top3_2026, driver_starts_2025, driver_starts_2026,
                 result_place, paid_win_odds, paid_place_odds, result_galloped)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                race_date,
                r.get("track"), r.get("race_number"), r.get("race_time"),
                r.get("horse_name"), r.get("driver_name"),
                r.get("top3_raw_2025"), r.get("top3_raw_2026"),
                r.get("driver_starts_2025"), r.get("driver_starts_2026"),
                r.get("result_place"),
                _to_float(r.get("result_win_odds")),
                _to_float(r.get("result_place_odds")),
                1 if r.get("result_galloped") else 0,
            ))
            stored += 1
        except Exception:
            pass
    conn.commit()
    conn.close()
    return stored


def calc_stats(rows):
    """Beräknar ackumulerad avkastning för en lista av lopp."""
    n = len(rows)
    if n == 0:
        return {"count": 0, "sum_place": 0.0, "avg_place": None, "sum_win": 0.0, "avg_win": None}
    sum_place = 0.0
    sum_win = 0.0
    for r in rows:
        place = r.get("result_place")
        galloped = r.get("result_galloped", 0)
        if place is not None and 1 <= int(place) <= 3 and not galloped:
            sum_place += r.get("paid_place_odds") or 0.0
        if place is not None and int(place) == 1 and not galloped:
            sum_win += r.get("paid_win_odds") or 0.0
    return {
        "count": n,
        "sum_place": round(sum_place, 2),
        "avg_place": round(sum_place / n, 3),
        "sum_win": round(sum_win, 2),
        "avg_win": round(sum_win / n, 3),
    }


def filter_by_driver_quality(rows):
    """Behåller bara hästar vars kusk har top3% ≥ 20% i alla år med starter."""
    result = []
    for r in rows:
        t25 = r.get("driver_top3_2025")
        t26 = r.get("driver_top3_2026")
        s25 = r.get("driver_starts_2025") or 0
        s26 = r.get("driver_starts_2026") or 0
        exclude = False
        if s25 > 0 and (t25 is None or t25 < 20.0):
            exclude = True
        if s26 > 0 and (t26 is None or t26 < 20.0):
            exclude = True
        if not exclude:
            result.append(r)
    return result


def get_weekly_stats():
    """Hämtar ackumulerade veckostatistik för analysperioden."""
    today = date.today().isoformat()
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM race_history WHERE race_date >= ? AND race_date <= ? ORDER BY race_date",
            (ANALYSIS_START, ANALYSIS_END),
        )
        all_rows = [dict(r) for r in cur.fetchall()]
        conn.close()
    except Exception:
        all_rows = []

    weeks = []
    for w in WEEK_RANGES:
        cum = [r for r in all_rows if r["race_date"] <= w["end"]]
        filtered = filter_by_driver_quality(cum)
        weeks.append({
            "label": w["label"],
            "end": w["end"],
            "report": w["report"],
            "complete": today > w["end"],
            "active": ANALYSIS_START <= today <= w["end"],
            "all": calc_stats(cum),
            "filtered": calc_stats(filtered),
        })
    return weeks, len(all_rows)


def refresh_cache():
    rows, error = fetch_todays_races()
    with _lock:
        _cache["data"] = rows
        _cache["error"] = error
        _cache["timestamp"] = time.time()
        _cache["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today_str = date.today().isoformat()
    if ANALYSIS_START <= today_str <= ANALYSIS_END:
        store_completed_races(rows or [], today_str)


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
    """Initierar DB, cache och bakgrundsuppdatering en gång per process."""
    global _refresh_thread_started
    init_db()
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


@app.route("/stats")
def stats_page():
    ensure_started()
    weeks, total = get_weekly_stats()
    return render_template(
        "stats.html",
        weeks=weeks,
        total=total,
        today=date.today().strftime("%Y-%m-%d"),
        analysis_start=ANALYSIS_START,
        analysis_end=ANALYSIS_END,
    )


if __name__ == "__main__":
    ensure_started()
    port = int(os.environ.get("PORT", "5000"))
    app.run(debug=False, host="0.0.0.0", port=port)
