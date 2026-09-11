"""Aktivní inzulín (IOB) – exponenciální model křivky působení (jako OpenAPS/Loop)."""
import math


def _params(dia_min, peak_min):
    tau = peak_min * (1 - peak_min / dia_min) / (1 - 2 * peak_min / dia_min)
    a = 2 * tau / dia_min
    S = 1 / (1 - a + (1 + a) * math.exp(-dia_min / tau))
    return tau, a, S


def iob_fraction(minutes_since, dia_h=5.0, peak_min=75):
    """Podíl dávky, který ještě působí po `minutes_since` minutách."""
    dia_min = dia_h * 60
    if minutes_since <= 0:
        return 1.0
    if minutes_since >= dia_min:
        return 0.0
    tau, a, S = _params(dia_min, peak_min)
    t = minutes_since
    iob = 1 - S * (1 - a) * ((t * t / (tau * dia_min * (1 - a)) - t / tau - 1) * math.exp(-t / tau) + 1)
    return max(0.0, min(1.0, iob))


def iob_at(boluses, ts, dia_h=5.0, peak_min=75):
    """boluses: iterable dictů {ts, units}. Vrací celkový IOB v čase ts."""
    total = 0.0
    for b in boluses:
        mins = (ts - b["ts"]) / 60.0
        if 0 <= mins < dia_h * 60:
            total += b["units"] * iob_fraction(mins, dia_h, peak_min)
    return round(total, 2)
