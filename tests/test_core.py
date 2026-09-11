"""Spuštění: python3 -m pytest tests  (nebo python3 tests/test_core.py)"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from glukoradce.db import DB  # noqa: E402
from glukoradce.iob import iob_fraction, iob_at  # noqa: E402
from glukoradce import recommender, outcomes, tandem_import  # noqa: E402
from glukoradce.demo import seed_demo, simulate  # noqa: E402


def fresh_db():
    path = os.path.join(tempfile.mkdtemp(), "t.sqlite")
    return DB(path)


class IOBTests(unittest.TestCase):
    def test_curve_monotone(self):
        prev = 1.0
        for m in range(0, 301, 5):
            f = iob_fraction(m, 5, 75)
            self.assertLessEqual(f, prev + 1e-9)
            prev = f
        self.assertEqual(iob_fraction(300, 5, 75), 0.0)
        self.assertAlmostEqual(iob_fraction(0), 1.0)

    def test_iob_sum(self):
        now = 1_000_000
        b = [{"ts": now - 3600, "units": 4}, {"ts": now - 6 * 3600, "units": 10}]
        v = iob_at(b, now, 5, 75)
        self.assertGreater(v, 1.5)
        self.assertLess(v, 4)


class RecommenderTests(unittest.TestCase):
    def setUp(self):
        self.db = fresh_db()
        self.s = self.db.get_settings()
        self.mid = self.db.add_meal("Pizza", 95, 38, 40)

    def test_basic(self):
        now = int(time.time())
        self.db.add_glucose([(now - 60, 7.8, "Flat", "t")])
        r = recommender.recommend(self.db, self.s, self.db.meal(self.mid), 1.0, now)
        self.assertAlmostEqual(r["base"]["carb_units"], 9.5)
        self.assertAlmostEqual(r["base"]["correction"], 0.72, places=2)
        self.assertEqual(r["now"], 10.2)
        self.assertAlmostEqual(r["inputs"]["fpu"], 5.02)
        self.assertEqual(r["late"], 2.5)  # 5.02 FPU × 1 j. × 50 %

    def test_hypo_blocks(self):
        now = int(time.time())
        r = recommender.recommend(self.db, self.s, self.db.meal(self.mid), 1.0, now, bg=3.5, trend="Flat")
        self.assertEqual(r["now"], 0.0)
        self.assertTrue(r["warnings"])

    def test_cap(self):
        now = int(time.time())
        r = recommender.recommend(self.db, self.s, self.db.meal(self.mid), 5.0, now, bg=6.0)
        self.assertLessEqual(r["now"], self.s["max_bolus"])
        self.assertTrue(any("strop" in w for w in r["warnings"]))

    def test_below_target_reduces(self):
        now = int(time.time())
        r = recommender.recommend(self.db, self.s, self.db.meal(self.mid), 1.0, now, bg=4.5, trend="Flat")
        self.assertLess(r["now"], 9.5)

    def test_segments(self):
        s = dict(self.s)
        s["segments"] = [{"start": "06:00", "icr": 8, "isf": 2}, {"start": "12:00", "icr": 12, "isf": 3}]
        import datetime as dt
        ts = int(dt.datetime.now().replace(hour=7, minute=0).timestamp())
        self.assertEqual(recommender.profile_at(s, ts)[0], 8)
        ts = int(dt.datetime.now().replace(hour=13, minute=0).timestamp())
        self.assertEqual(recommender.profile_at(s, ts)[0], 12)
        ts = int(dt.datetime.now().replace(hour=2, minute=0).timestamp())
        self.assertEqual(recommender.profile_at(s, ts)[0], 12)  # přes půlnoc platí poslední


class LearningTests(unittest.TestCase):
    def _run(self, given_now, given_late, delay=120):
        db = fresh_db()
        s = db.get_settings()
        mid = db.add_meal("Pizza", 95, 38, 40)
        t0 = int(time.time()) - 8 * 3600
        m = db.meal(mid)
        rec = {"inputs": {"isf": 2.5, "target": 6.0, "icr": 10},
               "base": {"carb_units": 9.5, "correction": 0, "fp_units_full": 5.02}}
        eid = db.add_meal_event(meal_id=mid, ts=t0, portions=1, suggested_now=9.5, suggested_late=2.5,
                                suggested_delay_min=delay, explanation=rec)
        db.update_meal_event(eid, given_now=given_now)
        bol = [(t0, given_now)]
        if given_late:
            db.update_meal_event(eid, given_late=given_late, given_late_ts=t0 + delay * 60)
            bol.append((t0 + delay * 60, given_late))
        db.add_glucose(simulate(t0 - 3600, t0 + 7 * 3600, [(t0, 95, 38, 40)], bol))
        r = outcomes.evaluate_event(db, s, db.meal_event(eid))
        return r, db.meal_params(mid)

    def test_followed_and_high_increases(self):
        r, p = self._run(9.5, 2.5)
        self.assertTrue(r["learned"])
        self.assertGreater(p["now_mult"], 1.0)
        self.assertGreater(p["late_mult"], 1.0)

    def test_own_underdose_never_decreases(self):
        r, p = self._run(6.0, 0.0)
        self.assertTrue(r["learned"])
        self.assertGreaterEqual(p["now_mult"], 1.0)
        self.assertGreaterEqual(p["late_mult"], 1.0)

    def test_hypo_never_increases(self):
        r, p = self._run(16.0, 8.0)
        self.assertTrue(r["learned"])
        self.assertLessEqual(p["now_mult"], 1.0)
        self.assertLessEqual(p["late_mult"], 1.0)

    def test_step_bounded(self):
        _, p = self._run(2.0, 0.0)
        self.assertLessEqual(p["now_mult"], 1.1 + 1e-9)

    def test_no_dose_no_learning(self):
        db = fresh_db(); s = db.get_settings()
        mid = db.add_meal("X", 50)
        t0 = int(time.time()) - 8 * 3600
        eid = db.add_meal_event(meal_id=mid, ts=t0, portions=1, explanation={})
        db.add_glucose(simulate(t0 - 3600, t0 + 7 * 3600, [(t0, 50, 0, 0)], []))
        r = outcomes.evaluate_event(db, s, db.meal_event(eid))
        self.assertFalse(r["learned"])
        self.assertEqual(db.meal_params(mid)["now_mult"], 1.0)

    def test_too_early(self):
        db = fresh_db(); s = db.get_settings()
        mid = db.add_meal("X", 50)
        eid = db.add_meal_event(meal_id=mid, ts=int(time.time()) - 3600, portions=1, explanation={})
        self.assertIsNone(outcomes.evaluate_event(db, s, db.meal_event(eid)))


class ImportTests(unittest.TestCase):
    def test_simple_csv(self):
        rows, info = tandem_import.parse_csv("ts,units,kind\n2026-09-10T12:00:00,5.5,meal\n2026-09-10T14:00:00,1.2,auto\n")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["kind"], "auto")

    def test_tandem_like(self):
        txt = ("Report for user\n\nEventDateTime,Description,InsulinDelivered\n"
               "9/10/2026 12:00:00,Food Bolus,6.20\n9/10/2026 13:05:00,Control-IQ Automatic Bolus,0.85\n")
        rows, info = tandem_import.parse_csv(txt)
        self.assertEqual(info["parsed"], 2)
        self.assertEqual(rows[0]["kind"], "meal")
        self.assertEqual(rows[1]["kind"], "auto")

    def test_dedup(self):
        db = fresh_db()
        txt = "ts,units,kind\n2026-09-10T12:00:00,5.5,meal\n"
        self.assertEqual(tandem_import.import_csv(db, txt)["imported"], 1)
        self.assertEqual(tandem_import.import_csv(db, txt)["imported"], 0)


class DemoTests(unittest.TestCase):
    def test_seed_and_evaluate(self):
        db = fresh_db(); s = db.get_settings()
        self.assertTrue(seed_demo(db, s))
        self.assertFalse(seed_demo(db, s))
        done = outcomes.evaluate_pending(db, s)
        self.assertEqual(len(done), 4)


if __name__ == "__main__":
    unittest.main()
