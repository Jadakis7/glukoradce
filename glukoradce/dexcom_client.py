"""Čtení glykémií z Dexcom Share (stejné rozhraní používá aplikace Dexcom Follow).

Použije se knihovna pydexcom, pokud je nainstalovaná (doporučeno – sleduje změny
na straně Dexcomu). Jinak se použije vestavěný minimální klient.

Vyžaduje zapnuté sdílení v aplikaci Dexcom G7 (Sdílení/Share) a přihlašovací
údaje k účtu Dexcom (ne Follow). Pro účty mimo USA (ČR) je region "ous".
"""
import datetime as dt
import re
import time

import requests

MGDL_TO_MMOL = 0.0555

_HOSTS = {
    "us": "https://share2.dexcom.com/ShareWebServices/Services",
    "ous": "https://shareous1.dexcom.com/ShareWebServices/Services",
    "jp": "https://share.dexcom.jp/ShareWebServices/Services",
}
_APP_ID = "d89443d2-327c-4a6f-89e5-496bbb0317db"

_TREND_NAMES = {
    1: "DoubleUp", 2: "SingleUp", 3: "FortyFiveUp", 4: "Flat",
    5: "FortyFiveDown", 6: "SingleDown", 7: "DoubleDown", 8: "NotComputable", 9: "RateOutOfRange",
}
TREND_ARROWS = {
    "DoubleUp": "⇈", "SingleUp": "↑", "FortyFiveUp": "↗", "Flat": "→",
    "FortyFiveDown": "↘", "SingleDown": "↓", "DoubleDown": "⇊", "NotComputable": "?", "RateOutOfRange": "?",
    "None": "", None: "",
}


class DexcomError(Exception):
    pass


class _BuiltinShareClient:
    def __init__(self, username, password, region="ous"):
        self.base = _HOSTS.get(region, _HOSTS["ous"])
        self.username, self.password = username, password
        self.session_id = None
        self.http = requests.Session()
        self.http.headers.update({"Accept": "application/json", "Content-Type": "application/json",
                                  "User-Agent": "Dexcom Share/3.0.2.11 CFNetwork/711.2.23 Darwin/14.0.0"})

    def _post(self, path, payload):
        r = self.http.post(f"{self.base}/{path}", json=payload, timeout=20)
        if r.status_code >= 400:
            raise DexcomError(f"Dexcom Share {path}: HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    def login(self):
        acc = self._post("General/AuthenticatePublisherAccount",
                         {"accountName": self.username, "password": self.password, "applicationId": _APP_ID})
        if not isinstance(acc, str) or acc.strip("0-") == "":
            raise DexcomError(f"Přihlášení selhalo: {acc}")
        sid = self._post("General/LoginPublisherAccountById",
                         {"accountId": acc, "password": self.password, "applicationId": _APP_ID})
        if not isinstance(sid, str) or sid.strip("0-") == "":
            raise DexcomError(f"Přihlášení selhalo (session): {sid}")
        self.session_id = sid

    def readings(self, minutes=1440, max_count=288):
        if not self.session_id:
            self.login()
        try:
            data = self._post("Publisher/ReadPublisherLatestGlucoseValues",
                              {"sessionId": self.session_id, "minutes": minutes, "maxCount": max_count})
        except DexcomError:
            self.login()
            data = self._post("Publisher/ReadPublisherLatestGlucoseValues",
                              {"sessionId": self.session_id, "minutes": minutes, "maxCount": max_count})
        out = []
        for d in data or []:
            m = re.search(r"Date\((\d+)", d.get("WT") or d.get("ST") or "")
            if not m:
                continue
            ts = int(m.group(1)) // 1000
            trend = d.get("Trend")
            if isinstance(trend, int):
                trend = _TREND_NAMES.get(trend)
            out.append((ts, round(d["Value"] * MGDL_TO_MMOL, 1), trend, "dexcom"))
        return out


class _PydexcomClient:
    def __init__(self, username, password, region="ous"):
        from pydexcom import Dexcom  # noqa
        try:
            self.dx = Dexcom(username=username, password=password, region=region)
        except TypeError:  # starší verze pydexcom
            self.dx = Dexcom(username, password, ous=(region != "us"))

    def readings(self, minutes=1440, max_count=288):
        out = []
        for r in self.dx.get_glucose_readings(minutes=minutes, max_count=max_count) or []:
            d = r.datetime
            if d.tzinfo is None:
                ts = int(d.timestamp())
            else:
                ts = int(d.astimezone(dt.timezone.utc).timestamp())
            trend = getattr(r, "trend_direction", None) or getattr(r, "trend_description", None)
            out.append((ts, round(r.value * MGDL_TO_MMOL, 1), trend, "dexcom"))
        return out


def make_client(username, password, region="ous"):
    if not username or not password:
        raise DexcomError("Chybí přihlašovací údaje k Dexcom účtu (Nastavení).")
    try:
        import pydexcom  # noqa: F401
        return _PydexcomClient(username, password, region)
    except ImportError:
        return _BuiltinShareClient(username, password, region)


def sync_once(db, settings, client_cache={}):
    """Stáhne poslední hodnoty a uloží nové do DB. Vrací počet nových řádků."""
    cfg = settings.get("dexcom") or {}
    key = (cfg.get("username"), cfg.get("password"), cfg.get("region"))
    client = client_cache.get("client")
    if client is None or client_cache.get("key") != key:
        client = make_client(*key)
        client_cache.update(client=client, key=key)
    last = db.latest_glucose()
    minutes = 1440
    if last:
        minutes = int(min(1440, max(30, (time.time() - last["ts"]) / 60 + 15)))
    rows = client.readings(minutes=minutes, max_count=288)
    return db.add_glucose(rows)
