#!/usr/bin/env python3
"""验收样例专项验证：schema_audit_v1 的 record-21a~21c。

可核对结果：字段回填结果保留原存储类型、默认值和目标版本；
回填后仍对应 sink-archive 旧写入契约，失败列保留原映射。
仅用标准库，运行前请保证后端为初始种子状态（重启服务）。
"""
from __future__ import annotations

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8011"
PASS = FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {detail}")


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post(path: str, values: dict) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps({"values": values}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


print("== 0. 重置内存仓库，保证验收可重复执行 ==")
reset = post("/api/backfill/test/reset", {})
check("仓库重置成功", reset.get("ok") is True, str(reset))

print("== 1. 重新执行 schema_audit_v1 批量回填（幂等，不产生重复登记） ==")
before = get("/api/backfill?batch=schema_audit_v1")["total"]
result = post("/api/backfill/batches", {
    "回填批次": "schema_audit_v1",
    "升级场景": "旧数据库",
    "items": [
        {"定义编号": "SCHE-0004", "配置编号": "SINK-0004", "字段名称": "record-21a"},
        {"定义编号": "SCHE-0005", "配置编号": "SINK-0004", "字段名称": "record-21b"},
        {"定义编号": "SCHE-0006", "配置编号": "SINK-0004", "字段名称": "record-21c"},
    ],
})
check("批量回填受理", result["ok"] is True, result.get("message", ""))
after = get("/api/backfill?batch=schema_audit_v1")["total"]
check("重复登记只留一份记录（仍 3 条）", before == after == 3, f"{before} -> {after}")

print("== 2. 可核对结果：原存储类型、默认值、目标版本 ==")
expect = {
    "record-21a": ("varchar", "", "回填成功"),
    "record-21b": ("bigint", "0", "回填成功"),
    "record-21c": ("string", "", "回填失败"),
}
record_paths = {
    "record-21a": "/api/backfill/record/BFR-0001",
    "record-21b": "/api/backfill/record/BFR-0002",
    "record-21c": "/api/backfill/record/BFR-0003",
}
for field, (storage, default, status) in expect.items():
    row = get(record_paths[field])
    check(f"{field} 原存储类型={storage}", row["原存储类型"] == storage and row["存储类型"] == storage)
    check(f"{field} 默认值={default!r}", row["原默认值"] == default and row["默认值"] == default)
    check(f"{field} 目标版本=v2", row["目标版本"] == "v2")
    check(f"{field} 状态={status}", row["status"] == status)

print("== 3. 回填后仍对应 sink-archive 旧写入契约 ==")
sink = get("/api/sink/4")
check("输出配置仍是 sink-archive", sink["输出类型"] == "sink-archive")
check("旧写入契约保持 v1 未切换", sink["生效版本"] == "v1" and sink["契约版本"] == "v1")
for field in ("record-21a", "record-21b", "record-21c"):
    mapping = next(m for m in sink["字段映射"] if m["字段名称"] == field)
    check(f"{field} 字段映射维持旧契约格式版本 v1", mapping["格式版本"] == "v1")

print("== 4. 失败列 record-21c 保留原映射 ==")
failed = get("/api/backfill/record/BFR-0003")
check("21c 标记断点续做", failed["断点续做"] is True)
check("21c 保留原存储类型 string 与原版本 v1",
      failed["存储类型"] == "string" and failed["格式版本"] == "v1")
check("21c 兼容判定不通过且原因可读", failed["兼容判定"] == "不通过" and failed["失败原因"])

print("== 5. 消费侧（sink 入口）按记录编号关联同一份结果 ==")
consumer = get("/api/sink/backfills/record/BFR-0003")
check("sink 入口按 BFR-0003 关联到同一失败列",
      consumer["记录编号"] == "BFR-0003" and consumer["字段名称"] == "record-21c")
shared = get("/api/sink/4/backfills")
check("sink-4 共享结果恰为 schema_audit_v1 三条",
      shared["total"] == 3
      and sorted(i["记录编号"] for i in shared["items"]) == ["BFR-0001", "BFR-0002", "BFR-0003"])

print(f"\n验收结果：通过 {PASS} 项，失败 {FAIL} 项")
sys.exit(1 if FAIL else 0)
