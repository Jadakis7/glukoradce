"""Návrh dávky k jídlu: sacharidy + korekce hned, tuky/bílkoviny (FPU) později.

Pravidlový základ (to, co by spočítal člověk) + naučené násobitele pro konkrétní
jídlo. Aplikace nikdy nic nedávkuje, jen navrhuje; návrh je omezen stropy.
"""
import datetime as dt
import math

from .iob import iob_at

# Odhad, o kolik se glykémie posune během ~30 min podle šipky trendu (mmol/l)
TREND_SHIFT = {
    "DoubleUp": 3.0, "SingleUp": 2.0, "FortyFiveUp": 1.0, "Flat": 0.0,
    "FortyFiveDown": -1.0, "SingleDown": -2.0, "DoubleDown": -3.0,
}


def _round_units(u, step=0.1):
    return round(math.floor(u / step + 0.5) * step, 2) if u > 0 else 0.0


def profile_at(settings, ts):
    """ICR/ISF/cíl platné v daném čase (podpora denních segmentů)."""
    icr, isf, target = settings["icr"], settings["isf"], settings["target"]
    segs = settings.get("segments") or []
    if segs:
        local = dt.datetime.fromtimestamp(ts)
        hhmm = local.strftime("%H:%M")
        segs = sorted(segs, key=lambda s: s["start"])
        active = segs[-1]  # segment přes půlnoc
        for s in segs:
            if s["start"] <= hhmm:
                active = s
        icr = float(active.get("icr", icr))
        isf = float(active.get("isf", isf))
        target = float(active.get("target", target))
    return icr, isf, target


def fpu(fat_g, protein_g):
    """Tuko-bílkovinné jednotky (varšavská metoda): 1 FPU = 100 kcal z tuků a bílkovin."""
    return (fat_g * 9 + protein_g * 4) / 100.0


def fpu_duration_h(units_fpu):
    if units_fpu < 1:
        return 0
    if units_fpu < 2:
        return 3
    if units_fpu < 3:
        return 4
    if units_fpu < 4:
        return 5
    return 8


def recommend(db, settings, meal, portions, now_ts, bg=None, trend=None):
    icr, isf, target = profile_at(settings, now_ts)
    carbs = meal["carbs"] * portions
    fat = (meal.get("fat") or 0) * portions
    protein = (meal.get("protein") or 0) * portions
    params = db.meal_params(meal["id"])
    now_mult = params.get("now_mult") or 1.0
    late_mult = params.get("late_mult") or 1.0
    delay = params.get("late_delay_min") or settings["late_delay_min"]

    if bg is None:
        g = db.latest_glucose()
        if g and now_ts - g["ts"] < 20 * 60:
            bg, trend = g["mmol"], g["trend"]
    boluses = db.boluses_between(now_ts - settings["dia_h"] * 3600, now_ts)
    iob = iob_at(boluses, now_ts, settings["dia_h"], settings["peak_min"])

    steps, warnings = [], []

    # 1) sacharidy
    carb_units = carbs / icr
    steps.append(f"Sacharidy {carbs:.0f} g ÷ ICR {icr:g} g/j = {carb_units:.1f} j.")
    if abs(now_mult - 1) > 0.02:
        steps.append(f"Naučená úprava pro toto jídlo ×{now_mult:.2f} (z {params.get('n_events', 0)} jídel)"
                     f" → {carb_units * now_mult:.1f} j.")
    meal_units = carb_units * now_mult

    # 2) korekce podle glykémie, trendu a aktivního inzulínu
    correction = 0.0
    if bg is not None:
        shift = TREND_SHIFT.get(trend or "Flat", 0.0)
        bg_pred = bg + shift
        raw = (bg_pred - target) / isf
        if bg < settings["hypo"]:
            warnings.append(f"Glykémie {bg:.1f} je pod {settings['hypo']:.1f} – nejdřív ošetřete hypoglykémii, "
                            f"bolus k jídlu zvažte až po vzestupu.")
        if raw >= 0:
            correction = max(0.0, raw - iob)
            correction = min(correction, settings["max_correction"])
            steps.append(f"Korekce: ({bg:.1f}{'%+.1f trend' % shift if shift else ''} − cíl {target:g}) ÷ ISF {isf:g}"
                         f" = {raw:.1f} j., minus aktivní inzulín {iob:.1f} j. → {correction:.1f} j.")
        else:
            reduce = min(-raw, meal_units * 0.5)
            meal_units -= reduce
            steps.append(f"Glykémie pod cílem: snižuji dávku k jídlu o {reduce:.1f} j.")
    else:
        warnings.append("Nemám aktuální glykémii z Dexcomu – korekce se nepočítá.")
        if iob > 0.2:
            steps.append(f"Aktivní inzulín {iob:.1f} j. (nezapočten, chybí glykémie).")

    now_units = meal_units + correction

    # 3) tuky a bílkoviny → odložená dávka
    f = fpu(fat, protein)
    fp_full = f * (10.0 / icr)          # 1 FPU ≈ inzulín jako na 10 g sacharidů
    fp_units = fp_full * settings["fpu_factor"] * late_mult
    late_units = 0.0
    if f >= 0.5:
        steps.append(f"Tuky {fat:.0f} g, bílkoviny {protein:.0f} g = {f:.1f} FPU → plné krytí {fp_full:.1f} j., "
                     f"kryjeme {settings['fpu_factor'] * 100:.0f} %" +
                     (f" × naučené {late_mult:.2f}" if abs(late_mult - 1) > 0.02 else "") + f" = {fp_units:.1f} j.")
        if fp_units >= settings["late_min_units"]:
            late_units = fp_units
            steps.append(f"Druhou dávku podejte za {delay} min (očekávaný vzestup z tuků/bílkovin trvá "
                         f"~{fpu_duration_h(f)} h).")
        else:
            steps.append("Odložená dávka je příliš malá, nenavrhuji ji.")

    # 4) bezpečnostní stropy
    cap = settings["max_bolus"]
    if now_units > cap:
        warnings.append(f"Návrh {now_units:.1f} j. překračuje strop {cap:g} j. – omezeno. Zkontrolujte zadání.")
        now_units = cap
    if late_units > cap:
        late_units = cap

    now_units = _round_units(now_units)
    late_units = _round_units(late_units)
    if bg is not None and bg < settings["hypo"]:
        now_units = 0.0

    return {
        "inputs": {"carbs": round(carbs, 1), "fat": round(fat, 1), "protein": round(protein, 1),
                   "fpu": round(f, 2), "bg": bg, "trend": trend, "iob": iob,
                   "icr": icr, "isf": isf, "target": target, "portions": portions},
        "base": {"carb_units": round(carb_units, 2), "correction": round(correction, 2),
                 "fp_units_full": round(fp_full, 2), "fp_units": round(fp_units, 2)},
        "learned": {"now_mult": now_mult, "late_mult": late_mult, "late_delay_min": delay,
                    "n_events": params.get("n_events", 0)},
        "now": now_units,
        "late": late_units,
        "late_delay_min": delay,
        "late_at": now_ts + delay * 60 if late_units else None,
        "steps": steps,
        "warnings": warnings,
    }
