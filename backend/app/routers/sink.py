"""流数据输出端接口：维护输出配置，覆盖启用输出、暂停输出、关闭输出等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.sink import SinkService

router = APIRouter(prefix="/api/sink", tags=["流数据输出端"])

service = SinkService()

LIST_FIELDS = ["配置编号", "输出类型", "目标地址", "写入模式", "批处理大小", "刷新间隔", "重试策略", "配置状态"]
STATUSES = ["草稿", "已启用", "已暂停", "已关闭"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按配置编号检索"),
    status: str | None = Query(default=None, description="草稿、已启用、已暂停、已关闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按配置编号与状态过滤流数据输出端列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条输出配置明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"输出配置 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/backfills")
def list_backfills(entry_id: int) -> dict[str, Any]:
    """输出端消费侧：读取该输出配置共享的字段回填结果，用记录编号关联。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"输出配置 {entry_id} 不存在或已归档")
    items = service.list_backfills(entry_id)
    return {"sink_id": entry_id, "total": len(items), "items": items}


@router.get("/backfills/record/{record_no}", response_model=dict)
def get_backfill_by_record_no(record_no: str) -> dict[str, Any]:
    """输出端消费侧：按记录编号关联同一份字段回填结果。"""
    entry = service.get_backfill_by_record_no(record_no)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"字段回填结果 {record_no} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条输出配置，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="输出配置已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条输出配置执行启用输出、暂停输出、关闭输出；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出流数据输出端清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "sink", "total": total, "items": items}
