# 零售促销实验数据服务

服务登记多来源事件、业务主体及现有摘要，并保存发布报告的原始内容。示例数据为虚构记录。

需要 Python 3.10 或更新版本；仅依赖标准库。

运行测试：python -m unittest discover -s tests -v
编译检查：python -m compileall -q analytics tests
本地演练：python -m analytics --database demo.sqlite3 --fixture fixtures/sample.json

analytics/store.py 保存来源事件和修订；identity.py 管理外部身份映射；metrics.py 汇总已映射主体的事件计数与通用金额；decisions.py 保存已发布报告；domain.py 保存现行的简单入组表；service.py 协调这些现有接口。原始摘要只表示观察，不提供行业专属分析结论。

## 实验分析

analytics/experiment.py 在原始事件之上提供门店促销实验分析：

- `plan(experiment, revision, recorded_at, stores)` 登记或纠正计划分组，修订按记录时间支持 as-of 重放；
- `analyze(experiment, start, end, as_of=None, attribution=None)` 把计划分组、实际触达、订单和退货连成门店级明细、交叉表与三层效应；
- `publish_analysis(...)` 将当时的分析结果与分析口径一并留档；
- `reconcile(report_id)` 按当前数据与最新计划重算，并与已发布快照逐项对比。

详细口径（实际暴露、销售净额、重叠活动的不确定范围和留档语义）见 docs/domain.md。
