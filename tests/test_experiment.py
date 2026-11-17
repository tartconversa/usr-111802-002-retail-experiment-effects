"""核验门店促销实验的分组、暴露、退货与留档口径。"""
import tempfile
import unittest
from pathlib import Path
from analytics import AnalyticsService

WINDOW = ("2026-01-09T00:00:00+08:00", "2026-01-15T00:00:00+08:00")
TOUCHED = "2026-01-10T10:00:00+08:00"
ORDERED = "2026-01-11T10:00:00+08:00"
RETURNED = "2026-01-12T10:00:00+08:00"
LATE = "2026-02-01T11:00:00+08:00"


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = AnalyticsService(Path(self.tmp.name) / "data.sqlite3")
        self.stores = [f"store-{index}" for index in range(1, 7)]
        for store in self.stores:
            self.service.register(store, store, "region")
            self.service.link("pos", f"ext-{store}", store)
        self.default = ({store: "trial" for store in self.stores[:3]}
                        | {store: "control" for store in self.stores[3:]})
        self.assertTrue(self.service.plan("promo", 1, "2026-01-01T00:00:00+08:00", self.default))

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def plan(self, stores, revision=2, experiment="promo", recorded_at="2026-02-01T00:00:00+08:00"):
        return self.service.plan(experiment, revision, recorded_at, stores)

    def event(self, event_id, kind, store, occurred_at, amount=0, observed_at=None, revision=1):
        self.service.ingest({
            "source": "pos", "event_id": event_id, "revision": revision, "kind": kind,
            "entity": f"ext-{store}", "occurred_at": occurred_at,
            "observed_at": observed_at or "2026-01-15T00:00:00+08:00", "payload": {"amount": amount}})

    def touch(self, store, event_id=None):
        self.event(event_id or f"touch-{store}", "touch", store, TOUCHED)

    def test_naive_sales_are_corrected_for_returns_and_contamination(self):
        for store in self.stores[:3]:
            self.touch(store)
            self.event(f"order-{store}", "order", store, ORDERED, 100)
            self.event(f"return-{store}", "return", store, RETURNED, 10)
        # 计划对照门店也被触达且销量偏高，必须从干净对照中剔除。
        self.touch(self.stores[3])
        self.event(f"order-{self.stores[3]}", "order", self.stores[3], ORDERED, 300)
        for store in self.stores[4:]:
            self.event(f"order-{store}", "order", store, ORDERED, 90)

        effect = self.service.analyze("promo", *WINDOW)["effect"]
        # 未扣退货、混入被污染对照：100 - 160 = -60
        self.assertEqual(effect["observed"]["difference"], "-60")
        # 扣退货后：90 - 160 = -70
        self.assertEqual(effect["after_returns"]["difference"], "-70")
        # 只看干净门店：90 - 90 = 0
        self.assertEqual(effect["adjusted"]["difference"], "0")
        self.assertEqual(effect["adjusted"]["interval"], ["0", "0"])
        self.assertEqual(effect["reconciliation"]["return_effect"], "-10")
        self.assertEqual(effect["reconciliation"]["exposure_effect"], "70")
        self.assertEqual(effect["reconciliation"]["explained_change"], "60")
        self.assertEqual(effect["adjusted"]["trial"],
                         {"stores": 3, "total": "270", "mean": "90", "sd": "0"})

    def test_contract_and_exposure_are_reported_separately(self):
        for store in self.stores[:3]:
            self.touch(store)
            self.event(f"order-{store}", "order", store, ORDERED, 100)
        for store in self.stores[3:]:
            self.event(f"order-{store}", "order", store, ORDERED, 90)

        result = self.service.analyze("promo", *WINDOW)
        self.assertEqual(result["counts"], {"control": 3, "trial": 3})
        self.assertEqual(result["plan"]["revision"], 1)
        self.assertEqual(result["effect"]["observed"]["difference"], "10")
        self.assertEqual(result["effect"]["adjusted"]["difference"], "10")

    def test_as_of_excludes_later_observations(self):
        for store in self.stores[:3]:
            self.touch(store)
            self.event(f"order-{store}", "order", store, ORDERED, 100)
        for store in self.stores[3:]:
            self.event(f"order-{store}", "order", store, ORDERED, 90,
                       observed_at="2026-02-05T00:00:00+08:00")

        published = self.service.analyze("promo", *WINDOW, as_of="2026-01-31T00:00:00+08:00")
        control = [row for row in published["stores"] if row["store"] == self.stores[3]][0]
        self.assertEqual(control["class"], "control")
        self.assertEqual(control["orders"], 0)
        # 对照组尚无已观察订单：100 - 0 = 100
        self.assertEqual(published["effect"]["adjusted"]["difference"], "100")

        now = self.service.analyze("promo", *WINDOW)
        self.assertEqual(now["effect"]["adjusted"]["difference"], "10")

    def test_plan_revisions_are_time_aware(self):
        corrected = ({store: "trial" for store in self.stores[:4]}
                     | {store: "control" for store in self.stores[4:]})
        self.assertTrue(self.plan(corrected, revision=2, recorded_at="2026-02-01T00:00:00+08:00"))

        early = self.service.experiments.effective("promo", "2026-01-31T00:00:00+08:00")
        latest = self.service.experiments.effective("promo")
        self.assertEqual(early["revision"], 1)
        self.assertEqual(early["stores"][self.stores[3]], "control")
        self.assertEqual(latest["revision"], 2)
        self.assertEqual(latest["stores"][self.stores[3]], "trial")
        # as-of 分析使用当时已知的计划。
        self.assertEqual(self.service.analyze("promo", *WINDOW, as_of="2026-01-31T00:00:00+08:00")
                         ["plan"]["revision"], 1)
        self.assertEqual(self.service.analyze("promo", *WINDOW)["plan"]["revision"], 2)

    def test_plan_rejects_conflicting_content_and_backdated_revisions(self):
        self.assertFalse(self.service.plan("promo", 1, "2026-01-01T00:00:00+08:00", self.default))
        with self.assertRaises(ValueError):
            self.plan({store: "trial" for store in self.stores}, revision=1)
        with self.assertRaises(ValueError):
            self.plan({store: "trial" for store in self.stores},
                      recorded_at="2025-12-01T00:00:00+08:00")
        with self.assertRaises(ValueError):
            self.service.plan("promo", 2, "2026-02-01T00:00:00+08:00", {"store-1": "holdout"})
        with self.assertRaises(ValueError):
            self.service.plan("promo", 2, "2026-02-01T00:00:00+08:00", {})

    def test_decision_snapshot_survives_late_data_and_plan_correction(self):
        for store in self.stores[:3]:
            self.touch(store)
            self.event(f"order-{store}", "order", store, ORDERED, 100)
        for store in self.stores[3:]:
            self.event(f"order-{store}", "order", store, ORDERED, 90)

        report = self.service.publish_analysis(
            "decision-1", "2026-01-20", "扩大投放", "promo", *WINDOW,
            as_of="2026-01-19T00:00:00+08:00", attribution=None)
        archived = report["body"]["analysis"]
        self.assertEqual(archived["effect"]["adjusted"]["difference"], "10")
        self.assertEqual(self.service.reconcile("decision-1")["changes"], [])

        # 晚到的退货：当前分析更新，历史决策正文不变。
        self.event("late-return", "return", self.stores[3], RETURNED, 1000, observed_at=LATE)
        late = self.service.reconcile("decision-1")
        self.assertTrue(late["changed"])
        self.assertTrue(any(change["path"].endswith("return_amount") for change in late["changes"]))
        self.assertEqual(self.service.decisions.get("decision-1")["body"]["analysis"], archived)

        # 分组纠正：当前分析反映新计划，历史决策仍记录当时的计划。
        self.plan(self.default | {self.stores[3]: "none"}, revision=2)
        corrected = self.service.reconcile("decision-1")
        self.assertTrue(corrected["changed"])
        self.assertEqual(corrected["current"]["plan"]["revision"], 2)
        self.assertIn(self.stores[3], corrected["current"]["cross_tab"]["withdrawn"])
        self.assertEqual(self.service.decisions.get("decision-1")["body"]["analysis"]
                         ["plan"]["revision"], 1)

    def test_overlap_widens_interval_until_fully_attributed(self):
        for store in self.stores[:3]:
            self.touch(store)
            self.event(f"order-{store}", "order", store, ORDERED, 100)
        for store in self.stores[3:]:
            self.event(f"order-{store}", "order", store, ORDERED, 80)
        self.plan({"store-1": "trial"}, experiment="loyalty")

        unadjusted = self.service.analyze("promo", *WINDOW)
        self.assertEqual(unadjusted["effect"]["attribution"]["overlaps"],
                         [{"experiment": "loyalty", "stores": ["store-1"]}])
        self.assertEqual(unadjusted["effect"]["adjusted"]["interval"], ["0", "20"])

        attributed = self.service.analyze("promo", *WINDOW, attribution=1)
        self.assertEqual(attributed["effect"]["adjusted"]["interval"], ["20", "20"])

    def test_withdrawn_stores_and_unknown_events(self):
        self.plan(self.default | {self.stores[5]: "none"}, revision=2)
        self.touch(self.stores[0])
        self.event("order-1", "order", self.stores[0], ORDERED, 100)
        self.service.ingest({"source": "pos", "event_id": "orphan", "revision": 1,
                             "kind": "order", "entity": "missing", "occurred_at": ORDERED,
                             "observed_at": "2026-01-15T00:00:00+08:00", "payload": {"amount": 5}})

        result = self.service.analyze("promo", *WINDOW)
        self.assertIn(self.stores[5], result["cross_tab"]["withdrawn"])
        self.assertEqual(result["unmatched"], {"order": 1})
        with self.assertRaises(ValueError):
            self.service.analyze("promo", WINDOW[1], WINDOW[0])

    def test_invalid_amounts_and_attribution(self):
        self.touch(self.stores[0])
        self.event("order-1", "order", self.stores[0], ORDERED, 100)
        with self.assertRaises(ValueError):
            self.service.analyze("promo", *WINDOW, attribution=1.5)
        with self.assertRaises(ValueError):
            self.service.analyze("promo", *WINDOW, attribution="half")
        self.service.ingest({"source": "pos", "event_id": "bad-refund", "revision": 1,
                             "kind": "return", "entity": f"ext-{self.stores[0]}",
                             "occurred_at": RETURNED, "observed_at": "2026-01-15T00:00:00+08:00",
                             "payload": {"amount": -5}})
        with self.assertRaises(ValueError):
            self.service.analyze("promo", *WINDOW)


if __name__ == "__main__":
    unittest.main()
