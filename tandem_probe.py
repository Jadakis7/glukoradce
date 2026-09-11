#!/usr/bin/env python3
"""Diagnostika připojení k Tandem Source.

    .venv/bin/python tandem_probe.py                 # použije účet z Nastavení Glukorádce
    .venv/bin/python tandem_probe.py email heslo EU  # nebo zadaný účet

Vypíše souhrn a uloží podrobnosti do data/tandem_probe.json (bez hesla).
Tento soubor můžete poslat k doladění parseru.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from glukoradce.db import DB  # noqa: E402
from glukoradce import tandem_sync  # noqa: E402

BASE = Path(__file__).parent


def main():
    if len(sys.argv) >= 3:
        email, password = sys.argv[1], sys.argv[2]
        region = sys.argv[3] if len(sys.argv) > 3 else "EU"
    else:
        s = DB(BASE / "data" / "glukoradce.sqlite").get_settings()
        cfg = s.get("tandem") or {}
        email, password, region = cfg.get("email"), cfg.get("password"), cfg.get("region", "EU")
        if not email:
            print("V Nastavení není účet Tandem. Zadejte: tandem_probe.py email heslo [EU|US]")
            return 1
    print(f"Přihlašuji se do Tandem Source jako {email} (region {region})…")
    d = tandem_sync.probe(email, password, region, hours=48)
    out = BASE / "data" / "tandem_probe.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(d, ensure_ascii=False, indent=1, default=str))
    print("tconnectsync:", d.get("tconnectsync_version"))
    if d.get("error"):
        print("CHYBA:", d["error"])
    print("Pokusy:")
    for a in d.get("attempts", []):
        print("  ", a)
    print("Počet událostí:", d.get("events_count"))
    print("Typy událostí:", json.dumps(d.get("event_classes"), ensure_ascii=False))
    b = d.get("boluses_found") or []
    print(f"Nalezené bolusy za 48 h: {len(b)}")
    for r in b[-15:]:
        import datetime as dt
        print("  ", dt.datetime.fromtimestamp(r["ts"]).strftime("%d.%m. %H:%M"), f"{r['units']:.2f} j.", r["kind"],
              f"({r['carbs']:g} g)" if r.get("carbs") else "")
    print("Podrobnosti uloženy do", out)
    return 0 if d.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
