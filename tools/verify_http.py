#!/usr/bin/env python3
"""字段回填 HTTP 接口链路验证（仅标准库，独立目录）。"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8011"
# 每次运行用唯一批次号，避免内存仓库累积造成跨轮次污染
RUN_TAG = f"run{int(time.time())}"
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


def call(method: str, path: str, values: dict | None = None):
    url = BASE + path
    data = None
    headers = {}
    if values is not None:
        data = json.dumps({"values": values}).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


print("== 0. 重置内存仓库，保证可重复执行 ==")
_, reset_body = call("POST", "/api/backfill/test/reset", {})
check("仓库重置成功", reset_body.get("ok") is True, str(reset_body))

print("== 列表/筛选 ==")
_, body = call("GET", "/api/backfill")
check("回填结果初始 6 条", body["total"] == 6, str(body["total"]))
unique_version = "v3"
_, body = call("GET", "/api/backfill?" + urllib.parse.urlencode({"batch": "schema_audit_v1"}))
check("按批次 schema_audit_v1 过滤 3 条", body["total"] == 3)
_, body = call("GET", "/api/backfill?" + urllib.parse.urlencode({"status": "回填失败"}))
check("失败状态过滤 1 条", body["total"] == 1, str(body["total"]))
_, body = call("GET", "/api/backfill?" + urllib.parse.urlencode({"scene": "依赖兼容"}))
check("按升级场景过滤依赖兼容 1 条", body["total"] == 1)
_, body = call("GET", "/api/backfill?" + urllib.parse.urlencode({"sink_code": "SINK-0005"}))
check("按输出配置过滤 sink-5 三条", body["total"] == 3)

print("== 记录编号关联（两个入口同一份结果） ==")
_, bfr3 = call("GET", "/api/backfill/record/BFR-0003")
check("backfill 入口按记录编号取到 21c", bfr3["字段名称"] == "record-21c")
_, sink_side = call("GET", "/api/sink/backfills/record/BFR-0003")
check("sink 入口按同一记录编号关联", sink_side["记录编号"] == "BFR-0003")
_, shared = call("GET", "/api/sink/4/backfills")
check("sink-4 关联共享回填 3 条", shared["total"] == 3)
_, shared_list = call("GET", "/api/backfill?" + urllib.parse.urlencode({"sink_code": "SINK-0004"}))
check("两个入口读到同一批记录编号",
      sorted(i["记录编号"] for i in shared["items"])
      == sorted(i["记录编号"] for i in shared_list["items"]))

print("== 可核对结果：原存储类型/默认值/目标版本 ==")
check("21c 原存储类型 string", bfr3["原存储类型"] == "string")
check("21c 默认值保留", "原默认值" in bfr3 and bfr3["原默认值"] == "")
check("21c 目标版本 v2", bfr3["目标版本"] == "v2")
check("21c 失败列保留原映射", bfr3["存储类型"] == "string" and bfr3["格式版本"] == "v1" and bfr3["断点续做"] is True)

print("== 批量回填：混合存储类型冲突拦截 ==")
mixed_batch = f"mixed_{RUN_TAG}"
status, body = call("POST", "/api/backfill/batches", {
    "回填批次": mixed_batch, "升级场景": "旧数据库", "items": [
        {"定义编号": "SCHE-0007", "配置编号": "SINK-0005", "字段名称": "record-22a",
         "目标版本": unique_version, "沿用归档列": True},
        {"定义编号": "SCHE-0008", "配置编号": "SINK-0005", "字段名称": "record-22b",
         "目标版本": unique_version},
    ]})
check("混合批次受理 ok=True", body["ok"] is True, str(body))
check("冲突数 1", body["entry"]["冲突数"] == 1)
check("失败数 1", body["entry"]["失败数"] == 1)
_, sink5 = call("GET", "/api/sink/5")
check("冲突后契约仍为 v2", sink5["生效版本"] == "v2")

print("== 断点续做：只重跑失败列，成功后联动契约 ==")
status, body = call("POST", "/api/backfill/batches/resume", {
    "回填批次": mixed_batch,
    "items": [{"定义编号": "SCHE-0008", "沿用归档列": True}]})
check("断点续做受理", body["ok"] is True, str(body))
check("续做后失败归零", body["entry"]["失败数"] == 0)
_, sink5 = call("GET", "/api/sink/5")
check(f"整批成功后契约联动切到 {unique_version}", sink5["生效版本"] == unique_version,
      sink5.get("生效版本"))
_, rows = call("GET", "/api/backfill?" + urllib.parse.urlencode({"batch": mixed_batch}))
check("续做未产生重复记录（同批次 2 条）", rows["total"] == 2, str(rows["total"]))

print("== 非法入参 ==")
_, body = call("POST", "/api/backfill/batches", {"回填批次": "x", "升级场景": "旧数据库"})
check("空 items 被拒绝", body["ok"] is False and "items" in body["message"])
_, body = call("POST", "/api/backfill/batches", {"回填批次": "x", "升级场景": "火星升级", "items": []})
check("非法场景可读报错", "升级场景" in body["message"])
code, _ = call("GET", "/api/backfill/record/NOPE")
check("不存在记录返回 404", code == 404)
_, body = call("POST", "/api/backfill/batches", {
    "回填批次": "bad", "升级场景": "旧数据库",
    "items": [{"定义编号": "SCHE-0004", "配置编号": "SINK-0004",
               "字段名称": "record-21a", "存储类型": "bigint"}]})
check("存储类型核对不一致整批拒绝", body["ok"] is False and "存储类型" in body["message"])

print("== 部署回滚 ==")
_, body = call("POST", "/api/backfill/rollback", {"配置编号": "SINK-0004"})
check("同版本契约拒绝回滚", body["ok"] is False and "回退" in body["message"])
_, before = call("GET", "/api/sink/5")
prev_version = before["上一契约版本"]
check("回滚前存在不同的上一契约版本", prev_version != before["生效版本"],
      f"{prev_version} vs {before['生效版本']}")
_, body = call("POST", "/api/backfill/rollback", {"配置编号": "SINK-0005"})
check("sink-lake 回滚受理", body["ok"] is True, str(body))
_, sink5 = call("GET", "/api/sink/5")
check(f"回滚后契约恢复到上一版本 {prev_version}", sink5["生效版本"] == prev_version)
_, rb = call("GET", "/api/backfill?" + urllib.parse.urlencode({"batch": "rollback_SINK-0005"}))
check("回滚记录全部标记已回滚且目标版本正确",
      rb["total"] >= 3 and all(r["status"] == "已回滚" and r["目标版本"] == prev_version for r in rb["items"]))

print("== 导出与既有模块未受影响 ==")
_, body = call("GET", "/api/backfill/export")
export_nos = {i["记录编号"] for i in body["items"]}
check("导出全量回填结果且含全部种子记录",
      body["module"] == "backfill"
      and {"BFR-0001", "BFR-0002", "BFR-0003", "BFR-0004", "BFR-0005", "BFR-0006"} <= export_nos)
_, body = call("GET", "/api/schema")
check("schema 既有 9 条记录完好", body["total"] == 9, str(body["total"]))
_, body = call("GET", "/api/sink")
check("sink 既有 5 条记录完好", body["total"] == 5, str(body["total"]))
_, body = call("GET", "/api/overview")
check("运营概览模块数 17", len(body["modules"]) == 17, str(len(body["modules"])))

print(f"\nHTTP 结果：通过 {PASS} 项，失败 {FAIL} 项")
sys.exit(1 if FAIL else 0)
