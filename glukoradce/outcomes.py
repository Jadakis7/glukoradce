"""Vyhodnocení jídla z CGM křivky a učení parametrů pro dané jídlo.

Princip: z průběhu glykémie po jídle zpětně odhadneme, jaká dávka by byla
"ideální", a naučené násobitele posuneme malým krokem tím směrem. Učí se jen
z jídel, kde je zaznamenaná skutečná dávka a dostatek dat, a bez překryvu
s jiným jídlem. Změny jsou omezené (learn_step) a násobitele mají meze.
"""
import time

WINDOW_H = 6.0          # jak dlouho po jídle hodnotíme
DAMP = 0.5              # tlumení zpětného odhadu ideální dávky


def _interp(points, t):
    """Lineární interpolace glykémie v čase t (unix s); None, když chybí data ±20 min."""
    prev = nxt = None
    for p in points:
        if p["ts"] <= t:
            prev = p
        elif nxt is None:
            nxt = p
            break
    if prev and abs(prev["ts"] - t) <= 20 * 60 and (nxt is None or abs(nxt["ts"] - t) > abs(prev["ts"] - t)):
        if nxt and nxt["ts"] - prev["ts"] <= 40 * 60:
            w = (t - prev["ts"]) / (nxt["ts"] - prev["ts"])
            return prev["mmol"] + w * (nxt["mmol"] - prev["mmol"])
        return prev["mmol"]
    if nxt and abs(nxt["ts"] - t) <= 20 * 60:
        return nxt["mmol"]
    return None


def metrics(points, t0, settings):
    """Popisné metriky křivky po jídle."""
    pts = [p for p in points if t0 <= p["ts"] <= t0 + WINDOW_H * 3600]
    if len(pts) < 10:
        return None
    expected = WINDOW_H * 12
    coverage = min(1.0, len(pts) / expected)
    peak = max(pts, key=lambda p: p["mmol"])
    low = min(pts, key=lambda p: p["mmol"])
    early = [p for p in pts if p["ts"] <= t0 + 3 * 3600]
    late = [p for p in pts if p["ts"] > t0 + 3 * 3600]
    above = sum(1 for p in pts if p["mmol"] > settings["high"]) * 5
    below = sum(1 for p in pts if p["mmol"] < settings["hypo"]) * 5
    in_range = sum(1 for p in pts if settings["hypo"] <= p["mmol"] <= settings["high"]) / len(pts)
    return {
        "coverage": round(coverage, 2),
        "bg_start": _interp(points, t0),
        "bg_1h": _interp(points, t0 + 3600),
        "bg_2h": _interp(points, t0 + 2 * 3600),
        "bg_3h": _interp(points, t0 + 3 * 3600),
        "bg_4h": _interp(points, t0 + 4 * 3600),
        "bg_5h": _interp(points, t0 + 5 * 3600),
        "bg_6h": _interp(points, t0 + 6 * 3600),
        "peak": peak["mmol"], "peak_min": int((peak["ts"] - t0) / 60),
        "min": low["mmol"], "min_min": int((low["ts"] - t0) / 60),
        "min_early": min((p["mmol"] for p in early), default=None),
        "min_late": min((p["mmol"] for p in late), default=None),
        "min_above_high": above, "min_below_hypo": below,
        "time_in_range": round(in_range, 2),
    }


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def evaluate_event(db, settings, event, now_ts=None):
    """Vyhodnotí jednu událost jídla; vrátí outcome dict a případně upraví meal_params."""
    now_ts = now_ts or time.time()
    t0 = event["ts"]
    if now_ts < t0 + WINDOW_H * 3600 + 15 * 60:
        return None  # ještě není čas
    points = db.glucose_between(t0 - 30 * 60, t0 + WINDOW_H * 3600 + 60)
    m = metrics(points, t0, settings)
    outcome = {"evaluated_ts": int(now_ts), "learned": False}
    if not m:
        outcome.update(verdict="Nehodnoceno – málo dat z CGM.", metrics=None)
        return _finish(db, event, outcome)
    outcome["metrics"] = m
    expl = event.get("explanation") or {}
    inputs, base = expl.get("inputs", {}), expl.get("base", {})
    isf = inputs.get("isf") or settings["isf"]
    target = inputs.get("target") or settings["target"]

    # automatické korekce pumpy (Control-IQ) v okně – pumpa musela přidat inzulín
    auto = db.boluses_between(t0, t0 + WINDOW_H * 3600)
    auto_early = sum(b["units"] for b in auto if b["kind"] == "auto" and b["ts"] <= t0 + 3 * 3600)
    auto_late = sum(b["units"] for b in auto if b["kind"] == "auto" and b["ts"] > t0 + 3 * 3600)
    outcome["auto_corrections"] = round(auto_early + auto_late, 2)

    # slovní hodnocení
    verdicts = []
    high = settings["high"]
    early_high = (m["bg_2h"] is not None and m["bg_2h"] > high) or \
        (m["peak_min"] <= 150 and m["peak"] > high + 1)
    late_high = (m["bg_5h"] is not None and m["bg_5h"] > high) or \
        (m["bg_4h"] is not None and m["bg_4h"] > high and (m["bg_2h"] is None or m["bg_4h"] > m["bg_2h"]))
    hypo_early = m["min_early"] is not None and m["min_early"] < settings["hypo"]
    hypo_late = m["min_late"] is not None and m["min_late"] < settings["hypo"]
    if hypo_early:
        verdicts.append("hypo do 3 h – první dávka byla nejspíš moc")
    elif early_high:
        verdicts.append("vysoký vrchol brzy po jídle – první dávka málo (nebo pozdě)")
    if hypo_late:
        verdicts.append("hypo po 3 h – druhá dávka byla nejspíš moc")
    elif late_high:
        verdicts.append("pozdní vzestup – druhá dávka málo nebo pozdě")
    if not verdicts:
        verdicts.append("dobrý průběh")
    if auto_early + auto_late > 0.5:
        verdicts.append(f"pumpa přidala automaticky {auto_early + auto_late:.1f} j.")
    outcome["verdict"] = "; ".join(verdicts).capitalize() + "."

    # ---------- učení ----------
    skip = None
    given_now = event.get("given_now")
    if given_now is None:
        skip = "není zapsaná skutečná dávka"
    elif m["coverage"] < 0.7:
        skip = "málo dat z CGM"
    elif _overlap(db, event):
        skip = "překryv s jiným jídlem"
    elif m["bg_start"] is not None and (m["bg_start"] > 13 or m["bg_start"] < settings["hypo"]):
        skip = "neobvyklá výchozí glykémie"
    if skip:
        outcome["learn_note"] = f"Neučím se: {skip}."
        return _finish(db, event, outcome)

    params = db.meal_params(event["meal_id"])
    step = settings["learn_step"]
    lo, hi = settings["mult_min"], settings["mult_max"]
    carb_units = base.get("carb_units") or 0
    correction = base.get("correction") or 0
    fp_base = (base.get("fp_units_full") or 0) * settings["fpu_factor"]
    notes = []

    def _delta(obs, cur, was_high, was_low):
        """Krok násobitele směrem k pozorování. Zpětný odhad ideálu je při hyperglykémii
        jen dolní mez (nelineární odpověď), při hypoglykémii jen horní mez – proto po
        vysoké křivce nikdy nesnižujeme a po hypu nikdy nezvyšujeme. Když uživatel dal
        jinou dávku, než jsme navrhli, a odhad ukazuje proti směru výsledku, neměníme nic."""
        d = _clamp(obs - cur, -step, step)
        if was_high:
            d = max(d, 0.0)
        if was_low:
            d = min(d, 0.0)
        return d

    # 1) první dávka: zpětný odhad ideálu z glykémie ve 2 h (+ hypo do 3 h, + auto korekce)
    new_now = params["now_mult"]
    if carb_units > 0.5 and m["bg_2h"] is not None:
        eff_now = given_now + auto_early
        ideal = eff_now + DAMP * (m["bg_2h"] - target) / isf
        if hypo_early:
            ideal = eff_now - 0.7 * (target - m["min_early"]) / isf
        ideal_meal_part = max(0.0, ideal - correction)
        obs = ideal_meal_part / carb_units
        delta = _delta(obs, params["now_mult"], early_high, hypo_early)
        new_now = round(_clamp(params["now_mult"] + delta, lo, hi), 3)
        notes.append(f"první dávka {eff_now:.1f} j. → odhad ideálu {ideal:.1f} j., násobitel "
                     f"{params['now_mult']:.2f} → {new_now:.2f}")

    # 2) druhá dávka: z glykémie v 5 h (+ hypo po 3 h)
    new_late = params["late_mult"]
    new_delay = params.get("late_delay_min") or settings["late_delay_min"]
    given_late = event.get("given_late") or 0.0
    if fp_base > 0.3 and m["bg_5h"] is not None:
        eff_late = given_late + auto_late
        ideal = eff_late + DAMP * (m["bg_5h"] - target) / isf
        if hypo_late:
            ideal = eff_late - 0.7 * (target - m["min_late"]) / isf
        obs = max(0.0, ideal) / fp_base
        delta = _delta(obs, params["late_mult"], late_high, hypo_late)
        new_late = round(_clamp(params["late_mult"] + delta, lo, hi), 3)
        notes.append(f"druhá dávka {eff_late:.1f} j. → odhad ideálu {max(0, ideal):.1f} j., násobitel "
                     f"{params['late_mult']:.2f} → {new_late:.2f}")
        # načasování: vzestup už ve 3 h, ale ve 2 h v pořádku → dřív; hypo mezi 2–3,5 h → později
        if m["bg_3h"] and m["bg_2h"] and m["bg_3h"] > settings["high"] and m["bg_2h"] <= settings["high"]:
            new_delay = max(60, new_delay - 15)
            notes.append(f"vzestup už ve 3 h → druhou dávku dřív ({new_delay} min)")
        elif hypo_late and m["min_min"] < 210 and given_late > 0:
            new_delay = min(180, new_delay + 15)
            notes.append(f"hypo krátce po druhé dávce → druhou dávku později ({new_delay} min)")

    hist = params["history"]
    hist.append({"event_id": event["id"], "ts": t0, "given_now": given_now, "given_late": given_late,
                 "bg_start": m["bg_start"], "bg_2h": m["bg_2h"], "bg_5h": m["bg_5h"], "peak": m["peak"],
                 "min": m["min"], "verdict": outcome["verdict"],
                 "now_mult": [params["now_mult"], new_now], "late_mult": [params["late_mult"], new_late],
                 "late_delay_min": new_delay})
    hist = hist[-30:]
    db.set_meal_params(event["meal_id"], new_now, new_late, new_delay, params.get("n_events", 0) + 1, hist)
    outcome["learned"] = True
    outcome["learn_note"] = "; ".join(notes) if notes else "Beze změny parametrů."
    outcome["params_after"] = {"now_mult": new_now, "late_mult": new_late, "late_delay_min": new_delay}
    return _finish(db, event, outcome)


def _overlap(db, event):
    t0 = event["ts"]
    others = db.meal_events(since=t0 - 4 * 3600, limit=100)
    for o in others:
        if o["id"] != event["id"] and t0 - 4 * 3600 <= o["ts"] <= t0 + WINDOW_H * 3600:
            return True
    return False


def _finish(db, event, outcome):
    db.update_meal_event(event["id"], outcome=outcome, evaluated=1)
    return outcome


def evaluate_pending(db, settings, now_ts=None):
    now_ts = now_ts or time.time()
    done = []
    for e in db.unevaluated_events(now_ts - WINDOW_H * 3600 - 15 * 60):
        r = evaluate_event(db, settings, e, now_ts)
        if r:
            done.append((e["id"], r))
    return done
