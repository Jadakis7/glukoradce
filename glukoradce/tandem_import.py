"""Import bolusů z pumpy Tandem t:slim X2.

Podporované vstupy (soubor CSV):
 1. Export z Tandem Source / t:connect (Reports → Export / "Download data").
    Importér hledá sloupce podle názvu (čas, jednotky, typ události) – formát se
    u Tandemu čas od času mění, proto je heuristický. Pokud něco nerozpozná,
    vypíše sloupce a nic neuloží.
 2. Jednoduchý vlastní CSV: `ts,units,kind` (ts = ISO datum nebo unix s,
    kind = meal | late | correction | auto | manual).

Automatické korekce Control-IQ (kind="auto") jsou důležité: říkají, kolik
inzulínu musela pumpa přidat sama – to se započítá do aktivního inzulínu i do
učení.
"""
import csv
import datetime as dt
import hashlib
import io
import re

_TS_KEYS = ("eventdatetime", "event date", "timestamp", "date/time", "datetime", "time", "ts", "date")
_UNITS_KEYS = ("insulindelivered", "insulin delivered", "bolus delivered", "delivered units", "units", "amount",
               "actual total bolus requested", "bolusamount")
_TYPE_KEYS = ("event type", "eventtype", "type", "description", "bolus type", "kind", "bolustype")


def _parse_ts(s):
    s = str(s).strip()
    if re.fullmatch(r"\d{9,11}", s):
        return int(s)
    if re.fullmatch(r"\d{12,14}", s):
        return int(s) // 1000
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M",
                "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            return int(dt.datetime.strptime(s[:len(fmt) + 8].strip(), fmt).timestamp())
        except ValueError:
            continue
    try:
        return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _find(header, keys):
    low = [h.strip().lower() for h in header]
    for k in keys:
        for i, h in enumerate(low):
            if h == k:
                return i
    for k in keys:
        for i, h in enumerate(low):
            if k in h:
                return i
    return None


def _kind(desc):
    d = (desc or "").lower()
    if "auto" in d or "control-iq" in d or "control iq" in d or "automatic" in d:
        return "auto"
    if "correction" in d or "korek" in d:
        return "correction"
    if "meal" in d or "food" in d or "carb" in d or "jídl" in d:
        return "meal"
    return "manual"


def parse_csv(text):
    """Vrací (rows, info). rows = list of dict(ts, units, kind, ext_id)."""
    # Tandem exporty mívají před hlavičkou několik informačních řádků – najdi hlavičku
    lines = text.splitlines()
    start = 0
    for i, ln in enumerate(lines[:40]):
        l = ln.lower()
        if ("units" in l or "delivered" in l or "amount" in l) and ("time" in l or "date" in l or "ts" in l):
            start = i
            break
    reader = csv.reader(io.StringIO("\n".join(lines[start:])))
    try:
        header = next(reader)
    except StopIteration:
        return [], {"error": "prázdný soubor"}
    ti, ui, ki = _find(header, _TS_KEYS), _find(header, _UNITS_KEYS), _find(header, _TYPE_KEYS)
    if ti is None or ui is None:
        return [], {"error": "nerozpoznané sloupce", "header": header}
    rows, skipped = [], 0
    for r in reader:
        if len(r) <= max(ti, ui):
            continue
        ts = _parse_ts(r[ti])
        try:
            units = float(str(r[ui]).replace(",", "."))
        except ValueError:
            skipped += 1
            continue
        if ts is None or units <= 0:
            skipped += 1
            continue
        desc = r[ki] if ki is not None and ki < len(r) else ""
        kind = desc.strip().lower() if desc.strip().lower() in ("meal", "late", "correction", "auto", "manual") \
            else _kind(desc)
        ext = "csv:" + hashlib.sha1(f"{ts}|{units}|{kind}".encode()).hexdigest()[:16]
        rows.append({"ts": ts, "units": round(units, 2), "kind": kind, "ext_id": ext})
    return rows, {"header": header, "parsed": len(rows), "skipped": skipped}


def import_csv(db, text):
    rows, info = parse_csv(text)
    n = 0
    for r in rows:
        if db.add_bolus(r["ts"], r["units"], r["kind"], source="tandem_csv", ext_id=r["ext_id"]):
            n += 1
    info["imported"] = n
    return info
