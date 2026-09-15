"""Automatické stahování bolusů z Tandem Source (cloud pumpy t:slim X2) přes knihovnu
tconnectsync (https://github.com/jwoglom/tconnectsync).

Tandem nemá veřejné API; tconnectsync používá stejné rozhraní jako web Tandem Source.
Formát dat se občas mění, proto je modul napsaný defenzivně: zkouší několik cest,
jak se ke datům dostat, a všechno, co najde, umí vypsat v diagnostice (probe), aby
šlo parser doladit podle skutečných dat.

Ukládá:  meal (bolus k jídlu), correction (ruční korekce), auto (automatická korekce
Control-IQ), manual (ostatní).  ext_id = "tandem:<id>" chrání před duplicitami.
"""
import datetime as dt
import json
import logging
import re
import time

log = logging.getLogger("glukoradce.tandem")


class TandemError(Exception):
    pass


# ---------------------------------------------------------------- přihlášení
def _api(email, password, region="EU"):
    try:
        import tconnectsync  # noqa: F401
    except ImportError as ex:
        raise TandemError("Knihovna tconnectsync není nainstalovaná (pip install tconnectsync).") from ex
    import os
    os.environ.setdefault("TCONNECT_REGION", region)
    # tconnectsync vykládá časy z pumpy v pásmu TIMEZONE_NAME (výchozí America/New_York!)
    os.environ.setdefault("TIMEZONE_NAME", os.environ.get("TZ") or "Europe/Prague")
    try:
        from tconnectsync import secret
        if hasattr(secret, "TCONNECT_REGION"):
            secret.TCONNECT_REGION = region
    except Exception:  # noqa: BLE001
        pass
    from tconnectsync.api import TConnectApi
    try:
        return TConnectApi(email, password, region)   # tconnectsync >= 3
    except TypeError:
        return TConnectApi(email, password)


def _ts_to_unix(v):
    """arrow.Arrow / datetime / str / int -> unix s."""
    if v is None:
        return None
    if hasattr(v, "timestamp"):
        t = v.timestamp
        try:
            t = t() if callable(t) else t
            return int(t)
        except Exception:  # noqa: BLE001
            pass
    return _parse_ts(v)


# ---------------------------------------------------------------- tconnectsync 3.x (Tandem Source BFF)
def choose_pump(api):
    """Vrátí (assignmentId, info) nejnověji použité pumpy na účtu."""
    pumper = api.tandemsource.get_pumper()
    pumps = (pumper or {}).get("pumps") or []
    if not pumps:
        raise TandemError("Na účtu Tandem Source není žádná pumpa.")
    def _key(p):
        return str(p.get("maxDateOfEvents") or "")
    best = max(pumps, key=_key)
    info = {k: best.get(k) for k in ("serialNumber", "assignmentId", "maxDateOfEvents", "modelNumber")}
    info["all_pumps"] = [{k: p.get(k) for k in ("serialNumber", "assignmentId", "maxDateOfEvents", "modelNumber")} for p in pumps]
    return best.get("assignmentId"), info


def fetch_events_v3(api, hours=24, diag=None):
    """Stáhne události pumpy za posledních `hours` hodin přes tconnectsync 3.x."""
    diag = diag if diag is not None else {}
    dev_id, info = choose_pump(api)
    diag["pump"] = info
    diag["device_id"] = dev_id
    now = dt.datetime.now()
    since = now - dt.timedelta(hours=hours)
    try:
        import arrow
        a, b = arrow.get(since), arrow.get(now)
    except ImportError:
        a, b = since.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")
    events = list(api.tandemsource.pump_events(dev_id, a, b))
    diag.setdefault("attempts", []).append(("tandemsource.pump_events(v3)", {"hours": hours}, "ok", len(events)))
    return events


def extract_boluses_v3(events):
    """Bolusy z událostí tconnectsync 3.x: LidBolusCompleted (+ LidBolexCompleted = prodloužená
    část) spárované přes bolusId s LidBolusRequestedMsg1 (sacharidy, glykémie, typ) a Msg2
    (volby – automatický bolus Control-IQ)."""
    by_id = {}
    for e in events:
        bid = getattr(e, "bolusId", None)
        if bid is not None:
            by_id.setdefault(bid, {})[type(e).__name__] = e
    out = []
    for e in events:
        cls = type(e).__name__
        if cls not in ("LidBolusCompleted", "LidBolexCompleted"):
            continue
        units = float(getattr(e, "insulinDelivered", 0) or 0)
        ts = _ts_to_unix(getattr(e, "eventTimestamp", None))
        if not ts or units <= 0:
            continue
        bid = getattr(e, "bolusId", None)
        seq = getattr(e, "seqNum", None)
        m = by_id.get(bid, {})
        m1, m2 = m.get("LidBolusRequestedMsg1"), m.get("LidBolusRequestedMsg2")
        carbs = float(getattr(m1, "carbAmount", 0) or 0) if m1 else 0.0
        bg = getattr(m1, "bg", 0) if m1 else 0
        btype = getattr(m1, "bolusTypeRaw", None) if m1 else None      # 2 = Automatic Correction
        opts = getattr(m2, "optionsRaw", None) if m2 else None         # 3 / 6 = Automatic Bolus
        corr_incl = getattr(m1, "correctionBolusIncludedRaw", 0) if m1 else 0
        if cls == "LidBolexCompleted":
            kind = "late"          # prodloužená část kombinovaného bolusu
        elif btype == 2 or opts in (3, 6):
            kind = "auto"
        elif carbs > 0:
            kind = "meal"
        elif corr_incl:
            kind = "correction"
        else:
            kind = "manual"
        out.append({"ts": ts, "units": round(units, 2), "kind": kind,
                    "ext_id": f"tandem:{bid}:{seq}" if seq is not None else f"tandem:{bid}:{ts}",
                    "carbs": carbs or None, "bg_mgdl": bg or None})
    out.sort(key=lambda r: r["ts"])
    return out


# ---------------------------------------------------------------- generický průzkum objektů
def _to_plain(obj, depth=0):
    """Převede objekt událostí tconnectsync na slovník (pro diagnostiku i parser)."""
    if depth > 4:
        return str(obj)
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    if isinstance(obj, (dt.datetime, dt.date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): _to_plain(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_plain(v, depth + 1) for v in list(obj)[:2000]]
    out = {"__class__": type(obj).__name__}
    for name in dir(obj):
        if name.startswith("_"):
            continue
        try:
            v = getattr(obj, name)
        except Exception:  # noqa: BLE001
            continue
        if callable(v):
            continue
        out[name] = _to_plain(v, depth + 1)
    return out


def _find_key(d, patterns):
    for k, v in d.items():
        kl = k.lower()
        if any(re.fullmatch(p, kl) for p in patterns):
            return v
    for k, v in d.items():
        kl = k.lower()
        if any(re.search(p, kl) for p in patterns):
            return v
    return None


def _parse_ts(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        v = float(v)
        if v > 1e12:
            v /= 1000
        # Tandem někdy používá sekundy od 1. 1. 2008
        if v < 1e9:
            v += dt.datetime(2008, 1, 1, tzinfo=dt.timezone.utc).timestamp()
        return int(v)
    s = str(v).strip()
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc) if s.endswith("Z") else d.astimezone()
        return int(d.timestamp())
    except ValueError:
        pass
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return int(dt.datetime.strptime(s, fmt).timestamp())
        except ValueError:
            continue
    return None


def extract_boluses(events):
    """Z libovolného seznamu událostí (objekty/dicty) vytáhne dokončené bolusy.

    Hledá události, jejichž třída/typ obsahuje 'Bolus' a 'Complet' (LidBolusCompleted),
    a v nich pole s doručeným inzulínem, časem a id bolusu. Typ (jídlo/korekce/auto)
    se dovodí z ostatních polí, když tam jsou.
    """
    out, seen = [], set()
    plain = [_to_plain(e) if not isinstance(e, dict) else e for e in events]
    # mapa bolusid -> doplňkové informace z požadavků (RequestedMsg): sacharidy, typ
    requests = {}
    for p in plain:
        if not isinstance(p, dict):
            continue
        cls = str(p.get("__class__") or p.get("type") or p.get("eventType") or "")
        if "bolus" in cls.lower() and "request" in cls.lower():
            bid = _find_key(p, [r"bolusid", r"bolus_id", r"bolusnum"])
            if bid is not None:
                r = requests.setdefault(str(bid), {})
                r.update({k: v for k, v in p.items() if not isinstance(v, (dict, list))})
    for p in plain:
        if not isinstance(p, dict):
            continue
        cls = str(p.get("__class__") or p.get("type") or p.get("eventType") or "").lower()
        if not ("bolus" in cls and ("complet" in cls or "deliver" in cls)):
            continue
        units = _find_key(p, [r"insulindelivered", r"insulin_delivered", r"delivered", r"insulin.*deliv", r"units"])
        ts = _find_key(p, [r"eventtimestamp", r"timestamp", r"eventdatetime", r"event_time", r"time", r"date"])
        bid = _find_key(p, [r"bolusid", r"bolus_id", r"bolusnum"])
        try:
            units = float(units)
        except (TypeError, ValueError):
            continue
        ts = _parse_ts(ts)
        if not ts or units <= 0:
            continue
        ext = f"tandem:{bid}" if bid is not None else f"tandem:{ts}:{units}"
        if ext in seen:
            continue
        seen.add(ext)
        info = dict(requests.get(str(bid), {}))
        info.update({k: v for k, v in p.items() if not isinstance(v, (dict, list))})
        text = json.dumps(info, default=str).lower()
        carbs = _find_key(info, [r"carb", r"carbs", r"carbsize", r"carbamount"])
        try:
            carbs = float(carbs) if carbs is not None else None
        except (TypeError, ValueError):
            carbs = None
        if re.search(r"automat|autobolus|\"aa\"|controliq|control_iq|algorithm", text) and not (carbs and carbs > 0):
            kind = "auto"
        elif carbs and carbs > 0:
            kind = "meal"
        elif "correction" in text:
            kind = "correction"
        else:
            kind = "manual"
        out.append({"ts": ts, "units": round(units, 2), "kind": kind, "ext_id": ext, "carbs": carbs})
    out.sort(key=lambda r: r["ts"])
    return out


# ---------------------------------------------------------------- stažení událostí
def fetch_events(api, hours=24, diag=None):
    """Zkusí několik cest, jak z tconnectsync dostat události pumpy. Vrací seznam."""
    diag = diag if diag is not None else {}
    now = dt.datetime.now()
    since = now - dt.timedelta(hours=hours)
    attempts = []

    # 1) Tandem Source (tconnectsync >= 2.0)
    ts_api = getattr(api, "tandemsource", None)
    if ts_api is not None:
        try:
            meta = ts_api.pump_event_metadata()
            diag["pump_event_metadata"] = _to_plain(meta)
            pumps = meta if isinstance(meta, list) else [meta]
            dev_id = None
            for pm in pumps:
                pm = _to_plain(pm)
                dev_id = _find_key(pm, [r"tconnectdeviceid", r"deviceid", r"pumpid", r"id"]) or dev_id
            diag["device_id"] = dev_id
            for kw in ({"min_date": since, "max_date": now}, {"min_date": since}, {}):
                try:
                    ev = ts_api.pump_events(dev_id, **kw)
                    attempts.append(("tandemsource.pump_events", kw, "ok", len(ev) if hasattr(ev, "__len__") else "?"))
                    ev = list(ev) if not isinstance(ev, dict) else ev.get("events") or list(ev.values())
                    # některé verze vracejí surová data, která je potřeba dekódovat
                    if ev and not hasattr(ev[0], "__dict__") and not isinstance(ev[0], dict):
                        for modname, fn in (("tconnectsync.eventparser.events", "decode_raw_events"),
                                            ("tconnectsync.parser.tandemsource", "decode_raw_events")):
                            try:
                                mod = __import__(modname, fromlist=[fn])
                                ev = list(getattr(mod, fn)(ev))
                                attempts.append((modname + "." + fn, {}, "ok", len(ev)))
                                break
                            except Exception as ex:  # noqa: BLE001
                                attempts.append((modname + "." + fn, {}, f"fail: {ex}", 0))
                    diag["attempts"] = attempts
                    return ev
                except TypeError as ex:
                    attempts.append(("tandemsource.pump_events", kw, f"TypeError: {ex}", 0))
                except Exception as ex:  # noqa: BLE001
                    attempts.append(("tandemsource.pump_events", kw, f"fail: {ex}", 0))
                    break
        except Exception as ex:  # noqa: BLE001
            attempts.append(("tandemsource.pump_event_metadata", {}, f"fail: {ex}", 0))

    # 2) starší rozhraní (controliq / ws2) – therapy_timeline / bolus data
    for path, args in (("controliq.therapy_timeline", (since, now)),
                       ("ws2.therapy_timeline_csv", (since, now)),
                       ("ws2.basaliqtech", (since, now))):
        obj = api
        try:
            for part in path.split("."):
                obj = getattr(obj, part)
            data = obj(*args)
            attempts.append((path, {}, "ok", len(data) if hasattr(data, "__len__") else "?"))
            diag["attempts"] = attempts
            if isinstance(data, dict):
                # controliq therapy_timeline: {"events": [...]}; ws2 csv: {"bolusData": [...], ...}
                evs = []
                for k, v in data.items():
                    if isinstance(v, list):
                        for item in v:
                            if isinstance(item, dict):
                                item = dict(item)
                                item.setdefault("__class__", k)
                            evs.append(item)
                return evs
            return list(data)
        except Exception as ex:  # noqa: BLE001
            attempts.append((path, {}, f"fail: {ex}", 0))
    diag["attempts"] = attempts
    raise TandemError("Nepodařilo se získat události z Tandem Source – viz diagnostika.")


def probe(email, password, region="EU", hours=24):
    """Diagnostika: přihlášení, dostupné metody, vzorek událostí, nalezené bolusy."""
    diag = {"tconnectsync_version": None, "api_attrs": [], "ok": False}
    try:
        import tconnectsync
        diag["tconnectsync_version"] = getattr(tconnectsync, "__version__", "?")
    except ImportError:
        diag["error"] = "tconnectsync není nainstalovaný"
        return diag
    try:
        api = _api(email, password, region)
        diag["api_attrs"] = [a for a in dir(api) if not a.startswith("_")]
        for sub in ("tandemsource", "controliq", "ws2", "android", "webui"):
            o = getattr(api, sub, None)
            if o is not None:
                diag[f"{sub}_methods"] = [a for a in dir(o) if not a.startswith("_") and callable(getattr(o, a, None))]
        try:
            events = fetch_events_v3(api, hours, diag)
            v3 = True
        except (AttributeError, TypeError, KeyError) as ex:
            diag.setdefault("attempts", []).append(("tandemsource v3", {}, f"fail: {ex}", 0))
            events = fetch_events(api, hours, diag)
            v3 = False
        diag["events_count"] = len(events)
        if v3 and not events and diag.get("pump", {}).get("maxDateOfEvents"):
            # data v cloudu končí dřív – ukázat, že stahování funguje, na okně kolem posledního nahrání
            try:
                import arrow
                last = arrow.get(diag["pump"]["maxDateOfEvents"])
                events = list(api.tandemsource.pump_events(diag["device_id"], last.shift(days=-2), last))
                diag["attempts"].append(("pump_events(v3) kolem posledního nahrání", {"do": str(last)}, "ok", len(events)))
                diag["note"] = (f"Poslední data pumpy v Tandem Source jsou z {last.format('D. M. YYYY HH:mm')}. "
                                "Aplikace t:connect na iPhonu nejspíš nenahrává – zkontrolujte, že je spárovaná "
                                "s pumpou a přihlášená.")
            except Exception as ex:  # noqa: BLE001
                diag["attempts"].append(("pump_events(v3) kolem posledního nahrání", {}, f"fail: {ex}", 0))
        classes = {}
        for e in events:
            c = type(e).__name__ if not isinstance(e, dict) else str(e.get("__class__") or e.get("type") or "dict")
            classes[c] = classes.get(c, 0) + 1
        diag["event_classes"] = classes
        sample = [e for e in events if "bolus" in (type(e).__name__ if not isinstance(e, dict) else json.dumps(e, default=str)).lower()][:8]
        diag["bolus_event_samples"] = [_to_plain(e) for e in sample]
        diag["boluses_found"] = extract_boluses_v3(events) if v3 else extract_boluses(events)
        if v3 and not diag["boluses_found"]:
            diag["boluses_found"] = extract_boluses(events)
        diag["ok"] = True
    except Exception as ex:  # noqa: BLE001
        diag["error"] = f"{type(ex).__name__}: {ex}"
    return diag


def sync_once(db, settings, cache={}):
    """Stáhne poslední bolusy z Tandem Source a uloží nové. Vrací počet nových."""
    cfg = settings.get("tandem") or {}
    if not cfg.get("email") or not cfg.get("password"):
        return 0
    key = (cfg["email"], cfg["password"], cfg.get("region", "EU"))
    api = cache.get("api")
    if api is None or cache.get("key") != key:
        api = _api(*key)
        cache.update(api=api, key=key)
    hours = 24
    last = cache.get("last_ok")
    if last:
        hours = max(3, int((time.time() - last) / 3600) + 3)
    try:
        try:
            events = fetch_events_v3(api, hours)
            boluses = extract_boluses_v3(events) or extract_boluses(events)
        except (AttributeError, TypeError, KeyError):
            events = fetch_events(api, hours)
            boluses = extract_boluses(events)
    except Exception:
        cache.pop("api", None)  # při chybě příště znovu přihlásit
        raise
    n = 0
    for b in boluses:
        n += store_bolus(db, b)
    cache["last_ok"] = time.time()
    return n


def store_bolus(db, b):
    """Uloží bolus z pumpy; pokud uživatel stejný bolus už zapsal ručně v aplikaci
    (±20 min, ±0,35 j.), jen ho spáruje, aby se nepočítal dvakrát. Vrací 1, když je nový."""
    existing = db.boluses_between(b["ts"] - 20 * 60, b["ts"] + 20 * 60)
    if any(x.get("ext_id") == b["ext_id"] for x in existing):
        return 0
    for x in existing:
        if x.get("source") == "app" and not x.get("ext_id") and abs(x["units"] - b["units"]) <= 0.35:
            db.link_bolus(x["id"], b["ext_id"], "tandem")
            return 0
    return 1 if db.add_bolus(b["ts"], b["units"], b["kind"], source="tandem", ext_id=b["ext_id"]) else 0
