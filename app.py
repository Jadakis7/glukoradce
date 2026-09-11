#!/usr/bin/env python3
"""Glukorádce – osobní bolusový poradce nad Dexcom G7 + Tandem t:slim X2.

Spuštění:  python3 app.py            (http://localhost:8765)
Demo:      python3 app.py --demo     (simulovaná data, bez Dexcomu)
"""
import argparse
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

import datetime as dt
import hmac
import secrets

from flask import Flask, jsonify, redirect, request, send_from_directory, session

sys.path.insert(0, str(Path(__file__).parent))
from glukoradce import dexcom_client, outcomes, recommender, tandem_import, tandem_sync  # noqa: E402
from glukoradce.db import DB  # noqa: E402
from glukoradce.iob import iob_at  # noqa: E402

log = logging.getLogger("glukoradce")
BASE = Path(__file__).parent
STATIC = BASE / "static"

app = Flask(__name__, static_folder=None)
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True,
                  PERMANENT_SESSION_LIFETIME=dt.timedelta(days=90))
db: DB = None
state = {"last_sync": None, "last_error": None, "demo": False,
         "tandem_last_sync": None, "tandem_error": None, "tandem_new": 0,
         "dexcom_retry_after": 0}
AUTH_BACKOFF_SEC = 30 * 60  # po neúspěšném přihlášení k Dexcomu půl hodiny nezkoušet (ochrana před zámkem účtu)


# ---------------------------------------------------------------- pomocné
def _settings():
    return db.get_settings()


def _public_settings(s):
    s = dict(s)
    for key in ("dexcom", "tandem"):
        d = dict(s.get(key) or {})
        d["password"] = "••••••" if d.get("password") else ""
        s[key] = d
    return s


def _iob_now(s, ts=None):
    ts = ts or time.time()
    return iob_at(db.boluses_between(ts - s["dia_h"] * 3600, ts), ts, s["dia_h"], s["peak_min"])


def _pending_late(now):
    """Jídla, u kterých byla navržena druhá dávka a ještě není zapsaná."""
    out = []
    for e in db.meal_events(since=now - 8 * 3600, limit=20):
        if (e.get("suggested_late") or 0) > 0 and e.get("given_late") is None:
            due = e["ts"] + (e.get("suggested_delay_min") or 120) * 60
            out.append({"event_id": e["id"], "meal": e["meal_name"], "units": e["suggested_late"],
                        "due_ts": due, "minutes_left": int((due - now) / 60)})
    return out


def _meal_history(meal_id, limit=5):
    hist = []
    for e in db.meal_events(meal_id=meal_id, limit=limit):
        o = e.get("outcome") or {}
        m = (o.get("metrics") or {}) if isinstance(o, dict) else {}
        hist.append({"event_id": e["id"], "ts": e["ts"], "portions": e["portions"],
                     "given_now": e.get("given_now"), "given_late": e.get("given_late"),
                     "suggested_now": e.get("suggested_now"), "suggested_late": e.get("suggested_late"),
                     "delay": e.get("suggested_delay_min"),
                     "verdict": o.get("verdict") if isinstance(o, dict) else None,
                     "learn_note": o.get("learn_note") if isinstance(o, dict) else None,
                     "peak": m.get("peak"), "bg_2h": m.get("bg_2h"), "bg_5h": m.get("bg_5h"), "min": m.get("min"),
                     "evaluated": bool(e.get("evaluated"))})
    return hist


# ---------------------------------------------------------------- přihlášení
# Heslo se nastavuje proměnnou prostředí GLUKORADCE_PASSWORD (na serveru povinné).
# Bez ní aplikace běží bez přihlášení – vhodné jen doma na localhostu.
PASSWORD = os.environ.get("GLUKORADCE_PASSWORD") or ""

LOGIN_HTML = """<!doctype html><html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Glukorádce – přihlášení</title>
<style>body{font:16px -apple-system,system-ui,sans-serif;background:#f6f6f4;color:#0b0b0b;display:flex;min-height:100vh;
align-items:center;justify-content:center;margin:0}form{background:#fff;border:1px solid #e4e3df;border-radius:14px;padding:24px;width:320px}
h1{font-size:20px;margin:0 0 12px}input{width:100%;box-sizing:border-box;font:inherit;padding:11px 12px;border:1px solid #e4e3df;border-radius:10px;margin:8px 0 12px}
button{width:100%;font:inherit;font-weight:600;padding:12px;border:0;border-radius:12px;background:#2a78d6;color:#fff}.e{color:#e34948;font-size:14px}
@media(prefers-color-scheme:dark){body{background:#141413;color:#f4f4f1}form{background:#1f1f1e;border-color:#33332f}input{background:#141413;color:#f4f4f1;border-color:#33332f}}</style></head>
<body><form method="post"><h1>Glukorádce</h1><label>Heslo</label><input type="password" name="password" autofocus autocomplete="current-password">
{ERR}<button>Přihlásit</button></form></body></html>"""


def _authed():
    return not PASSWORD or session.get("ok") is True


@app.before_request
def _guard():
    if not PASSWORD or request.path.startswith("/login") or request.path.startswith("/static/"):
        return None
    if _authed():
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "unauthorized"}), 401
    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not PASSWORD:
        return redirect("/")
    err = ""
    if request.method == "POST":
        pw = request.form.get("password") or ""
        time.sleep(0.5)  # brzda proti hádání hesla
        if hmac.compare_digest(pw, PASSWORD):
            session.permanent = True
            session["ok"] = True
            return redirect("/")
        err = "<div class=e>Špatné heslo.</div>"
    return LOGIN_HTML.replace("{ERR}", err)


@app.get("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ---------------------------------------------------------------- statické
@app.get("/")
def index():
    return send_from_directory(STATIC, "index.html")


@app.get("/static/<path:p>")
def static_files(p):
    return send_from_directory(STATIC, p)


# ---------------------------------------------------------------- API
@app.get("/api/status")
def api_status():
    s = _settings()
    now = time.time()
    g = db.latest_glucose()
    return jsonify({
        "now": int(now),
        "glucose": g,
        "glucose_age_min": int((now - g["ts"]) / 60) if g else None,
        "trend_arrow": dexcom_client.TREND_ARROWS.get(g["trend"]) if g else "",
        "iob": _iob_now(s, now),
        "pending_late": _pending_late(now),
        "last_sync": state["last_sync"], "last_error": state["last_error"], "demo": state["demo"],
        "dexcom_configured": bool((s.get("dexcom") or {}).get("username")),
        "tandem_configured": bool((s.get("tandem") or {}).get("email")),
        "tandem_last_sync": state["tandem_last_sync"], "tandem_error": state["tandem_error"],
        "settings": {k: s[k] for k in ("hypo", "high", "target", "dia_h")},
    })


@app.get("/api/glucose")
def api_glucose():
    hours = float(request.args.get("hours", 6))
    now = time.time()
    t0 = now - hours * 3600
    return jsonify({
        "points": db.glucose_between(t0, now),
        "boluses": db.boluses_between(t0, now),
        "meal_events": [{"id": e["id"], "ts": e["ts"], "name": e["meal_name"], "portions": e["portions"]}
                        for e in db.meal_events(since=t0, limit=50)],
    })


@app.get("/api/meals")
def api_meals():
    out = []
    for m in db.meals():
        p = db.meal_params(m["id"])
        m["params"] = {"now_mult": p["now_mult"], "late_mult": p["late_mult"],
                       "late_delay_min": p["late_delay_min"], "n_events": p["n_events"]}
        out.append(m)
    return jsonify(out)


@app.post("/api/meals")
def api_add_meal():
    d = request.get_json(force=True)
    mid = db.add_meal(d["name"].strip(), float(d.get("carbs") or 0), float(d.get("fat") or 0),
                      float(d.get("protein") or 0), d.get("portion_label") or "porce", d.get("note"))
    return jsonify(db.meal(mid))


@app.put("/api/meals/<int:mid>")
def api_update_meal(mid):
    d = request.get_json(force=True)
    db.update_meal(mid, **{k: v for k, v in d.items() if k in ("name", "carbs", "fat", "protein", "portion_label", "note", "archived")})
    return jsonify(db.meal(mid))


@app.post("/api/meals/<int:mid>/reset")
def api_reset_meal(mid):
    db.set_meal_params(mid, 1.0, 1.0, None, 0, [])
    return jsonify(db.meal_params(mid))


@app.post("/api/recommend")
def api_recommend():
    d = request.get_json(force=True)
    s = _settings()
    meal = db.meal(int(d["meal_id"]))
    if not meal:
        return jsonify({"error": "jídlo nenalezeno"}), 404
    portions = float(d.get("portions") or 1)
    bg = d.get("bg")
    bg = float(bg) if bg not in (None, "") else None
    rec = recommender.recommend(db, s, meal, portions, time.time(), bg=bg, trend=d.get("trend"))
    rec["meal"] = meal
    rec["history"] = _meal_history(meal["id"])
    return jsonify(rec)


@app.post("/api/meal_events")
def api_add_event():
    """Zapsat jídlo + skutečně podanou první dávku."""
    d = request.get_json(force=True)
    s = _settings()
    meal = db.meal(int(d["meal_id"]))
    now = int(d.get("ts") or time.time())
    portions = float(d.get("portions") or 1)
    rec = d.get("recommendation") or recommender.recommend(db, s, meal, portions, now)
    given_now = float(d.get("given_now")) if d.get("given_now") not in (None, "") else None
    eid = db.add_meal_event(meal_id=meal["id"], ts=now, portions=portions,
                            bg_start=(rec.get("inputs") or {}).get("bg"), iob_start=(rec.get("inputs") or {}).get("iob"),
                            suggested_now=rec.get("now"), suggested_late=rec.get("late"),
                            suggested_delay_min=rec.get("late_delay_min"),
                            explanation={k: rec.get(k) for k in ("inputs", "base", "learned", "steps", "warnings")},
                            note=d.get("note"))
    if given_now is not None:
        db.update_meal_event(eid, given_now=given_now)
        if given_now > 0:
            db.add_bolus(now, given_now, "meal", eid, source="app")
    return jsonify(db.meal_event(eid))


@app.post("/api/meal_events/<int:eid>/dose")
def api_event_dose(eid):
    """Doplnit/opravit skutečné dávky k jídlu (první nebo druhá)."""
    d = request.get_json(force=True)
    e = db.meal_event(eid)
    if not e:
        return jsonify({"error": "nenalezeno"}), 404
    now = int(d.get("ts") or time.time())
    if "given_now" in d:
        u = float(d["given_now"])
        db.update_meal_event(eid, given_now=u)
        for b in db.boluses_between(e["ts"] - 60, e["ts"] + 60):
            if b["meal_event_id"] == eid and b["kind"] == "meal":
                db.delete_bolus(b["id"])
        if u > 0:
            db.add_bolus(e["ts"], u, "meal", eid, source="app")
    if "given_late" in d:
        u = float(d["given_late"])
        db.update_meal_event(eid, given_late=u, given_late_ts=now)
        if u > 0:
            db.add_bolus(now, u, "late", eid, source="app")
    return jsonify(db.meal_event(eid))


@app.get("/api/meal_events")
def api_events():
    limit = int(request.args.get("limit", 30))
    meal_id = request.args.get("meal_id")
    return jsonify(db.meal_events(meal_id=int(meal_id) if meal_id else None, limit=limit))


@app.post("/api/meal_events/<int:eid>/evaluate")
def api_evaluate(eid):
    e = db.meal_event(eid)
    if not e:
        return jsonify({"error": "nenalezeno"}), 404
    r = outcomes.evaluate_event(db, _settings(), e)
    return jsonify(r or {"pending": True, "note": "Ještě neuplynulo 6 h od jídla."})


@app.post("/api/boluses")
def api_add_bolus():
    d = request.get_json(force=True)
    ts = int(d.get("ts") or time.time())
    bid = db.add_bolus(ts, float(d["units"]), d.get("kind") or "manual", source="app")
    return jsonify({"id": bid, "ts": ts})


@app.delete("/api/boluses/<int:bid>")
def api_del_bolus(bid):
    db.delete_bolus(bid)
    return jsonify({"ok": True})


@app.post("/api/import/tandem")
def api_import_tandem():
    f = request.files.get("file")
    text = f.read().decode("utf-8-sig", errors="replace") if f else (request.get_data(as_text=True) or "")
    info = tandem_import.import_csv(db, text)
    return jsonify(info)


@app.get("/api/settings")
def api_get_settings():
    return jsonify(_public_settings(_settings()))


@app.put("/api/settings")
def api_put_settings():
    d = request.get_json(force=True)
    cur = _settings()
    for key, fields in (("dexcom", ("username", "password", "region")), ("tandem", ("email", "password", "region", "enabled"))):
        if key in d:
            merged = dict(cur.get(key) or {})
            for k in fields:
                if k not in d[key]:
                    continue
                if k == "password" and (d[key][k] in ("", "••••••") or d[key][k] is None):
                    continue  # prázdné = ponechat uložené heslo
                merged[k] = d[key][k]
            d[key] = merged
    for k in ("icr", "isf", "target", "dia_h", "fpu_factor", "max_bolus", "max_correction", "hypo", "high",
              "learn_step", "peak_min", "late_delay_min", "late_min_units", "poll_sec"):
        if k in d:
            d[k] = float(d[k]) if k not in ("peak_min", "late_delay_min", "poll_sec") else int(d[k])
    s = db.set_settings(d)
    state["last_error"] = None
    state["tandem_error"] = None
    state["dexcom_retry_after"] = 0  # po změně nastavení zkusit hned
    return jsonify(_public_settings(s))


@app.post("/api/sync")
def api_sync():
    n = _sync_once()
    return jsonify({"new": n, "last_sync": state["last_sync"], "error": state["last_error"],
                    "tandem_new": state["tandem_new"], "tandem_error": state["tandem_error"]})


@app.post("/api/tandem/probe")
def api_tandem_probe():
    """Diagnostika připojení k Tandem Source (trvá 10–60 s)."""
    cfg = _settings().get("tandem") or {}
    if not cfg.get("email") or not cfg.get("password"):
        return jsonify({"error": "Nejdřív uložte e-mail a heslo k Tandem Source."}), 400
    d = tandem_sync.probe(cfg["email"], cfg["password"], cfg.get("region", "EU"), hours=48)
    try:
        (BASE / "data" / "tandem_probe.json").write_text(json.dumps(d, ensure_ascii=False, indent=1, default=str))
    except Exception:  # noqa: BLE001
        pass
    d.pop("bolus_event_samples", None)
    d.pop("pump_event_metadata", None)
    return jsonify(d)


# ---------------------------------------------------------------- pozadí
def _sync_once():
    s = _settings()
    n = 0
    if not state["demo"] and time.time() >= state["dexcom_retry_after"]:
        try:
            n = dexcom_client.sync_once(db, s)
            state["last_sync"] = int(time.time())
            state["last_error"] = None
        except Exception as ex:  # noqa: BLE001
            msg = str(ex)
            if "authenticate" in msg.lower() or "password" in msg.lower() or "account" in msg.lower():
                state["dexcom_retry_after"] = time.time() + AUTH_BACKOFF_SEC
                msg += " – zkontrolujte jméno/heslo v Nastavení; další pokus za 30 min (ochrana před zamknutím účtu)."
            state["last_error"] = msg
            log.warning("Dexcom sync selhal: %s", ex)
    tcfg = s.get("tandem") or {}
    if not state["demo"] and tcfg.get("email") and tcfg.get("password") and tcfg.get("enabled", True):
        try:
            state["tandem_new"] = tandem_sync.sync_once(db, s)
            state["tandem_last_sync"] = int(time.time())
            state["tandem_error"] = None
        except Exception as ex:  # noqa: BLE001
            state["tandem_error"] = str(ex)
            log.warning("Tandem sync selhal: %s", ex)
    try:
        for eid, r in outcomes.evaluate_pending(db, s):
            log.info("Vyhodnoceno jídlo #%s: %s", eid, r.get("verdict"))
    except Exception as ex:  # noqa: BLE001
        log.exception("Vyhodnocení selhalo: %s", ex)
    return n


def _background():
    while True:
        _sync_once()
        time.sleep(max(60, int(_settings().get("poll_sec") or 300)))


def _watched_files():
    return [BASE / "app.py"] + sorted((BASE / "glukoradce").glob("*.py"))


RESTART_CODE = 3


def _restart():
    """Ukončí proces s kódem 3 – start.sh ho okamžitě spustí znovu."""
    log.info("Restartuji server (změna kódu)…")
    logging.shutdown()
    os._exit(RESTART_CODE)


def _autoreload():
    """Když se změní zdrojový kód (např. po aktualizaci), server se sám restartuje."""
    stamp = {f: f.stat().st_mtime for f in _watched_files() if f.exists()}
    while True:
        time.sleep(3)
        try:
            for f in _watched_files():
                m = f.stat().st_mtime if f.exists() else None
                if m and stamp.get(f) != m:
                    time.sleep(2)  # počkat, až se dopíšou všechny soubory
                    _restart()
        except Exception as ex:  # noqa: BLE001
            log.warning("autoreload: %s", ex)


@app.post("/api/restart")
def api_restart():
    threading.Timer(0.5, _restart).start()
    return jsonify({"ok": True, "note": "Server se restartuje, obnovte stránku za pár sekund."})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("GLUKORADCE_DB", str(BASE / "data" / "glukoradce.sqlite")))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--demo", action="store_true", help="simulovaná data místo Dexcomu")
    ap.add_argument("--no-sync", action="store_true")
    args = ap.parse_args()
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler()]
    try:
        from logging.handlers import RotatingFileHandler
        handlers.append(RotatingFileHandler(Path(args.db).parent / "server.log", maxBytes=2_000_000, backupCount=2))
    except Exception:  # noqa: BLE001
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)

    global db
    data_dir = Path(args.db).parent
    data_dir.mkdir(parents=True, exist_ok=True)
    db = DB(args.db)
    # tajný klíč pro podepisování přihlašovací cookie (přežije restart)
    key_file = data_dir / ".secret_key"
    if os.environ.get("SECRET_KEY"):
        app.secret_key = os.environ["SECRET_KEY"]
    else:
        if not key_file.exists():
            key_file.write_text(secrets.token_hex(32))
        app.secret_key = key_file.read_text().strip()
    if os.environ.get("GLUKORADCE_HTTPS", "").lower() in ("1", "true", "yes") or os.environ.get("RAILWAY_ENVIRONMENT"):
        app.config["SESSION_COOKIE_SECURE"] = True
    if PASSWORD:
        log.info("Přihlášení heslem je zapnuté.")
    else:
        log.warning("GLUKORADCE_PASSWORD není nastaveno – aplikace běží BEZ přihlášení (jen pro localhost).")
    state["demo"] = args.demo or bool(db.get_settings().get("demo"))
    if state["demo"]:
        from glukoradce.demo import seed_demo
        if seed_demo(db, db.get_settings()):
            log.info("Demo data vytvořena.")
    if not args.no_sync:
        threading.Thread(target=_background, daemon=True).start()
    if os.environ.get("GLUKORADCE_AUTORELOAD", "1") != "0":
        threading.Thread(target=_autoreload, daemon=True).start()
    log.info("Glukorádce běží na http://localhost:%s  (DB: %s)", args.port, args.db)
    try:
        from waitress import serve
        serve(app, host=args.host, port=args.port, threads=8)
    except ImportError:
        app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
