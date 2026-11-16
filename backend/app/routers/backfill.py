"""字段回填结果接口：批量回填、断点续做、部署回滚与消费侧按记录编号关联。

格式定义侧与输出端消费侧共用这一组接口、同一份回填结果，避免两边各写一套
兼容判定与版本登记。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.backfill import SCENARIOS, BackfillService
from app.store import store

router = APIRouter(prefix="/api/backfill", tags=["字段回填结果"])

service = BackfillService()

LIST_FIELDS = [
    "记录编号", "回填批次", "定义编号", "字段名称", "配置编号", "契约编号",
    "原存储类型", "存储类型", "原默认值", "默认值", "原格式版本", "格式版本",
    "目标版本", "升级场景", "归档列", "目标类型", "兼容判定", "冲突标记",
    "失败原因", "断点续做", "契约是否切换",
]
STATUSES = ["待回填", "回填成功", "回填失败", "已回滚"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按记录编号检索"),
    status: str | None = Query(default=None, description="待回填、回填成功、回填失败、已回滚"),
    batch: str | None = Query(default=None, description="按回填批次过滤，如 schema_audit_v1"),
    scene: str | None = Query(default=None, description="旧数据库、新环境、部署回滚、依赖兼容"),
    sink_code: str | None = Query(default=None, description="按输出配置编号过滤共享结果"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """分页读取字段回填结果；回填结果保留原存储类型、默认值和目标版本。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword, status=status, batch=batch, scene=scene,
        sink_code=sink_code, page=page, size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出全量字段回填结果，供两个业务入口核对同一份事实源。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "backfill", "total": total, "items": items}


@router.post("/test/reset", response_model=ActionResult)
def reset_store() -> ActionResult:
    """测试支撑：按种子数据重建内存仓库，让验证脚本可重复执行。"""
    store.reset()
    return ActionResult(ok=True, message="内存仓库已重置为种子数据")


@router.get("/record/{record_no}", response_model=dict)
def get_by_record_no(record_no: str) -> dict[str, Any]:
    """消费侧按记录编号关联同一份字段回填结果。"""
    entry = service.find_by_record_no(record_no)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"字段回填结果 {record_no} 不存在或已归档")
    return entry


@router.get("/sink/{sink_id}")
def list_by_sink(sink_id: int) -> dict[str, Any]:
    """另一个业务入口（输出端）按输出配置 id 读取共享回填结果。"""
    items = service.list_by_sink(sink_id)
    return {"sink_id": sink_id, "total": len(items), "items": items}


@router.post("/batches", response_model=ActionResult)
def run_backfill(payload: EntryPayload) -> ActionResult:
    """批量回填：读取 SCHE 与 SINK 映射核对存量字段，成功后联动输出配置契约。"""
    summary, message = service.run_backfill(payload.values)
    if summary is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=summary)


@router.post("/batches/resume", response_model=ActionResult)
def resume_batch(payload: EntryPayload) -> ActionResult:
    """失败批次断点续做：沿用原记录编号重跑失败列，成功后再尝试切换契约。"""
    summary, message = service.resume_batch(payload.values)
    if summary is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=summary)


@router.post("/rollback", response_model=ActionResult)
def rollback_contract(payload: EntryPayload) -> ActionResult:
    """部署回滚：输出配置契约恢复到上一版本，回填记录标记为已回滚。"""
    summary, message = service.rollback_contract(payload.values)
    if summary is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=summary)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """单条动作：重试回填（失败列）、回滚契约。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
