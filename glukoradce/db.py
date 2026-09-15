"""SQLite úložiště – glykémie, jídla, bolusy, naučené parametry, nastavení."""
import json
import sqlite3
import threading
import time
from pathlib import Path

DEFAULT_SETTINGS = {
    # Terapeutické parametry (vždy si je zkontrolujte se svým diabetologem)
    "icr": 10.0,            # g sacharidů na 1 j. inzulínu
    "isf": 2.5,             # mmol/l na 1 j. (citlivost)
    "target": 6.0,          # cílová glykémie mmol/l
    "segments": [],         # volitelně: [{"start":"06:00","icr":8,"isf":2.0}, ...]
    "dia_h": 5.0,           # doba působení inzulínu (Control-IQ počítá s 5 h)
    "peak_min": 75,         # vrchol působení inzulínu v minutách
    "fpu_factor": 0.5,      # jaká část tuko-bílkovinných jednotek se kryje (start 50 %)
    "late_delay_min": 120,  # kdy podat druhou dávku (min po jídle)
    "late_min_units": 0.5,  # pod tuto hodnotu se druhá dávka nenavrhuje
    "max_bolus": 15.0,      # bezpečnostní strop jedné dávky
    "max_correction": 5.0,  # strop korekce
    "hypo": 3.9,
    "high": 10.0,
    "learn_step": 0.10,     # max. změna násobitele po jednom jídle
    "mult_min": 0.6,
    "mult_max": 1.5,
    "dexcom": {"username": "", "password": "", "region": "ous"},
    "tandem": {"email": "", "password": "", "region": "EU", "enabled": True},
    "poll_sec": 300,
    "demo": False,
}


class DB:
    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init()

    # ---------- infrastruktura ----------
    def _init(self):
        with self._lock:
            c = self._conn
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS glucose(
                    ts INTEGER PRIMARY KEY, mmol REAL NOT NULL, trend TEXT, source TEXT);
                CREATE TABLE IF NOT EXISTS meals(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    carbs REAL NOT NULL, fat REAL NOT NULL DEFAULT 0, protein REAL NOT NULL DEFAULT 0,
                    portion_label TEXT DEFAULT 'porce', note TEXT, created_ts INTEGER, archived INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS meal_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, meal_id INTEGER NOT NULL, ts INTEGER NOT NULL,
                    portions REAL NOT NULL, bg_start REAL, iob_start REAL,
                    suggested_now REAL, suggested_late REAL, suggested_delay_min INTEGER,
                    given_now REAL, given_late REAL, given_late_ts INTEGER,
                    explanation TEXT, outcome TEXT, evaluated INTEGER DEFAULT 0, note TEXT);
                CREATE TABLE IF NOT EXISTS boluses(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, units REAL NOT NULL,
                    kind TEXT NOT NULL, meal_event_id INTEGER, source TEXT, ext_id TEXT UNIQUE);
                CREATE INDEX IF NOT EXISTS boluses_ts ON boluses(ts);
                CREATE TABLE IF NOT EXISTS meal_params(
                    meal_id INTEGER PRIMARY KEY, now_mult REAL DEFAULT 1.0, late_mult REAL DEFAULT 1.0,
                    late_delay_min INTEGER, n_events INTEGER DEFAULT 0, updated_ts INTEGER, history TEXT);
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
                """
            )
            c.commit()

    def _q(self, sql, args=(), one=False):
        with self._lock:
            cur = self._conn.execute(sql, args)
            rows = cur.fetchall()
        rows = [dict(r) for r in rows]
        return (rows[0] if rows else None) if one else rows

    def _x(self, sql, args=()):
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur.lastrowid

    # ---------- nastavení ----------
    def get_settings(self):
        s = dict(DEFAULT_SETTINGS)
        for r in self._q("SELECT key, value FROM settings"):
            s[r["key"]] = json.loads(r["value"])
        return s

    def set_settings(self, patch):
        for k, v in patch.items():
            if k not in DEFAULT_SETTINGS:
                continue
            self._x("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, json.dumps(v)))
        return self.get_settings()

    # ---------- glykémie ----------
    def add_glucose(self, rows):
        """rows: iterable of (ts, mmol, trend, source). Vrací počet nových."""
        n = 0
        with self._lock:
            for ts, mmol, trend, source in rows:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO glucose(ts,mmol,trend,source) VALUES(?,?,?,?)",
                    (int(ts), float(mmol), trend, source))
                n += cur.rowcount
            self._conn.commit()
        return n

    def glucose_between(self, t0, t1):
        return self._q("SELECT ts,mmol,trend FROM glucose WHERE ts BETWEEN ? AND ? ORDER BY ts", (int(t0), int(t1)))

    def latest_glucose(self):
        return self._q("SELECT ts,mmol,trend FROM glucose ORDER BY ts DESC LIMIT 1", one=True)

    # ---------- jídla ----------
    def meals(self, include_archived=False):
        sql = "SELECT * FROM meals" + ("" if include_archived else " WHERE archived=0") + " ORDER BY name"
        return self._q(sql)

    def meal(self, meal_id):
        return self._q("SELECT * FROM meals WHERE id=?", (meal_id,), one=True)

    def add_meal(self, name, carbs, fat=0, protein=0, portion_label="porce", note=None):
        mid = self._x("INSERT INTO meals(name,carbs,fat,protein,portion_label,note,created_ts) VALUES(?,?,?,?,?,?,?)",
                      (name, carbs, fat, protein, portion_label, note, int(time.time())))
        self._x("INSERT OR IGNORE INTO meal_params(meal_id, history) VALUES(?, '[]')", (mid,))
        return mid

    def update_meal(self, meal_id, **fields):
        allowed = {"name", "carbs", "fat", "protein", "portion_label", "note", "archived"}
        sets = [(k, v) for k, v in fields.items() if k in allowed]
        if not sets:
            return
        self._x("UPDATE meals SET " + ",".join(f"{k}=?" for k, _ in sets) + " WHERE id=?",
                tuple(v for _, v in sets) + (meal_id,))

    def meal_params(self, meal_id):
        p = self._q("SELECT * FROM meal_params WHERE meal_id=?", (meal_id,), one=True)
        if not p:
            self._x("INSERT OR IGNORE INTO meal_params(meal_id, history) VALUES(?, '[]')", (meal_id,))
            p = self._q("SELECT * FROM meal_params WHERE meal_id=?", (meal_id,), one=True)
        p["history"] = json.loads(p.get("history") or "[]")
        return p

    def set_meal_params(self, meal_id, now_mult, late_mult, late_delay_min, n_events, history):
        self._x("INSERT OR REPLACE INTO meal_params(meal_id,now_mult,late_mult,late_delay_min,n_events,updated_ts,history)"
                " VALUES(?,?,?,?,?,?,?)",
                (meal_id, now_mult, late_mult, late_delay_min, n_events, int(time.time()), json.dumps(history)))

    # ---------- události jídla ----------
    def add_meal_event(self, **f):
        cols = ["meal_id", "ts", "portions", "bg_start", "iob_start", "suggested_now", "suggested_late",
                "suggested_delay_min", "explanation", "note"]
        vals = [f.get(c) for c in cols]
        if isinstance(vals[8], (dict, list)):
            vals[8] = json.dumps(vals[8], ensure_ascii=False)
        return self._x(f"INSERT INTO meal_events({','.join(cols)}) VALUES({','.join('?'*len(cols))})", vals)

    def meal_event(self, eid):
        e = self._q("SELECT * FROM meal_events WHERE id=?", (eid,), one=True)
        return self._decode_event(e)

    def _decode_event(self, e):
        if not e:
            return e
        for k in ("explanation", "outcome"):
            if e.get(k):
                try:
                    e[k] = json.loads(e[k])
                except Exception:
                    pass
        return e

    def meal_events(self, meal_id=None, limit=50, since=None):
        sql = "SELECT e.*, m.name AS meal_name, m.carbs AS meal_carbs FROM meal_events e JOIN meals m ON m.id=e.meal_id"
        cond, args = [], []
        if meal_id is not None:
            cond.append("e.meal_id=?"); args.append(meal_id)
        if since is not None:
            cond.append("e.ts>=?"); args.append(int(since))
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY e.ts DESC LIMIT ?"
        args.append(limit)
        return [self._decode_event(e) for e in self._q(sql, tuple(args))]

    def unevaluated_events(self, before_ts):
        return [self._decode_event(e) for e in
                self._q("SELECT * FROM meal_events WHERE evaluated=0 AND ts<=? ORDER BY ts", (int(before_ts),))]

    def update_meal_event(self, eid, **f):
        allowed = {"given_now", "given_late", "given_late_ts", "outcome", "evaluated", "note"}
        sets = [(k, (json.dumps(v, ensure_ascii=False) if k == "outcome" and not isinstance(v, str) else v))
                for k, v in f.items() if k in allowed]
        if sets:
            self._x("UPDATE meal_events SET " + ",".join(f"{k}=?" for k, _ in sets) + " WHERE id=?",
                    tuple(v for _, v in sets) + (eid,))

    # ---------- bolusy ----------
    def add_bolus(self, ts, units, kind, meal_event_id=None, source="manual", ext_id=None):
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO boluses(ts,units,kind,meal_event_id,source,ext_id) VALUES(?,?,?,?,?,?)",
                (int(ts), float(units), kind, meal_event_id, source, ext_id))
            self._conn.commit()
            return cur.lastrowid if cur.rowcount else None

    def boluses_between(self, t0, t1):
        return self._q("SELECT * FROM boluses WHERE ts BETWEEN ? AND ? ORDER BY ts", (int(t0), int(t1)))

    def link_bolus(self, bid, ext_id, source):
        """Spáruje ručně zapsaný bolus se záznamem z pumpy (aby nebyl dvakrát)."""
        self._x("UPDATE boluses SET ext_id=?, source=? WHERE id=?", (ext_id, source, bid))

    def delete_boluses_by_source(self, source):
        """Smaže bolusy daného zdroje (např. po opravě časového pásma – stáhnou se znovu)."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM boluses WHERE source=?", (source,))
            self._conn.commit()
            return cur.rowcount

    def delete_bolus(self, bid):
        self._x("DELETE FROM boluses WHERE id=?", (bid,))
