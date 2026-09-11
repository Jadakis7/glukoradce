"""Demo režim: simulovaná glykémie a několik jídel, aby šla aplikace vyzkoušet bez Dexcomu."""
import random
import time

from .iob import iob_fraction
from .recommender import fpu

STEP = 300  # 5 min


def _carb_rise(minutes, carbs, icr, isf, dur=180):
    """Kolik mmol/l přidá dávka sacharidů mezi minutou m a m+5 (trojúhelníkový profil)."""
    total = carbs / icr * isf
    if minutes < 0 or minutes >= dur:
        return 0.0
    half = dur / 2
    dens = (minutes / half) if minutes < half else (dur - minutes) / half  # 0..1
    return total * dens * 10 / dur  # plocha trojúhelníku = dur/2, krok 5 min


def _fp_rise(minutes, f, icr, isf, start=120, dur=300):
    total = f * 10 / icr * isf
    if minutes < start or minutes >= start + dur:
        return 0.0
    return total / (dur / 5)


def _ins_drop(minutes, units, isf, dia_h=5, peak=75):
    if minutes < 0 or minutes >= dia_h * 60:
        return 0.0
    return units * isf * (iob_fraction(minutes, dia_h, peak) - iob_fraction(minutes + 5, dia_h, peak))


def simulate(t_start, t_end, meals, boluses, icr=10, isf=2.5, seed=1):
    """meals: [(ts, carbs, fat, protein)], boluses: [(ts, units)]. Vrací [(ts, mmol, trend, 'demo')]."""
    rng = random.Random(seed)
    rows = []
    bg = 6.2
    t = t_start - (t_start % STEP)
    prev = bg
    while t <= t_end:
        d = 0.0
        for mts, c, f, p in meals:
            m = (t - mts) / 60
            d += _carb_rise(m, c, icr, isf) + _fp_rise(m, fpu(f, p), icr, isf)
        for bts, u in boluses:
            d -= _ins_drop((t - bts) / 60, u, isf)
        # návrat k bazální rovnováze + šum
        d += (6.0 - bg) * 0.01 + rng.gauss(0, 0.08)
        bg = max(2.8, bg + d)
        slope = (bg - prev) * 3  # mmol / 15 min
        trend = "Flat"
        if slope > 1.5: trend = "SingleUp"
        elif slope > 0.6: trend = "FortyFiveUp"
        elif slope < -1.5: trend = "SingleDown"
        elif slope < -0.6: trend = "FortyFiveDown"
        rows.append((t, round(bg, 1), trend, "demo"))
        prev = bg
        t += STEP
    return rows


def seed_demo(db, settings):
    """Naplní DB třemi dny simulovaných dat a proběhlými jídly (pizza 3×), pokud je prázdná."""
    if db.latest_glucose():
        return False
    now = int(time.time())
    icr, isf = settings["icr"], settings["isf"]
    pizza = db.add_meal("Pizza (1 celá, ~500 g)", carbs=95, fat=38, protein=40, portion_label="pizza",
                        note="Margherita/salámová, klasická velikost")
    pasta = db.add_meal("Těstoviny s omáčkou", carbs=70, fat=15, protein=20, portion_label="talíř")
    db.add_meal("Chléb se sýrem (2 krajíce)", carbs=40, fat=18, protein=16, portion_label="porce")
    db.add_meal("Jablko", carbs=15, fat=0, protein=0, portion_label="kus")

    day = 86400
    # tři pizzy: nejdřív jen sacharidový bolus (pozdní vzestup), pak lepší a lepší
    events = [
        (now - 3 * day + 5 * 3600, pizza, 1.0, 9.5, 0.0, None),        # bez druhé dávky → pozdní hyper
        (now - 2 * day + 5 * 3600, pizza, 1.0, 9.5, 3.0, 120),         # 2. dávka 3 j.
        (now - 1 * day + 5 * 3600, pizza, 1.0, 10.0, 4.5, 105),        # 2. dávka 4,5 j. dřív
        (now - 2 * day + 12 * 3600, pasta, 1.0, 7.0, 1.5, 120),
    ]
    sim_meals, sim_bol = [], []
    for ts, mid, portions, now_u, late_u, delay in events:
        m = db.meal(mid)
        sim_meals.append((ts, m["carbs"] * portions, m["fat"] * portions, m["protein"] * portions))
        sim_bol.append((ts, now_u))
        eid = db.add_meal_event(meal_id=mid, ts=ts, portions=portions, bg_start=None, iob_start=0,
                                suggested_now=now_u, suggested_late=late_u, suggested_delay_min=delay or 120,
                                explanation={"inputs": {"isf": isf, "target": settings["target"], "icr": icr},
                                             "base": {"carb_units": m["carbs"] * portions / icr, "correction": 0,
                                                      "fp_units_full": fpu(m["fat"] * portions, m["protein"] * portions) * 10 / icr}},
                                note="demo")
        db.add_bolus(ts, now_u, "meal", eid, source="demo")
        db.update_meal_event(eid, given_now=now_u)
        if late_u:
            lts = ts + delay * 60
            db.add_bolus(lts, late_u, "late", eid, source="demo")
            db.update_meal_event(eid, given_late=late_u, given_late_ts=lts)
            sim_bol.append((lts, late_u))
    # snídaně a večeře bez záznamu v appce (jen simulace křivky) – ať to vypadá jako reálný den
    for d in (3, 2, 1, 0):
        base = now - d * day - (now % day)
        for h, c in ((7.5, 45), (18.5, 60)):
            ts = int(base + h * 3600)
            if ts < now:
                sim_meals.append((ts, c, 8, 15)); sim_bol.append((ts - 600, c / icr))
    rows = simulate(now - 3 * day - 3600, now, sim_meals, sim_bol, icr, isf)
    db.add_glucose(rows)
    return True
