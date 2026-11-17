"""门店促销实验：分组计划、实际暴露、订单退货与效应分析。

分析口径是显式的：
- 计划分组来自可修订的入组计划，并按记录时间支持 as-of 重放；
- 实际暴露只由分析窗口内的触达/入组事件决定；
- 销售额区分订单金额与退货金额，净额 = 订单金额 - 退货金额；
- 只在"实际暴露"的干净门店之间估计效应，重叠活动无法归因时保留不确定范围。
"""
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from .decisions import encode
from .store import timestamp
import hashlib

PLAN_ARMS = frozenset({"trial", "control", "none"})

# 双侧 95% t 临界值；df 超过表内范围时按锚点线性插值。
_T95 = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
    6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
    11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131,
    16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
    21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
    26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
}
_ANCHORS = sorted(_T95.items()) + [(60, 2.000), (120, 1.980), (10 ** 9, 1.960)]


def t_critical(df):
    """返回给定自由度下的双侧 95% t 临界值。"""
    df = max(1.0, float(df))
    for (low_df, low_t), (high_df, high_t) in zip(_ANCHORS, _ANCHORS[1:]):
        if df <= high_df:
            if high_df == low_df:
                return high_t
            return low_t + (high_t - low_t) * (df - low_df) / (high_df - low_df)
    return 1.960


def text(value):
    """把 Decimal 输出为最多四位小数的字符串。"""
    if value is None:
        return None
    value = value.quantize(Decimal("0.0001"))
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _instant(value):
    return datetime.fromisoformat(value)


def _name(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("实验名称不能为空")
    return value


def _amount(payload):
    try:
        value = Decimal(str(payload.get("amount", 0)))
    except InvalidOperation as exc:
        raise ValueError("金额字段无效") from exc
    if not value.is_finite():
        raise ValueError("金额字段无效")
    return value


def _share(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal, str)):
        raise ValueError("归因比例必须为数值")
    try:
        share = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("归因比例必须为数值") from exc
    if not share.is_finite() or share < 0 or share > 1:
        raise ValueError("归因比例必须在 0 与 1 之间")
    return share


class ExperimentPlan:
    """保存可修订的门店入组计划，修订按记录时间生效。"""

    def __init__(self, connection):
        self.connection = connection
        connection.execute("""CREATE TABLE IF NOT EXISTS plan_revisions (
            experiment TEXT NOT NULL, revision INTEGER NOT NULL, recorded_at TEXT NOT NULL,
            PRIMARY KEY(experiment, revision))""")
        connection.execute("""CREATE TABLE IF NOT EXISTS plan_stores (
            experiment TEXT NOT NULL, revision INTEGER NOT NULL, store TEXT NOT NULL,
            arm TEXT NOT NULL, PRIMARY KEY(experiment, revision, store))""")

    def record(self, experiment, revision, recorded_at, stores):
        """登记一次计划或纠正；同编号同内容幂等，冲突内容或回填时间会被拒绝。"""
        name = _name(experiment)
        if type(revision) is not int or revision < 1:
            raise ValueError("计划修订号必须为正整数")
        recorded = timestamp(recorded_at)
        if not isinstance(stores, dict) or not stores:
            raise ValueError("计划门店不能为空")
        cleaned = {}
        for store, arm in stores.items():
            if not isinstance(store, str) or not store.strip():
                raise ValueError("计划门店不能为空")
            if arm not in PLAN_ARMS:
                raise ValueError("未知实验分组")
            cleaned[store] = arm
        existing = self.connection.execute(
            "SELECT store, arm FROM plan_stores WHERE experiment=? AND revision=?",
            (name, revision)).fetchall()
        if existing:
            if {row["store"]: row["arm"] for row in existing} == cleaned:
                return False
            raise ValueError("同实验同修订的计划内容冲突")
        latest = self.connection.execute(
            "SELECT revision, recorded_at FROM plan_revisions WHERE experiment=? "
            "ORDER BY revision DESC LIMIT 1", (name,)).fetchone()
        if latest is not None:
            if revision <= latest["revision"]:
                raise ValueError("计划修订号必须递增")
            if _instant(recorded) < _instant(latest["recorded_at"]):
                raise ValueError("计划记录时间不能早于现有修订")
        self.connection.execute("INSERT INTO plan_revisions VALUES(?,?,?)",
                                (name, revision, recorded))
        self.connection.executemany("INSERT INTO plan_stores VALUES(?,?,?,?)",
                                    [(name, revision, store, arm) for store, arm in cleaned.items()])
        return True

    def experiments(self):
        return [row[0] for row in self.connection.execute(
            "SELECT DISTINCT experiment FROM plan_revisions ORDER BY experiment")]

    def revisions(self, experiment):
        rows = self.connection.execute(
            "SELECT revision, recorded_at FROM plan_revisions WHERE experiment=? ORDER BY revision",
            (_name(experiment),)).fetchall()
        return [{"revision": row["revision"], "recorded_at": row["recorded_at"],
                 "stores": self._stores(experiment, row["revision"])} for row in rows]

    def effective(self, experiment, as_of=None):
        """返回 as_of 时点已知的最新计划修订及其分组。"""
        name = _name(experiment)
        if as_of is None:
            row = self.connection.execute(
                "SELECT revision, recorded_at FROM plan_revisions WHERE experiment=? "
                "ORDER BY revision DESC LIMIT 1", (name,)).fetchone()
        else:
            row = self.connection.execute(
                "SELECT revision, recorded_at FROM plan_revisions WHERE experiment=? AND recorded_at<=? "
                "ORDER BY revision DESC LIMIT 1", (name, timestamp(as_of))).fetchone()
        if row is None:
            return {"experiment": name, "revision": None, "recorded_at": None, "stores": {}}
        return {"experiment": name, "revision": row["revision"], "recorded_at": row["recorded_at"],
                "stores": self._stores(name, row["revision"])}

    def _stores(self, experiment, revision):
        rows = self.connection.execute(
            "SELECT store, arm FROM plan_stores WHERE experiment=? AND revision=? ORDER BY store",
            (experiment, revision)).fetchall()
        return {row["store"]: row["arm"] for row in rows}


class ExperimentAnalysis:
    """把计划、入组/触达事件与订单退货连成可复核的分析结果。"""

    def __init__(self, store, entities, plan):
        self.store, self.entities, self.plan = store, entities, plan

    def analyze(self, experiment, start, end, as_of=None, attribution=None):
        name = _name(experiment)
        start_at, end_at = timestamp(start), timestamp(end)
        if _instant(start_at) > _instant(end_at):
            raise ValueError("分析窗口起点晚于终点")
        as_of_at = None if as_of is None else timestamp(as_of)
        share = _share(attribution)
        plan = self.plan.effective(name, as_of_at)
        window_start, window_end = _instant(start_at), _instant(end_at)

        activity = {}
        unmatched = defaultdict(int)

        def entry(store):
            return activity.setdefault(store, {
                "touched": False, "assigned": False, "orders": 0,
                "order_amount": Decimal(0), "returns": 0, "return_amount": Decimal(0)})

        for event in self.store.current(as_of_at):
            occurred = _instant(event["occurred_at"])
            if occurred < window_start or occurred > window_end:
                continue
            subject = self.entities.resolve(event["source"], event["entity"])
            if subject is None:
                unmatched[event["kind"]] += 1
                continue
            kind = event["kind"]
            row = entry(subject)
            if kind == "touch":
                row["touched"] = True
            elif kind == "assignment":
                row["assigned"] = True
            elif kind == "order":
                amount = _amount(event["payload"])
                if amount < 0:
                    raise ValueError("订单金额必须为非负")
                row["orders"] += 1
                row["order_amount"] += amount
            elif kind == "return":
                refund = _amount(event["payload"])
                if refund < 0:
                    raise ValueError("退货金额必须为非负")
                row["returns"] += 1
                row["return_amount"] += refund

        stores = []
        for store in sorted(set(activity) | set(plan["stores"])):
            row = entry(store)
            planned = plan["stores"].get(store)
            classification = self._classify(planned, row["touched"], row["assigned"])
            gross = row["order_amount"]
            net = gross - row["return_amount"]
            stores.append({
                "store": store,
                "planned": planned,
                "touched": row["touched"],
                "assigned": row["assigned"],
                "class": classification,
                "orders": row["orders"],
                "returns": row["returns"],
                "gross": text(gross),
                "return_amount": text(row["return_amount"]),
                "net": text(net),
            })

        groups = defaultdict(list)
        for row in stores:
            if row["class"]:
                groups[row["class"]].append(row["store"])
        cross_tab = {key: sorted(groups[key]) for key in sorted(groups)}
        counts = {key: len(value) for key, value in cross_tab.items()}

        planned_trial = [row for row in stores if row["planned"] == "trial"]
        planned_control = [row for row in stores if row["planned"] == "control"]
        clean_trial = [row for row in stores if row["class"] == "trial"]
        clean_control = [row for row in stores if row["class"] == "control"]

        observed = self._difference(planned_trial, planned_control, "gross")
        after_returns = self._difference(planned_trial, planned_control, "net")
        adjusted = self._difference(clean_trial, clean_control, "net")

        compared = {store for store, arm in plan["stores"].items() if arm in {"trial", "control"}}
        overlaps = self._overlaps(name, compared, as_of_at)
        notes = []
        if plan["revision"] is None:
            notes.append("该实验未登记入组计划，仅按实际暴露归类")
        if not clean_trial or not clean_control:
            notes.append("干净试验组或对照组为空，效应不可估计")
        if counts.get("contaminated"):
            notes.append("存在计划对照组但已触达/入组的门店，已从干净对照中剔除")
        if counts.get("unreached_trial"):
            notes.append("存在计划试验组但窗口内未触达的门店，已从实际暴露中剔除")
        if overlaps:
            notes.append("存在重叠实验门店：%s" % ", ".join(item["experiment"] for item in overlaps))
        interval, interval_note = self._interval(adjusted, share, overlaps)
        if interval_note:
            notes.append(interval_note)

        effect = {
            "observed": observed,
            "after_returns": after_returns,
            "adjusted": dict(adjusted, interval=interval),
            "reconciliation": self._reconciliation(observed, after_returns, adjusted),
            "attribution": {"share": text(share) if share is not None else None, "overlaps": overlaps},
            "notes": notes,
        }
        return {
            "experiment": name,
            "window": {"start": start_at, "end": end_at, "as_of": as_of_at},
            "plan": plan,
            "stores": stores,
            "cross_tab": cross_tab,
            "counts": counts,
            "unmatched": dict(sorted(unmatched.items())),
            "effect": effect,
        }

    @staticmethod
    def _classify(planned, touched, assigned):
        if planned == "none":
            return "withdrawn"
        if planned == "trial":
            return "trial" if touched else "unreached_trial"
        if planned == "control":
            return "contaminated" if (touched or assigned) else "control"
        return "unplanned" if (touched or assigned) else None

    @staticmethod
    def _summary(rows, field):
        values = [Decimal(row[field]) for row in rows]
        total = sum(values, Decimal(0))
        if not values:
            return {"stores": 0, "total": text(total), "mean": None, "sd": None}
        mean = total / len(values)
        if len(values) < 2:
            return {"stores": len(values), "total": text(total), "mean": text(mean), "sd": None}
        variance = sum(((value - mean) ** 2 for value in values), Decimal(0)) / (len(values) - 1)
        return {"stores": len(values), "total": text(total), "mean": text(mean),
                "sd": text(variance.sqrt())}

    def _difference(self, trial_rows, control_rows, field):
        trial = self._summary(trial_rows, field)
        control = self._summary(control_rows, field)
        difference = None
        if trial["mean"] is not None and control["mean"] is not None:
            difference = Decimal(trial["mean"]) - Decimal(control["mean"])
        return {"trial": trial, "control": control, "difference": text(difference),
                "interval": None}

    @staticmethod
    def _reconciliation(observed, after_returns, adjusted):
        naive, net, clean = (Decimal(value["difference"]) if value["difference"] is not None else None
                             for value in (observed, after_returns, adjusted))
        if None in (naive, net, clean):
            return {"naive": text(naive), "after_returns": text(net), "adjusted": text(clean),
                    "return_effect": None, "exposure_effect": None, "explained_change": None}
        return {
            "naive": text(naive),
            "after_returns": text(net),
            "adjusted": text(clean),
            "return_effect": text(net - naive),
            "exposure_effect": text(clean - net),
            "explained_change": text(clean - naive),
        }

    @staticmethod
    def _interval(adjusted, share, overlaps):
        """统计区间之外，再并入重叠活动的归因不确定范围。

        可识别边界始终是"零效应"与"全部观测差异"之间；调用方给出的归因比例
        只用于收窄该边界内的最坏情形，不会扩张到观测差异之外。
        """
        difference = adjusted["difference"]
        trial, control = adjusted["trial"], adjusted["control"]
        if difference is None or trial["sd"] is None or control["sd"] is None:
            return None, "分店数量不足，无法估计效应区间"
        diff = Decimal(difference)
        variance = (Decimal(trial["sd"]) ** 2 / trial["stores"]
                    + Decimal(control["sd"]) ** 2 / control["stores"])
        if variance <= 0:
            low = high = diff
        else:
            se = variance.sqrt()
            term_trial = (Decimal(trial["sd"]) ** 2 / trial["stores"]) ** 2 / (trial["stores"] - 1)
            term_control = (Decimal(control["sd"]) ** 2 / control["stores"]) ** 2 / (control["stores"] - 1)
            denominator = term_trial + term_control
            df = float(variance ** 2 / denominator) if denominator > 0 else 1.0
            half = Decimal(str(t_critical(df))) * se
            low, high = diff - half, diff + half

        note = None
        if not overlaps:
            if share is not None:
                note = "无重叠活动，归因比例不影响区间"
            return [text(low), text(high)], note

        boundary_low, boundary_high = min(Decimal(0), diff), max(Decimal(0), diff)
        if share is None:
            low, high = min(low, boundary_low), max(high, boundary_high)
            note = "存在重叠活动且未提供归因比例，区间覆盖零效应到全部观测差异"
        elif share >= 1:
            note = "调用方声明重叠活动完全归因，区间仅反映抽样误差"
        else:
            remainder = (1 - share) * abs(diff)
            low = min(low, max(boundary_low, diff * share - remainder))
            high = max(high, min(boundary_high, diff * share + remainder))
            note = "存在重叠活动，已按归因比例 %s 保留其余部分的归因不确定范围" % text(share)
        return [text(low), text(high)], note

    def _overlaps(self, experiment, stores, as_of=None):
        overlaps = []
        for other in self.plan.experiments():
            if other == experiment:
                continue
            other_plan = self.plan.effective(other, as_of)
            shared = sorted(store for store, arm in other_plan["stores"].items()
                            if store in stores and arm in {"trial", "control"})
            if shared:
                overlaps.append({"experiment": other, "stores": shared})
        return overlaps

    def reconcile(self, decisions, report_id):
        """对比已发布快照与按当前数据重算的结果，历史正文保持不变。

        历史快照继续使用发布时的 as_of，因此不会看起来使用了后来才出现的数据；
        当前分析则使用全部已观察数据与最新计划修订，差异逐项列出。
        """
        report = decisions.get(report_id)
        if report is None:
            raise ValueError("报告不存在")
        body = report["body"]
        if body.get("kind") != "experiment-analysis":
            raise ValueError("报告不是实验分析")
        archived = body["analysis"]
        share = archived["effect"]["attribution"]["share"]
        current = self.analyze(archived["experiment"], archived["window"]["start"],
                               archived["window"]["end"], as_of=None, attribution=share)
        changes = list(self._diff(archived, current))
        # 快照的 as_of 与当前全量重算的 as_of 本就不同，不作为数据变化报告。
        changes = [change for change in changes if change["path"] != "window.as_of"]
        return {
            "report_id": report_id,
            "archived_as_of": archived["window"]["as_of"],
            "archived_digest": report["digest"],
            "current_digest": hashlib.sha256(encode(current).encode("utf-8")).hexdigest(),
            "changed": archived != current,
            "changes": changes,
            "current": current,
        }

    @classmethod
    def _diff(cls, old, new, path=""):
        if isinstance(old, dict) and isinstance(new, dict):
            for key in sorted(set(old) | set(new)):
                child = "%s.%s" % (path, key) if path else key
                yield from cls._diff(old.get(key), new.get(key), child)
            return
        if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
            for index, (left, right) in enumerate(zip(old, new)):
                yield from cls._diff(left, right, "%s[%d]" % (path, index))
            return
        if old != new:
            yield {"path": path, "archived": old, "current": new}
