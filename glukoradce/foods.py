"""Odhad živin jídla podle názvu.

1) Když je v nastavení API klíč Anthropic, zeptá se modelu Claude (přesnější, umí libovolné jídlo
   i „2 rohlíky se šunkou a 300 ml kakaa“).
2) Jinak (nebo při chybě) použije vestavěnou tabulku běžných českých jídel – zdarma, bez internetu.

Hodnoty jsou vždy jen odhad – uživatel je před uložením může upravit.
"""
import difflib
import json
import logging
import re
import unicodedata
import urllib.request

log = logging.getLogger("glukoradce.foods")

DEFAULT_MODEL = "claude-haiku-4-5"

# název: (sacharidy g, tuky g, bílkoviny g, popis porce)
FOODS = {
    "pizza": (95, 35, 35, "celá pizza (32 cm)"),
    "pizza salámová": (95, 40, 38, "celá pizza (32 cm)"),
    "pizza margherita": (90, 30, 30, "celá pizza (32 cm)"),
    "pizza šunková": (92, 32, 40, "celá pizza (32 cm)"),
    "pizza hawai": (98, 30, 38, "celá pizza (32 cm)"),
    "pizza quattro formaggi": (88, 45, 42, "celá pizza (32 cm)"),
    "kousek pizzy": (12, 4, 4, "1 dílek z 8"),
    "svíčková s knedlíkem": (75, 30, 30, "talíř, 5 knedlíků"),
    "guláš s knedlíkem": (70, 30, 35, "talíř, 5 knedlíků"),
    "vepřo knedlo zelo": (75, 35, 35, "talíř, 5 knedlíků"),
    "houskový knedlík": (14, 0.5, 2, "1 plátek"),
    "bramborový knedlík": (16, 0.5, 2, "1 plátek"),
    "smažený sýr s hranolkami": (75, 45, 30, "talíř"),
    "smažený sýr s bramborem": (55, 40, 30, "talíř"),
    "smažený řízek s bramborem": (50, 30, 35, "talíř"),
    "smažený řízek s bramborovým salátem": (65, 45, 35, "talíř"),
    "kuřecí řízek s hranolkami": (60, 35, 40, "talíř"),
    "hranolky": (45, 15, 4, "porce 150 g"),
    "rajská s knedlíkem": (80, 15, 25, "talíř"),
    "koprová s knedlíkem": (75, 20, 25, "talíř"),
    "špagety boloňské": (80, 20, 30, "talíř"),
    "špagety carbonara": (75, 40, 30, "talíř"),
    "lasagne": (60, 35, 35, "porce 400 g"),
    "těstoviny se sýrovou omáčkou": (75, 35, 25, "talíř"),
    "rizoto": (70, 20, 25, "talíř"),
    "smažená rýže": (70, 20, 20, "talíř"),
    "kuřecí kung pao": (60, 25, 35, "talíř s rýží"),
    "kuře na kari s rýží": (65, 20, 35, "talíř"),
    "sushi": (70, 8, 25, "set 8 ks"),
    "kebab v pitě": (60, 30, 35, "1 kebab"),
    "kebab box": (55, 30, 40, "1 box s hranolkami"),
    "hamburger": (45, 25, 25, "1 burger"),
    "cheeseburger": (35, 15, 18, "1 ks (fast food)"),
    "big mac": (45, 25, 25, "1 ks"),
    "hamburger s hranolkami": (85, 40, 30, "menu"),
    "hot dog": (30, 15, 10, "1 ks"),
    "párek v rohlíku": (30, 15, 10, "1 ks"),
    "langoš": (55, 25, 12, "1 ks se sýrem a kečupem"),
    "bramborák": (20, 10, 4, "1 ks"),
    "palačinky": (45, 12, 10, "2 ks s džemem"),
    "lívance": (50, 15, 12, "4 ks"),
    "ovocné knedlíky": (80, 20, 15, "5 ks s tvarohem a máslem"),
    "buchtičky s krémem": (85, 15, 15, "porce"),
    "rohlík": (28, 1.5, 4.5, "1 ks (43 g)"),
    "houska": (28, 1.5, 4.5, "1 ks"),
    "chléb": (25, 1, 4, "1 krajíc (50 g)"),
    "chléb s máslem a šunkou": (25, 10, 10, "1 krajíc"),
    "rohlík s máslem a sýrem": (28, 12, 10, "1 ks"),
    "toast se šunkou a sýrem": (30, 12, 14, "2 plátky"),
    "croissant": (30, 15, 6, "1 ks"),
    "kobliha": (35, 15, 5, "1 ks"),
    "koláč": (35, 10, 5, "1 ks"),
    "bábovka": (30, 12, 4, "1 plátek"),
    "štrůdl": (35, 10, 3, "1 kousek"),
    "dort": (45, 20, 6, "1 řez"),
    "zmrzlina": (25, 10, 4, "2 kopečky"),
    "čokoláda": (25, 15, 3, "1/2 tabulky (50 g)"),
    "tatranka": (20, 10, 3, "1 ks"),
    "müsli tyčinka": (18, 5, 3, "1 ks"),
    "sušenky": (25, 10, 3, "5 ks"),
    "jablko": (15, 0, 0.5, "1 ks"),
    "banán": (25, 0.3, 1.3, "1 ks"),
    "pomeranč": (15, 0, 1, "1 ks"),
    "hroznové víno": (18, 0, 0.5, "hrst (100 g)"),
    "jogurt ovocný": (20, 4, 6, "1 kelímek (150 g)"),
    "jogurt bílý": (7, 5, 6, "1 kelímek (150 g)"),
    "müsli s mlékem": (55, 10, 15, "miska"),
    "ovesná kaše": (45, 8, 12, "miska"),
    "cornflakes s mlékem": (45, 5, 10, "miska"),
    "vejce se slaninou": (2, 25, 20, "2 vejce"),
    "míchaná vejce": (2, 15, 18, "3 vejce"),
    "omeleta": (5, 20, 20, "3 vejce se sýrem"),
    "mléko": (12, 9, 8, "sklenice 250 ml"),
    "kakao": (30, 8, 9, "hrnek 300 ml"),
    "cola": (35, 0, 0, "330 ml"),
    "džus": (25, 0, 1, "250 ml"),
    "pivo": (15, 0, 2, "0,5 l"),
    "nealko pivo": (25, 0, 2, "0,5 l"),
    "čočka s párkem": (55, 20, 30, "talíř"),
    "fazolová polévka": (30, 8, 12, "talíř"),
    "bramboračka": (25, 8, 5, "talíř"),
    "gulášová polévka": (20, 10, 15, "talíř"),
    "kulajda": (20, 15, 8, "talíř"),
    "vývar s nudlemi": (15, 3, 6, "talíř"),
    "bramborová kaše": (35, 8, 4, "porce 250 g"),
    "brambory vařené": (35, 0, 4, "porce 200 g"),
    "rýže vařená": (45, 0.5, 4, "porce 150 g"),
    "těstoviny vařené": (45, 1, 7, "porce 150 g"),
    "kuřecí prsa grilovaná": (0, 5, 45, "porce 200 g"),
    "losos": (0, 20, 40, "porce 200 g"),
    "steak": (0, 25, 50, "porce 250 g"),
    "salát caesar": (20, 30, 30, "velká miska"),
    "zeleninový salát": (10, 10, 3, "miska s dresinkem"),
    "řecký salát": (12, 25, 12, "miska"),
    "wrap kuřecí": (45, 20, 30, "1 ks"),
    "bageta se šunkou": (55, 15, 22, "1 ks"),
    "sendvič": (40, 15, 18, "1 ks"),
    "chlebíček": (12, 8, 5, "1 ks"),
    "tortilla chips": (35, 15, 4, "porce 60 g"),
    "brambůrky": (30, 20, 3, "porce 60 g"),
    "popcorn": (30, 12, 4, "malá porce"),
}


def _norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", " ", s).strip()


_NORM_INDEX = {_norm(k): k for k in FOODS}


def estimate_local(name):
    """Najde nejbližší jídlo ve vestavěné tabulce. Vrací dict nebo None."""
    q = _norm(name)
    if not q:
        return None
    keys = list(_NORM_INDEX)
    hit = None
    if q in _NORM_INDEX:
        hit = q
    else:
        # jídlo obsažené v dotazu („velká pizza salámová“ -> „pizza salámová“) – nejdelší shoda
        contained = [k for k in keys if k in q or q in k]
        if contained:
            hit = max(contained, key=len)
        else:
            close = difflib.get_close_matches(q, keys, n=1, cutoff=0.6)
            hit = close[0] if close else None
    if not hit:
        return None
    orig = _NORM_INDEX[hit]
    c, f, p, label = FOODS[orig]
    return {"carbs": c, "fat": f, "protein": p, "portion_label": label,
            "note": f"Odhad z vestavěné tabulky (podle „{orig}“).", "source": "table"}


def estimate_claude(name, api_key, model=None, timeout=30):
    """Zeptá se modelu Claude přes Anthropic API. Vrací dict nebo vyhodí výjimku."""
    model = model or DEFAULT_MODEL
    prompt = (
        "Jsi nutriční asistent pro diabetika 1. typu v Česku. Pro popsané jídlo odhadni obsah živin "
        "v gramech pro JEDNU typickou porci (nebo pro množství uvedené v popisu). Odpověz POUZE JSON "
        "objektem bez dalšího textu, s klíči: carbs (sacharidy g, číslo), fat (tuky g, číslo), "
        "protein (bílkoviny g, číslo), portion_label (krátký český popis porce, např. 'celá pizza 32 cm' "
        "nebo 'talíř 400 g'), note (jedna česká věta: z čeho odhad vychází, případně nejistota).\n\n"
        f"Jídlo: {name.strip()}"
    )
    body = json.dumps({"model": model, "max_tokens": 300,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"content-type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as ex:
        detail = ex.read().decode(errors="replace")[:300]
        raise RuntimeError(f"Anthropic API {ex.code}: {detail}") from ex
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError("Model nevrátil JSON: " + text[:200])
    j = json.loads(m.group(0))
    out = {}
    for k in ("carbs", "fat", "protein"):
        try:
            out[k] = round(float(j.get(k, 0) or 0), 1)
        except (TypeError, ValueError):
            out[k] = 0.0
    out["portion_label"] = str(j.get("portion_label") or "porce")[:60]
    out["note"] = str(j.get("note") or "")[:300]
    out["source"] = "claude"
    return out


def estimate(name, api_key=None, model=None):
    """Hlavní vstup: Claude když je klíč, jinak tabulka. Vždy vrací dict (může být s 'error')."""
    if not (name or "").strip():
        return {"error": "Zadejte název jídla."}
    err = None
    if api_key:
        try:
            return estimate_claude(name, api_key, model)
        except Exception as ex:  # noqa: BLE001
            err = f"{ex}"
            log.warning("Odhad přes Claude selhal: %s", ex)
    local = estimate_local(name)
    if local:
        if err:
            local["note"] += f" (Claude nedostupný: {err[:120]})"
        return local
    return {"error": ("Jídlo nemám v tabulce – zadejte hodnoty ručně, nebo v Nastavení vložte API klíč "
                      "Anthropic, aby odhad dělal Claude.") + (f" ({err[:120]})" if err else "")}
