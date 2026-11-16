"""字段回填结果业务规则。

这里是存量格式定义批量升级的唯一编排入口，统一收口四件事，避免版本重复登记与
兼容判定口径不一致：

1. 读取 SCHE 格式定义（按字段名称、存储类型、格式版本核对存量字段）与 SINK 输出
   配置的字段映射，校验通过后才允许回填；
2. 同一份字段回填结果被格式定义侧与输出端消费侧共享，消费侧用记录编号关联；
3. 同一字段并发升级只提交一个版本（自然键去重 + 进程内写锁），失败批次保留旧
   映射，靠记录编号断点续做；
4. 回填结果与输出配置契约在同一批次内一起更新：批次内存在失败列时契约不切换，
   旧归档列与新目标类型同时存在时按冲突拦截，禁止两种存储类型混入同一批次。
"""
from __future__ import annotations

import threading
from copy import deepcopy
from typing import Any

from app.store import store

MODULE = "backfill"
SCHEMA_MODULE = "schema"
SINK_MODULE = "sink"

REQUIRED_FIELDS = ["记录编号", "回填批次", "定义编号", "字段名称", "配置编号", "目标版本"]
STATUS_ORDER = ["待回填", "回填成功", "回填失败", "已回滚"]
# 升级流程必须覆盖的四类场景
SCENARIOS = ["旧数据库", "新环境", "部署回滚", "依赖兼容"]

# 统一兼容判定矩阵：键为原存储类型，值为允许迁入的目标类型集合。
# 全平台只有这一份口径，四个升级场景都走同一个 _type_compatible，杜绝各入口判定不一致。
TYPE_COMPATIBILITY: dict[str, set[str]] = {
    "varchar": {"varchar", "string", "text"},
    # string 收窄到 varchar 不兼容：record-21c 即在此被拦下
    "string": {"string", "text"},
    "text": {"text", "string", "varchar"},
    "bigint": {"bigint", "long", "decimal", "double", "varchar", "string"},
    "long": {"long", "bigint", "decimal", "double", "varchar", "string"},
    "int": {"int", "bigint", "long", "decimal", "double", "varchar", "string"},
    "decimal": {"decimal", "double", "varchar", "string"},
    "double": {"double", "decimal", "varchar", "string"},
    "timestamp": {"timestamp", "varchar", "string"},
    "boolean": {"boolean", "varchar", "string"},
}

# 同一字段并发升级的自然键：定义 + 字段 + 输出配置 + 目标版本。
# 同键重复登记只保留一份记录（沿用原记录编号），不再产生新版本。
NATURAL_KEYS = ("定义编号", "字段名称", "配置编号", "目标版本")

_WRITE_LOCK = threading.RLock()


def _norm_version(version: Any) -> str:
    """把 v1 / V1 / 1 归一化成 v1，保证版本比较与去重口径一致。"""
    text = str(version or "").strip().lower()
    if not text:
        return ""
    if text[0] != "v":
        text = f"v{text}"
    return text


def _version_rank(version: str) -> int:
    """提取版本序号用于先后比较；无法识别时排在最后，避免误切换契约。"""
    digits = "".join(ch for ch in str(version) if ch.isdigit())
    return int(digits) if digits else -1


class BackfillService:
    """字段回填结果服务：列表检索、批量回填、断点续做、回滚与消费侧关联查询。"""

    def __init__(self) -> None:
        # 启动即清理重复登记，保证存量数据里同一自然键只有一份记录
        self._dedupe_table()

    # ------------------------------------------------------------------ 基础检索
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        batch: str | None = None,
        scene: str | None = None,
        sink_code: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("记录编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if batch:
            rows = [row for row in rows if row.get("回填批次") == batch]
        if scene:
            rows = [row for row in rows if row.get("升级场景") == scene]
        if sink_code:
            rows = [row for row in rows if row.get("配置编号") == sink_code]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def find_by_record_no(self, record_no: str) -> dict[str, Any] | None:
        """消费侧按记录编号关联同一份回填结果。"""
        for row in store.rows(MODULE):
            if row.get("记录编号") == record_no:
                return row
        return None

    def list_by_sink(self, sink_id: int) -> list[dict[str, Any]]:
        """另一个业务入口（输出端）按输出配置 id 取共享回填结果。"""
        sink = store.find(SINK_MODULE, sink_id)
        if sink is None:
            return []
        code = sink.get("配置编号")
        return [row for row in store.rows(MODULE) if row.get("配置编号") == code]

    # ------------------------------------------------------------------ 存量核对
    def _locate_schema(self, definition_code: str, field_name: str,
                       storage_type: str | None, format_version: str | None,
                       ) -> tuple[dict[str, Any] | None, str]:
        """按字段名称、存储类型和格式版本核对 SCHE 存量定义。

        缺存储类型/版本时只按字段名称核对（兼容既有字段）；提交了就必须一致，
        不一致直接判定为存量核对失败，不允许拿错版本做升级。
        """
        candidate = None
        for row in store.rows(SCHEMA_MODULE):
            if row.get("定义编号") == definition_code and row.get("字段名称") == field_name:
                candidate = row
                break
        if candidate is None:
            return None, f"格式定义 {definition_code} 中找不到字段 {field_name}，存量核对失败"
        actual_type = candidate.get("存储类型") or candidate.get("字段类型") or ""
        if storage_type and str(storage_type) != str(actual_type):
            return None, (
                f"字段 {field_name} 存储类型核对不一致：提交 {storage_type}，"
                f"SCHE 存量为 {actual_type}"
            )
        actual_version = _norm_version(candidate.get("格式版本"))
        if format_version and _norm_version(format_version) != actual_version:
            return None, (
                f"字段 {field_name} 格式版本核对不一致：提交 {_norm_version(format_version)}，"
                f"SCHE 存量为 {actual_version}"
            )
        return candidate, ""

    def _locate_mapping(self, sink: dict[str, Any], field_name: str,
                        archive_column: str | None = None) -> dict[str, Any] | None:
        """在 SINK 字段映射里定位字段：优先字段名称，再退到旧归档列。"""
        mappings = sink.get("字段映射") or []
        for mapping in mappings:
            if mapping.get("字段名称") == field_name:
                return mapping
        if archive_column:
            for mapping in mappings:
                if mapping.get("归档列") == archive_column:
                    return mapping
        return None

    # ------------------------------------------------------------------ 兼容判定
    def _type_compatible(self, source_type: str, target_type: str) -> bool:
        """唯一的类型兼容判定入口；decimal(18,2) 这类带精度目标按族匹配。"""
        source = str(source_type or "").strip().lower()
        target = str(target_type or "").strip().lower()
        if not target or target == source:
            return True
        if target.startswith("decimal") and source == "decimal":
            return True
        allowed = TYPE_COMPATIBILITY.get(source)
        if allowed is None:
            # 未登记的存量类型走 fail-closed，不静默放行
            return False
        return target in allowed

    def _check_compatibility(self, record: dict[str, Any], sink: dict[str, Any]) -> str:
        """返回空串表示通过，否则返回可读的不通过原因。"""
        scenario = record["升级场景"]
        if scenario not in SCENARIOS:
            return f"升级场景「{scenario}」不在支持范围：{'、'.join(SCENARIOS)}"
        target_version = record["目标版本"]
        if scenario == "部署回滚":
            previous = _norm_version(sink.get("上一契约版本"))
            if previous and target_version != previous:
                return (
                    f"部署回滚目标版本必须与上一契约版本 {previous} 一致，"
                    f"当前请求 {target_version}"
                )
        if scenario == "依赖兼容":
            supported = {_norm_version(item) for item in sink.get("支持版本") or []}
            if supported and target_version not in supported:
                return (
                    f"依赖兼容不通过：{sink.get('配置编号')} 支持版本 "
                    f"{'、'.join(sorted(supported))}，不含目标版本 {target_version}"
                )
        if not self._type_compatible(record["原存储类型"], record.get("目标类型")):
            return (
                f"兼容判定不通过：{record['原存储类型']}→{record.get('目标类型')} "
                f"在{scenario}口径下不允许"
            )
        return ""

    # ------------------------------------------------------------------ 去重/编号
    def _dedupe_table(self) -> int:
        """同一自然键重复登记只留一份：成功记录优先，否则保留 id 最大者。"""
        rows = store.rows(MODULE)
        kept: dict[tuple, dict[str, Any]] = {}
        for row in rows:
            key = tuple(row.get(field) for field in NATURAL_KEYS)
            current = kept.get(key)
            if current is None:
                kept[key] = row
                continue
            if current.get("status") == "回填成功":
                winner = current
            elif row.get("status") == "回填成功":
                winner = row
            else:
                winner = row if int(row.get("id", 0)) > int(current.get("id", 0)) else current
            kept[key] = winner
        deduped = list(kept.values())
        deduped.sort(key=lambda item: int(item.get("id", 0)))
        removed = len(rows) - len(deduped)
        if removed:
            rows[:] = deduped
        return removed

    def _next_record_no(self) -> str:
        rows = store.rows(MODULE)
        next_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        return f"BFR-{next_id:04d}"

    def _find_existing(self, values: dict[str, Any]) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if all(row.get(field) == values.get(field) for field in NATURAL_KEYS):
                return row
        return None

    # ------------------------------------------------------------------ 批量回填
    def run_backfill(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """批量回填主流程。

        入参 values：
          回填批次、升级场景、目标版本（可选，缺省取 SCHE 当前版本的下一版），
          items: [{定义编号, 配置编号, 字段名称, 归档列?, 目标类型?, 升级场景?}]
        """
        with _WRITE_LOCK:
            return self._run_backfill_locked(values)

    def _run_backfill_locked(self, values: dict[str, Any],
                             allow_downgrade: bool = False
                             ) -> tuple[dict[str, Any] | None, str]:
        batch_no = str(values.get("回填批次") or "").strip()
        scenario = str(values.get("升级场景") or "").strip()
        items = values.get("items") or []
        if not batch_no:
            return None, "缺少回填批次，无法登记字段回填结果"
        if scenario not in SCENARIOS:
            return None, f"升级场景「{scenario}」不在支持范围：{'、'.join(SCENARIOS)}"
        if not isinstance(items, list) or not items:
            return None, "回填明细 items 为空，没有可升级的字段"

        # 快照受影响的 SINK 行，处理中抛错就整体还原，保证契约与结果要么一起成要么不动
        sink_codes = {str(item.get("配置编号") or "").strip() for item in items}
        snapshots = {
            code: deepcopy(sink)
            for code in sink_codes
            if (sink := self._find_sink_by_code(code)) is not None
        }
        if len(snapshots) != len({code for code in sink_codes if code}):
            return None, "存在找不到的输出配置编号，请先核对 SINK 输出配置"
        try:
            processed: list[dict[str, Any]] = []
            for item in items:
                record, reason = self._build_record(item, batch_no, scenario, values.get("目标版本"))
                if record is None:
                    # 入参级硬错误：整批拒绝，不产生半截数据
                    self._restore_sinks(snapshots)
                    return None, reason
                processed.append(record)

            # 批次级存储类型混合检测：同一 SINK 不允许旧列写入与新类型写入混跑
            self._mark_batch_conflicts(processed)

            # 全部成功的 SINK 才允许切换契约；批次内仍有失败列时保留旧契约、旧映射
            switched = self._apply_contracts(batch_no, processed, allow_downgrade=allow_downgrade)
            summary = self._summarize(batch_no, processed, switched)
            return summary, self._summary_message(summary)
        except Exception as exc:  # pragma: no cover - 兜底保护内存状态
            self._restore_sinks(snapshots)
            return None, f"批量回填执行异常，已回滚契约改动：{exc}"

    def _build_record(self, item: dict[str, Any], batch_no: str,
                      default_scenario: str, default_target: Any,
                      ) -> tuple[dict[str, Any] | None, str]:
        definition_code = str(item.get("定义编号") or "").strip()
        sink_code = str(item.get("配置编号") or "").strip()
        field_name = str(item.get("字段名称") or "").strip()
        if not definition_code or not sink_code or not field_name:
            return None, "回填明细缺少 定义编号/配置编号/字段名称，无法核对存量字段"
        sink = self._find_sink_by_code(sink_code)
        if sink is None:
            return None, f"输出配置 {sink_code} 不存在，无法读取字段映射"
        mapping = self._locate_mapping(sink, field_name, item.get("归档列"))
        if mapping is None:
            return None, f"输出配置 {sink_code} 的字段映射中找不到字段 {field_name}"

        submit_type = item.get("存储类型") or mapping.get("存储类型")
        # 格式版本只接受明细显式提交值，不用 SINK 映射版本兜底：契约侧版本允许与
        # SCHE 存量版本漂移，这正是本次升级要收敛的差异，不能拿它卡存量核对。
        submit_version = item.get("格式版本")
        schema_row, reason = self._locate_schema(
            definition_code, field_name, submit_type, submit_version
        )
        if schema_row is None:
            return None, reason

        current_version = _norm_version(schema_row.get("格式版本"))
        target_version = _norm_version(item.get("目标版本") or default_target)
        if not target_version:
            target_version = f"v{max(_version_rank(current_version), 0) + 1}"
        # 同一字段并发升级的自然键，重复登记只保留一份记录
        values = {
            "定义编号": definition_code,
            "字段名称": field_name,
            "配置编号": sink_code,
            "目标版本": target_version,
        }
        # 目标类型取值优先级：显式沿用归档列（强制旧路径）> 明细指定 > 同键历史回填
        # 结果 > SINK 字段映射。保证失败批次续做时沿用原升级目标，判定口径不漂移。
        if item.get("沿用归档列"):
            target_type = None
        else:
            target_type = item.get("目标类型")
            if target_type is None:
                existing_for_type = self._find_existing(values)
                if existing_for_type is not None:
                    target_type = existing_for_type.get("目标类型")
            if target_type is None:
                target_type = mapping.get("目标类型")
            target_type = target_type or None
        scenario = str(item.get("升级场景") or default_scenario).strip()

        # 回填结果必须保留原存储类型、默认值和目标版本
        original_type = schema_row.get("存储类型") or schema_row.get("字段类型") or ""
        original_default = schema_row.get("默认值")
        if original_default is None:
            original_default = mapping.get("默认值", "")

        existing = self._find_existing(values)
        record = existing if existing is not None else {
            "id": self._next_id(),
            "记录编号": self._next_record_no(),
        }
        record.update({
            "回填批次": batch_no,
            "定义编号": definition_code,
            "字段名称": field_name,
            "配置编号": sink_code,
            "契约编号": sink.get("契约编号") or sink_code,
            "原存储类型": original_type,
            "存储类型": original_type,
            "原默认值": original_default,
            "默认值": original_default,
            "原格式版本": current_version,
            "格式版本": current_version,
            "目标版本": target_version,
            "升级场景": scenario,
            "归档列": mapping.get("归档列") or item.get("归档列") or "",
            "目标类型": target_type,
            "冲突标记": False,
            "失败原因": "",
            "断点续做": False,
            "契约是否切换": False,
        })

        # 单字段同时挂着旧归档列与新目标类型时不直接判失败：该字段的共享回填结果
        # 本身就是唯一取舍，真正的冲突是同一批次混写两种存储类型，见 _mark_batch_conflicts。
        compat_reason = self._check_compatibility(record, sink)

        if compat_reason:
            record["status"] = "回填失败"
            record["pending"] = True
            record["abnormal"] = True
            record["兼容判定"] = "不通过"
            record["冲突标记"] = False
            record["失败原因"] = compat_reason
            record["断点续做"] = True
            # 失败列保留旧映射：存储类型/默认值/格式版本全部维持存量原值
            record["存储类型"] = original_type
            record["默认值"] = original_default
            record["格式版本"] = current_version
        else:
            record["status"] = "回填成功"
            record["pending"] = target_version != current_version
            record["abnormal"] = False
            record["兼容判定"] = "通过"
            record["失败原因"] = ""
            record["断点续做"] = False

        if existing is None:
            store.rows(MODULE).append(record)
        return record, ""

    def _mark_batch_conflicts(self, processed: list[dict[str, Any]]) -> None:
        """同一 SINK 批次内旧列写入与新类型写入混跑时，给新类型侧打冲突标记。"""
        by_sink: dict[str, list[dict[str, Any]]] = {}
        for record in processed:
            by_sink.setdefault(record["配置编号"], []).append(record)
        for sink_code, records in by_sink.items():
            has_old_only = any(not item.get("目标类型") for item in records)
            has_new_type = any(item.get("目标类型") for item in records)
            if has_old_only and has_new_type:
                for item in records:
                    if item.get("目标类型") and item["status"] == "回填成功":
                        item["status"] = "回填失败"
                        item["pending"] = True
                        item["abnormal"] = True
                        item["兼容判定"] = "不通过"
                        item["冲突标记"] = True
                        item["断点续做"] = True
                        item["失败原因"] = (
                            f"批次内 {sink_code} 同时存在旧归档列写入与新目标类型写入，"
                            "两种存储类型不能进入同一批次，已拦截契约切换"
                        )

    def _apply_contracts(self, batch_no: str, processed: list[dict[str, Any]],
                         allow_downgrade: bool = False) -> list[str]:
        """回填结果与输出配置契约一起更新；返回成功切换契约的 SINK 编号列表。

        切换条件按整批口径判断：只要该 SINK 在本批次里还存在任一失败列（含本次未
        重跑的历史失败记录），契约与字段映射就整体不动，等断点续做全部成功后再切。
        普通回填只允许向上升版；部署回滚走 allow_downgrade 显式放行降级。
        """
        switched: list[str] = []
        by_sink: dict[str, list[dict[str, Any]]] = {}
        for record in processed:
            by_sink.setdefault(record["配置编号"], []).append(record)
        all_rows = store.rows(MODULE)
        for sink_code, records in by_sink.items():
            sink = self._find_sink_by_code(sink_code)
            if sink is None:
                continue
            batch_failures = [
                row for row in all_rows
                if row.get("回填批次") == batch_no
                and row.get("配置编号") == sink_code
                and row.get("status") == "回填失败"
            ]
            if batch_failures:
                # 批次仍有失败列：契约与字段映射整体不动，失败列保留原映射
                for item in records:
                    item["契约是否切换"] = False
                continue
            target_version = records[0]["目标版本"]
            current_version = _norm_version(sink.get("生效版本"))
            if target_version == current_version:
                for item in records:
                    item["契约是否切换"] = False
                continue
            if not allow_downgrade and _version_rank(target_version) <= _version_rank(current_version):
                # 普通回填不允许借回填把契约切到更旧版本，回滚走专门入口
                continue
            sink["上一契约版本"] = current_version
            sink["契约版本"] = target_version
            sink["生效版本"] = target_version
            mappings = sink.setdefault("字段映射", [])
            target_type_by_field = {
                row.get("字段名称"): row.get("目标类型")
                for row in all_rows
                if row.get("回填批次") == batch_no and row.get("配置编号") == sink_code
            }
            for mapping in mappings:
                field = mapping.get("字段名称")
                if field in target_type_by_field:
                    # 只在记录带真实新目标类型时更新；沿用归档列/回滚（None）保留映射
                    # 原目标类型，避免清空升级元数据导致后续冲突检测与再升级失效
                    new_type = target_type_by_field[field]
                    if new_type is not None:
                        mapping["目标类型"] = new_type
                    mapping["格式版本"] = target_version
            # 契约切换时，本批次该 SINK 下全部成功记录一起升到目标版本
            for row in all_rows:
                if (
                    row.get("回填批次") == batch_no
                    and row.get("配置编号") == sink_code
                    and row.get("status") == "回填成功"
                ):
                    row["格式版本"] = target_version
                    row["契约是否切换"] = True
                    row["pending"] = False
            switched.append(sink_code)
        return switched

    # ------------------------------------------------------------------ 断点续做
    def resume_batch(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """失败批次断点续做：沿用原记录编号重跑失败列，成功后再联动契约。"""
        with _WRITE_LOCK:
            batch_no = str(values.get("回填批次") or "").strip()
            if not batch_no:
                return None, "缺少回填批次，无法断点续做"
            overrides = {
                str(item.get("定义编号")): item
                for item in values.get("items") or []
                if item.get("定义编号")
            }
            failed = [
                row for row in store.rows(MODULE)
                if row.get("回填批次") == batch_no and row.get("status") == "回填失败"
            ]
            if not failed:
                return None, f"批次 {batch_no} 没有待续做的失败记录"
            items = []
            for row in failed:
                item = {
                    "定义编号": row["定义编号"],
                    "配置编号": row["配置编号"],
                    "字段名称": row["字段名称"],
                    "归档列": row.get("归档列"),
                    "目标类型": row.get("目标类型"),
                    "存储类型": row.get("原存储类型"),
                    "格式版本": row.get("原格式版本"),
                    "升级场景": row.get("升级场景"),
                    "目标版本": row.get("目标版本"),
                }
                item.update(overrides.get(row["定义编号"], {}))
                items.append(item)
            return self._run_backfill_locked({
                "回填批次": batch_no,
                "升级场景": failed[0].get("升级场景"),
                "目标版本": failed[0].get("目标版本"),
                "items": items,
            })

    # ------------------------------------------------------------------ 部署回滚
    def rollback_contract(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """部署回滚：把 SINK 契约恢复到上一版本，并为每个字段留一份回滚回填记录。"""
        with _WRITE_LOCK:
            sink_code = str(values.get("配置编号") or "").strip()
            batch_no = str(values.get("回填批次") or "").strip() or f"rollback_{sink_code}"
            sink = self._find_sink_by_code(sink_code)
            if sink is None:
                return None, f"输出配置 {sink_code} 不存在，无法回滚契约"
            previous = _norm_version(sink.get("上一契约版本"))
            current = _norm_version(sink.get("生效版本"))
            if not previous:
                return None, f"输出配置 {sink_code} 没有上一契约版本，无法回滚"
            if previous == current:
                return None, (
                    f"输出配置 {sink_code} 上一契约版本与当前版本均为 {current}，"
                    "没有可回退的历史版本"
                )
            items = []
            for mapping in sink.get("字段映射") or []:
                schema_row = self._find_schema_by_field(sink_code, mapping.get("字段名称"))
                if schema_row is None:
                    continue
                items.append({
                    "定义编号": schema_row["定义编号"],
                    "配置编号": sink_code,
                    "字段名称": mapping.get("字段名称"),
                    "归档列": mapping.get("归档列"),
                    "目标类型": None,
                    "沿用归档列": True,
                    "升级场景": "部署回滚",
                    "目标版本": previous,
                })
            if not items:
                return None, f"输出配置 {sink_code} 没有可回滚的字段映射"
            summary, message = self._run_backfill_locked({
                "回填批次": batch_no,
                "升级场景": "部署回滚",
                "目标版本": previous,
                "items": items,
            }, allow_downgrade=True)
            if summary is None:
                return None, message
            # 契约降级与字段映射回退已在 _apply_contracts(allow_downgrade=True) 完成，
            # 这里只把回滚批次的记录状态收敛为已回滚
            for row in store.rows(MODULE):
                if row.get("回填批次") == batch_no and row.get("配置编号") == sink_code:
                    row["status"] = "已回滚"
                    row["pending"] = False
                    row["abnormal"] = False
                    row["格式版本"] = previous
                    row["契约是否切换"] = True
            return summary, f"契约已回滚到 {previous}：{message}"

    # ------------------------------------------------------------------ 单条动作
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"字段回填结果 {entry_id} 不存在或已归档"
        if action == "重试回填":
            if entry.get("status") != "回填失败":
                return None, "只有回填失败的记录允许重试回填"
            summary, message = self.resume_batch({"回填批次": entry.get("回填批次")})
            if summary is None:
                return None, message
            refreshed = self.find_by_record_no(entry["记录编号"])
            return refreshed, message
        if action == "回滚契约":
            return self.rollback_contract({
                "配置编号": entry.get("配置编号"),
                "回填批次": f"rollback_{entry.get('回填批次')}",
            })
        return None, f"动作「{action}」不属于字段回填结果可执行范围"

    # ------------------------------------------------------------------ 内部工具
    def _find_sink_by_code(self, sink_code: str) -> dict[str, Any] | None:
        for row in store.rows(SINK_MODULE):
            if row.get("配置编号") == sink_code:
                return row
        return None

    def _find_schema_by_field(self, sink_code: str, field_name: str) -> dict[str, Any] | None:
        sink = self._find_sink_by_code(sink_code)
        if sink is None:
            return None
        for row in store.rows(SCHEMA_MODULE):
            if row.get("字段名称") == field_name and row.get("流名称"):
                return row
        return None

    def _next_id(self) -> int:
        return max((int(row.get("id", 0)) for row in store.rows(MODULE)), default=0) + 1

    def _restore_sinks(self, snapshots: dict[str, dict[str, Any]]) -> None:
        for code, snapshot in snapshots.items():
            sink = self._find_sink_by_code(code)
            if sink is not None:
                sink.clear()
                sink.update(deepcopy(snapshot))

    def _summarize(self, batch_no: str, processed: list[dict[str, Any]],
                   switched: list[str]) -> dict[str, Any]:
        failed = [item for item in processed if item["status"] == "回填失败"]
        conflicts = [item for item in processed if item.get("冲突标记")]
        return {
            "回填批次": batch_no,
            "记录总数": len(processed),
            "成功数": len(processed) - len(failed),
            "失败数": len(failed),
            "冲突数": len(conflicts),
            "契约切换": switched,
            "失败记录编号": [item["记录编号"] for item in failed],
            "items": processed,
        }

    def _summary_message(self, summary: dict[str, Any]) -> str:
        text = (
            f"批次 {summary['回填批次']} 回填完成：成功 {summary['成功数']} 条、"
            f"失败 {summary['失败数']} 条"
        )
        if summary["冲突数"]:
            text += f"、冲突 {summary['冲突数']} 条"
        if summary["契约切换"]:
            text += f"；已联动切换契约：{'、'.join(summary['契约切换'])}"
        elif summary["失败数"]:
            text += "；存在失败列，输出配置保留旧契约与旧映射，可按记录编号断点续做"
        return text
