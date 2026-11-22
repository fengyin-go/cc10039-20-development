"""注册分区规则包接口：文件导入、规则行追溯、湖表分区登记。

这些端点使用固定路径，必须比通用资源路由先注册，否则会被
``/api/{module}/{entry_id}`` 抢先匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ImportPayload, PartitionRegisterPayload
from app.services import service_for
from app.services.partition_rule_pack import PartitionRulePackService
from app.store import store

router = APIRouter(prefix="/api", tags=["注册分区规则包"])


def _pack_service() -> PartitionRulePackService:
    service = service_for("partition_rule_pack")
    if not isinstance(service, PartitionRulePackService):
        raise HTTPException(status_code=500, detail="分区规则包服务未注册")
    return service


@router.post("/partition_rule_pack/import")
def import_rule_pack(payload: ImportPayload) -> dict[str, Any]:
    """接收分区策略文件：解析目标表、日期与分区粒度，逐行校验后登记。"""
    result = _pack_service().import_file(
        file_name=payload.file_name,
        content=payload.content,
        default_table=payload.default_table,
        remark=payload.remark or "",
    )
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@router.get("/partition_rule_pack/{pack_id}/lines")
def pack_lines(pack_id: int) -> dict[str, Any]:
    """查看规则包的逐行校验与登记结果，含来源文件与行号。"""
    service = _pack_service()
    pack = store.find("partition_rule_pack", pack_id)
    if pack is None:
        raise HTTPException(status_code=404, detail=f"分区规则包 {pack_id} 不存在")
    lines = service.lines_for_pack(pack_id)
    return {"pack": pack, "summary": service.pack_summary(pack_id), "items": lines}


@router.get("/partition_rule_pack/{pack_id}/partitions")
def pack_partitions(pack_id: int) -> dict[str, Any]:
    """查看由该规则包写给湖表的分区记录。"""
    service = _pack_service()
    pack = store.find("partition_rule_pack", pack_id)
    if pack is None:
        raise HTTPException(status_code=404, detail=f"分区规则包 {pack_id} 不存在")
    items = service.partitions(pack_id=pack_id)
    return {"pack": pack, "total": len(items), "items": items}


@router.get("/partition_rule/rules")
def rule_lines(
    table: str | None = Query(default=None),
    date: str | None = Query(default=None),
) -> dict[str, Any]:
    """按目标表/日期查询规则行：每行都带生效状态和来源文件行号。"""
    items = _pack_service().all_lines(table=table, date=date)
    return {"total": len(items), "items": items}


@router.get("/lake_partition")
def lake_partitions(
    table: str | None = Query(default=None),
    date: str | None = Query(default=None),
) -> dict[str, Any]:
    """查询湖表分区台账：每个日期分区对应唯一来源文件与行号。"""
    items = _pack_service().partitions(table=table, date=date)
    return {"total": len(items), "items": items}


@router.get("/lake_table/{entry_id}/pending-partitions")
def pending_partitions(entry_id: int) -> dict[str, Any]:
    """查看湖表尚待登记的日期，以及当前生效规则的解释。"""
    entry = store.find("lake_table", entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"湖表 {entry_id} 不存在")
    items = _pack_service().pending_lines(str(entry.get("表名") or ""))
    return {"table": entry.get("表名"), "total": len(items), "items": items}


@router.post("/lake_table/{entry_id}/register-partitions")
def register_partitions(entry_id: int, payload: PartitionRegisterPayload) -> dict[str, Any]:
    """湖表登记从规则包领取分区：可指定日期或只领取第一片（limit）。"""
    entry = store.find("lake_table", entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"湖表 {entry_id} 不存在")
    table = str(entry.get("表名") or "")
    result = _pack_service().register_partitions(
        table,
        pack_id=payload.pack_id,
        dates=payload.dates,
        limit=payload.limit,
        entry_id=entry_id,
    )
    return result


@router.get("/lake_table/{entry_id}/partitions")
def table_partitions(entry_id: int) -> dict[str, Any]:
    """查看某张湖表已登记的全部日期分区及其文件来源。"""
    entry = store.find("lake_table", entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"湖表 {entry_id} 不存在")
    items = _pack_service().partitions(table=str(entry.get("表名") or ""))
    return {"table": entry.get("表名"), "total": len(items), "items": items}
