#!/usr/bin/env python3
"""字段回填结果端到端业务流验证（服务层，独立于被测代码目录）。

运行：python3 /workspace/tools/verify_backfill.py
每个场景开始前重置内存仓库，避免场景间状态污染。
"""
from __future__ import annotations

import sys
import threading

sys.path.insert(0, "/workspace/backend")

from app.services.backfill import BackfillService  # noqa: E402
from app.services.sink import SinkService  # noqa: E402
from app.store import store  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


def find_bfr(batch: str, field: str) -> dict:
    for row in store.rows("backfill"):
        if row["回填批次"] == batch and row["字段名称"] == field:
            return row
    raise AssertionError(f"找不到 {batch}/{field}")


def sink4() -> dict:
    return next(r for r in store.rows("sink") if r["配置编号"] == "SINK-0004")


def sink5() -> dict:
    return next(r for r in store.rows("sink") if r["配置编号"] == "SINK-0005")


def schema6() -> dict:
    return next(r for r in store.rows("schema") if r["定义编号"] == "SCHE-0006")


# ---------------------------------------------------------------- 场景一：验收样例
print("场景一：schema_audit_v1 验收样例（record-21a/21b/21c）")
store.reset()
svc = BackfillService()
v1_items = [
    {"定义编号": "SCHE-0004", "配置编号": "SINK-0004", "字段名称": "record-21a"},
    {"定义编号": "SCHE-0005", "配置编号": "SINK-0004", "字段名称": "record-21b"},
    {"定义编号": "SCHE-0006", "配置编号": "SINK-0004", "字段名称": "record-21c"},
]
summary, msg = svc.run_backfill({"回填批次": "schema_audit_v1", "升级场景": "旧数据库", "items": v1_items})
check("回填返回成功受理", summary is not None, msg)
check("成功 2 条、失败 1 条", summary and summary["成功数"] == 2 and summary["失败数"] == 1)
for field, orig_type, default in [("record-21a", "varchar", ""), ("record-21b", "bigint", "0"),
                                  ("record-21c", "string", "")]:
    row = find_bfr("schema_audit_v1", field)
    check(f"{field} 保留原存储类型 {orig_type}", row["原存储类型"] == orig_type and row["存储类型"] == orig_type)
    check(f"{field} 保留默认值 {default!r}", row["原默认值"] == default and row["默认值"] == default)
    check(f"{field} 保留目标版本 v2", row["目标版本"] == "v2")
check("record-21a 回填成功且仍对应 sink-archive 旧契约",
      find_bfr("schema_audit_v1", "record-21a")["status"] == "回填成功"
      and find_bfr("schema_audit_v1", "record-21a")["契约编号"] == "sink-archive")
check("record-21b 回填成功", find_bfr("schema_audit_v1", "record-21b")["status"] == "回填成功")
failed_21c = find_bfr("schema_audit_v1", "record-21c")
check("record-21c 回填失败并保留原映射",
      failed_21c["status"] == "回填失败" and failed_21c["断点续做"] is True
      and failed_21c["存储类型"] == "string" and failed_21c["格式版本"] == "v1")
check("失败原因可读", "兼容" in failed_21c["失败原因"])
check("存在失败列时 sink-archive 契约保持 v1", sink4()["生效版本"] == "v1" and sink4()["契约版本"] == "v1")
mapping_21c = next(m for m in sink4()["字段映射"] if m["字段名称"] == "record-21c")
check("失败列字段映射维持旧 v1", mapping_21c["格式版本"] == "v1" and mapping_21c["目标类型"] is None)
check("失败记录编号沿用 BFR-0003", failed_21c["记录编号"] == "BFR-0003")

# ---------------------------------------------------------------- 场景二：断点续做 + 契约联动
print("场景二：失败批次断点续做，成功后回填结果与契约一起更新")
summary, msg = svc.resume_batch({
    "回填批次": "schema_audit_v1",
    "items": [{"定义编号": "SCHE-0006", "目标类型": "text"}],
})
check("断点续做受理", summary is not None, msg)
check("续做后失败数归零", summary and summary["失败数"] == 0)
row_21c = find_bfr("schema_audit_v1", "record-21c")
check("21c 仍用原记录编号（无重复登记）", row_21c["记录编号"] == "BFR-0003")
check("21c 回填成功且原存储类型仍保留", row_21c["status"] == "回填成功" and row_21c["原存储类型"] == "string")
check("21c 格式版本随契约升到 v2", row_21c["格式版本"] == "v2")
check("sink-archive 契约一起切到 v2", sink4()["生效版本"] == "v2" and sink4()["上一契约版本"] == "v1")
check("21a/21b 同批次成功记录一起升到 v2",
      all(find_bfr("schema_audit_v1", f)["格式版本"] == "v2" for f in ("record-21a", "record-21b")))
m_21c = next(m for m in sink4()["字段映射"] if m["字段名称"] == "record-21c")
check("字段映射随契约更新到 v2", m_21c["格式版本"] == "v2" and m_21c["目标类型"] == "text")
total_bfr = len(store.rows("backfill"))
summary2, _ = svc.run_backfill({"回填批次": "schema_audit_v1", "升级场景": "旧数据库", "items": v1_items})
check("整批重跑幂等：记录总数不增加", len(store.rows("backfill")) == total_bfr)

# ---------------------------------------------------------------- 场景三：并发去重
print("场景三：同一字段并发升级只提交一个版本")
store.reset()
svc = BackfillService()
item = [{"定义编号": "SCHE-0007", "配置编号": "SINK-0005", "字段名称": "record-22a"}]
errors = []


def worker() -> None:
    try:
        svc.run_backfill({"回填批次": "concurrent_t", "升级场景": "新环境", "目标版本": "v2", "items": item})
    except Exception as exc:  # pragma: no cover
        errors.append(exc)


threads = [threading.Thread(target=worker) for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()
same_key = [r for r in store.rows("backfill")
            if (r["定义编号"], r["字段名称"], r["配置编号"], r["目标版本"])
            == ("SCHE-0007", "record-22a", "SINK-0005", "v2")]
check("8 个并发请求只留一份记录", len(same_key) == 1 and not errors, f"实际 {len(same_key)} 条")
check("并发去重记录编号唯一", len({r["记录编号"] for r in same_key}) == 1)

# ---------------------------------------------------------------- 场景四：批次存储类型混合冲突
print("场景四：旧归档列与新目标类型混写同一批次被拦截")
store.reset()
svc = BackfillService()
mixed_items = [
    {"定义编号": "SCHE-0007", "配置编号": "SINK-0005", "字段名称": "record-22a",
     "目标版本": "v3", "沿用归档列": True},
    {"定义编号": "SCHE-0008", "配置编号": "SINK-0005", "字段名称": "record-22b",
     "目标版本": "v3"},
]
summary, msg = svc.run_backfill({"回填批次": "mixed_t", "升级场景": "旧数据库",
                                 "items": mixed_items})
check("混合批次受理但标记冲突", summary is not None and summary["冲突数"] == 1)
check("新类型侧被标记冲突失败", find_bfr("mixed_t", "record-22b")["冲突标记"] is True
      and find_bfr("mixed_t", "record-22b")["status"] == "回填失败")
check("冲突批次契约不切换", sink5()["生效版本"] == "v2")
check("冲突失败列保留旧映射", find_bfr("mixed_t", "record-22b")["存储类型"] == "bigint"
      and find_bfr("mixed_t", "record-22b")["格式版本"] == "v1")
check("沿用归档列的记录不写新目标类型",
      find_bfr("mixed_t", "record-22a").get("目标类型") is None)

# ---------------------------------------------------------------- 场景五：新环境整体升级
print("场景五：schema_audit_v2 新环境整体升级，契约一次切换")
store.reset()
svc = BackfillService()
v2_items = [
    {"定义编号": "SCHE-0007", "配置编号": "SINK-0005", "字段名称": "record-22a"},
    {"定义编号": "SCHE-0008", "配置编号": "SINK-0005", "字段名称": "record-22b"},
    {"定义编号": "SCHE-0009", "配置编号": "SINK-0005", "字段名称": "record-22c"},
]
summary, msg = svc.run_backfill({"回填批次": "schema_audit_v2", "升级场景": "新环境", "items": v2_items})
check("v2 批次全部成功", summary is not None and summary["失败数"] == 0 and summary["成功数"] == 3)
check("sink-lake 契约保持/切换到 v2", sink5()["生效版本"] == "v2")
for field, t, d in [("record-22a", "varchar", ""), ("record-22b", "bigint", "0"),
                    ("record-22c", "decimal", "0.00")]:
    row = find_bfr("schema_audit_v2", field)
    check(f"{field} 保留原存储类型/默认值/目标版本",
          row["原存储类型"] == t and row["原默认值"] == d and row["目标版本"] == "v2")

# ---------------------------------------------------------------- 场景六：部署回滚
print("场景六：部署回滚恢复上一契约版本")
store.reset()
svc = BackfillService()
svc.run_backfill({"回填批次": "schema_audit_v2", "升级场景": "新环境", "items": v2_items})
check("回滚前 sink-lake 在 v2", sink5()["生效版本"] == "v2")
summary, msg = svc.rollback_contract({"配置编号": "SINK-0005"})
check("回滚受理", summary is not None, msg)
check("契约恢复到上一版本 v1", sink5()["生效版本"] == "v1" and sink5()["契约版本"] == "v1")
rollback_rows = [r for r in store.rows("backfill") if r["回填批次"] == "rollback_SINK-0005"]
check("回滚回填记录标记已回滚", rollback_rows and all(r["status"] == "已回滚" for r in rollback_rows))
check("回滚记录保留原存储类型与目标版本",
      all(r["原存储类型"] and r["目标版本"] == "v1" for r in rollback_rows))
bad, msg = svc.rollback_contract({"配置编号": "SINK-0004"})
check("无上一版本的契约拒绝回滚", bad is None)

# ---------------------------------------------------------------- 场景七：依赖兼容与存量核对
print("场景七：依赖兼容口径与 SCHE 存量核对")
store.reset()
svc = BackfillService()
bad, msg = svc.run_backfill({"回填批次": "dep_t", "升级场景": "依赖兼容", "目标版本": "v9",
                             "items": [{"定义编号": "SCHE-0004", "配置编号": "SINK-0004",
                                        "字段名称": "record-21a"}]})
check("不支持的目标版本被依赖兼容拦下", bad is not None and bad["失败数"] == 1)
check("拦截原因写明依赖兼容", "依赖兼容" in find_bfr("dep_t", "record-21a")["失败原因"])
bad, msg = svc.run_backfill({"回填批次": "mismatch_t", "升级场景": "旧数据库",
                             "items": [{"定义编号": "SCHE-0004", "配置编号": "SINK-0004",
                                        "字段名称": "record-21a", "存储类型": "bigint"}]})
check("存储类型核对不一致整批拒绝", bad is None and "存储类型" in msg)
bad, msg = svc.run_backfill({"回填批次": "missing_t", "升级场景": "旧数据库",
                             "items": [{"定义编号": "SCHE-0004", "配置编号": "SINK-0004",
                                        "字段名称": "not-exist"}]})
check("字段不存在整批拒绝", bad is None and "找不到" in msg)
bad, msg = svc.run_backfill({"回填批次": "scene_t", "升级场景": "未知场景",
                             "items": [{"定义编号": "SCHE-0004", "配置编号": "SINK-0004",
                                        "字段名称": "record-21a"}]})
check("非法升级场景拒绝", bad is None and "升级场景" in msg)

# ---------------------------------------------------------------- 场景八：消费侧共享结果
print("场景八：输出端消费侧按记录编号关联同一份回填结果")
store.reset()
svc = BackfillService()
svc.run_backfill({"回填批次": "schema_audit_v1", "升级场景": "旧数据库", "items": v1_items})
sink_svc = SinkService()
shared = sink_svc.list_backfills(4)
check("sink 入口读到同批次 3 条共享结果", len(shared) == 3)
check("共享结果即 backfill 表同一对象", shared[0] in store.rows("backfill"))
by_no = sink_svc.get_backfill_by_record_no("BFR-0003")
check("按记录编号关联到失败列", by_no is not None and by_no["字段名称"] == "record-21c")
check("SINK-0003 没有回填结果时返回空", sink_svc.list_backfills(3) == [])
check("重复登记清理后自然键唯一",
      len({tuple(r.get(k) for k in ("定义编号", "字段名称", "配置编号", "目标版本"))
           for r in store.rows("backfill")}) == len(store.rows("backfill")))

print(f"\n结果：通过 {PASS} 项，失败 {FAIL} 项")
sys.exit(1 if FAIL else 0)
