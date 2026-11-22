"""注册分区规则包领域服务。

职责：
1. 接收分区策略文件并逐行解析、校验（目标表 / 日期 / 分区粒度）；
2. 为「目标表 + 日期」维护唯一一条可解释的生效规则；
3. 供湖表注册领取待登记日期，写入湖表分区台账并保留文件与行号来源。

规则解析与校验在 ``partition_file.py``，本文件只负责登记、去重、规则仲裁
和湖表分区落账。
"""
from __future__ import annotations

import datetime
import hashlib
from typing import Any

from app.services.base import BaseModuleService
from app.services.partition_file import parse_and_validate
from app.store import store

PACK_MODULE = "partition_rule_pack"
LINE_MODULE = "partition_rule_line"
PARTITION_MODULE = "lake_partition"

LINE_PENDING = "待登记"
LINE_REGISTERED = "已登记"
LINE_SUPERSEDED = "已失效"
LINE_INVALID = "校验失败"


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _next_id(module: str) -> int:
    rows = store.rows(module)
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


class PartitionRulePackService(BaseModuleService):
    MODULE = PACK_MODULE
    ENTITY = "分区规则包"
    LIST_FIELDS = ["规则包名称", "来源文件", "目标湖表", "覆盖日期数", "接收时间"]
    REQUIRED_FIELDS = []
    STATUS_ORDER = ["已接收", "部分失败", "接收失败", "已登记", "已失效"]
    ACTION_RULES = {}
    NEGATIVE_ACTIONS = []
    CREATE_ERROR = "分区规则包只能通过分区策略文件导入，不支持手工登记"

    # ------------------------------------------------------------------
    # 导入
    # ------------------------------------------------------------------

    def import_file(
        self,
        *,
        file_name: str,
        content: str,
        default_table: str = "",
        remark: str = "",
    ) -> dict[str, Any]:
        """接收一份分区策略文件，逐行校验后登记为规则包。

        同一内容（文件指纹）只登记一次；重复导入返回已有规则包，
        不新增规则行、不重复登记任何分区。
        """
        file_name = (file_name or "").strip()
        if not file_name:
            return self._reject("缺少文件名，无法登记来源")
        if content is None:
            return self._reject("缺少文件内容")

        fingerprint = hashlib.sha256(content.encode("utf-8")).hexdigest()
        existing = next(
            (row for row in store.rows(PACK_MODULE)
             if row.get("文件指纹") == fingerprint),
            None,
        )
        if existing is not None:
            store.log_event("partition_rule_pack_duplicate", PACK_MODULE, PACK_MODULE, {
                "pack_id": existing["id"],
                "file_name": file_name,
                "fingerprint": fingerprint,
            })
            return {
                "ok": True,
                "duplicate": True,
                "message": (
                    f"文件 {file_name} 已于 {existing.get('接收时间')} 导入过"
                    f"（规则包 #{existing['id']}），本次按幂等处理，未重复登记"
                ),
                "pack": existing,
                "lines": self.lines_for_pack(int(existing["id"])),
                "summary": self.pack_summary(int(existing["id"])),
            }

        results, file_errors = parse_and_validate(content, default_table)
        if file_errors:
            return self._reject("文件解析失败：" + "；".join(file_errors))

        pack_id = _next_id(PACK_MODULE)
        valid_tables = sorted({
            row["目标湖表"] for row in results if row["有效"]
        })
        applied_table = valid_tables[0] if len(valid_tables) == 1 else (
            default_table.strip() or ("多表规则包" if valid_tables else "未识别目标表")
        )
        pack = {
            "id": pack_id,
            "status": "已接收",
            "pending": True,
            "abnormal": False,
            "规则包名称": f"规则包-{pack_id:04d}",
            "来源文件": file_name,
            "目标湖表": applied_table,
            "覆盖日期数": len({row["规范化日期"] for row in results if row["有效"]}),
            "接收时间": _now(),
            "文件指纹": fingerprint,
            "文件大小": len(content.encode("utf-8")),
            "数据行数": len(results),
            "有效行数": sum(1 for row in results if row["有效"]),
            "失败行数": sum(1 for row in results if not row["有效"]),
            "覆盖日期列表": sorted({
                row["规范化日期"] for row in results if row["有效"]
            }),
            "备注": remark,
        }
        pack["abnormal"] = pack["失败行数"] > 0
        store.rows(PACK_MODULE).append(pack)

        for row in results:
            line_id = _next_id(LINE_MODULE)
            store.rows(LINE_MODULE).append({
                "id": line_id,
                "status": LINE_INVALID if not row["有效"] else LINE_PENDING,
                "pending": bool(row["有效"]),
                "abnormal": not row["有效"],
                "规则包id": pack_id,
                "来源文件": file_name,
                "文件指纹": fingerprint,
                "行号": row["行号"],
                "目标湖表": row["目标湖表"],
                "日期": row["日期"],
                "规范化日期": row["规范化日期"],
                "分区粒度": row["分区粒度"],
                "规范化粒度": row["规范化粒度"],
                "分区字段": row["分区字段"],
                "分区键": row["分区键"],
                "原始行": row["原始行"],
                "有效": row["有效"],
                "失败原因": row["失败原因"],
                "状态说明": "",
                "登记记录id": None,
                "接收时间": pack["接收时间"],
            })

        self.recompute_rules()
        store.log_event("partition_rule_pack_imported", PACK_MODULE, "lake_table", {
            "pack_id": pack_id,
            "file_name": file_name,
            "fingerprint": fingerprint,
            "table": applied_table,
            "total": pack["数据行数"],
            "valid": pack["有效行数"],
            "invalid": pack["失败行数"],
        })
        return {
            "ok": True,
            "duplicate": False,
            "message": (
                f"已接收 {file_name}：{pack['数据行数']} 行，"
                f"有效 {pack['有效行数']} 行，失败 {pack['失败行数']} 行"
            ),
            "pack": pack,
            "lines": self.lines_for_pack(pack_id),
            "summary": self.pack_summary(pack_id),
        }

    @staticmethod
    def _reject(message: str) -> dict[str, Any]:
        return {
            "ok": False,
            "duplicate": False,
            "message": message,
            "pack": None,
            "lines": [],
            "summary": {},
        }

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """规则包只能由文件导入产生，禁止绕过校验手工登记。"""
        return None, [self.CREATE_ERROR]

    # ------------------------------------------------------------------
    # 规则仲裁：同一表 + 日期只保留一条可解释的生效规则
    # ------------------------------------------------------------------

    def recompute_rules(self) -> None:
        """按「已登记锁定、待登记取最新导入」重算所有规则行状态。"""
        partitions = store.rows(PARTITION_MODULE)
        registered: dict[tuple[str, str], dict[str, Any]] = {}
        for part in partitions:
            registered[(part.get("湖表名", ""), part.get("日期", ""))] = part

        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for line in store.rows(LINE_MODULE):
            if not line.get("有效"):
                line["status"] = LINE_INVALID
                line["pending"] = False
                line["abnormal"] = True
                continue
            key = (line.get("目标湖表", ""), line.get("规范化日期", ""))
            groups.setdefault(key, []).append(line)

        for key, candidates in groups.items():
            # 较晚导入的规则包优先；同一规则包内取行号最小的行。
            candidates.sort(key=lambda row: (-int(row["规则包id"]), int(row["行号"])))
            winner = candidates[0]
            partition = registered.get(key)
            for line in candidates:
                if partition is not None:
                    if int(partition.get("规则行id", -1)) == int(line["id"]):
                        line["status"] = LINE_REGISTERED
                        line["pending"] = False
                        line["abnormal"] = False
                        line["登记记录id"] = partition["id"]
                        line["状态说明"] = (
                            f"已由规则包 #{line['规则包id']} 第 {line['行号']} 行登记"
                            f"，分区键 {line['分区键']}"
                        )
                    else:
                        line["status"] = LINE_SUPERSEDED
                        line["pending"] = False
                        line["abnormal"] = False
                        line["登记记录id"] = None
                        line["状态说明"] = (
                            f"该日期已由 {partition.get('来源文件')} 第 "
                            f"{partition.get('行号')} 行登记，本行不重复登记"
                        )
                elif line is winner:
                    line["status"] = LINE_PENDING
                    line["pending"] = True
                    line["abnormal"] = False
                    line["登记记录id"] = None
                    line["状态说明"] = (
                        f"生效规则：{line['分区键']}（规则包 #{line['规则包id']}；"
                        f"同一表同一日期以较晚导入的规则为准）"
                    )
                else:
                    line["status"] = LINE_SUPERSEDED
                    line["pending"] = False
                    line["abnormal"] = False
                    line["登记记录id"] = None
                    line["状态说明"] = (
                        f"被规则包 #{winner['规则包id']} 第 {winner['行号']} 行"
                        f"（{winner['分区键']}）覆盖"
                    )

        for pack in store.rows(PACK_MODULE):
            lines = self._raw_lines(int(pack["id"]))
            pending = sum(1 for line in lines if line["status"] == LINE_PENDING)
            registered_count = sum(1 for line in lines if line["status"] == LINE_REGISTERED)
            superseded = sum(1 for line in lines if line["status"] == LINE_SUPERSEDED)
            invalid = sum(1 for line in lines if not line.get("有效"))
            valid = len(lines) - invalid
            if valid == 0:
                status = "接收失败"
            elif pending > 0:
                status = "部分失败" if invalid else "已接收"
            elif registered_count > 0:
                status = "已登记"
            elif superseded > 0:
                status = "已失效"
            else:
                status = "已接收"
            pack["status"] = status
            pack["pending"] = status in ("已接收", "部分失败")
            pack["abnormal"] = invalid > 0 or status in ("接收失败", "部分失败", "已失效")
            pack["待登记行数"] = pending
            pack["已登记行数"] = registered_count
            pack["已失效行数"] = superseded
            pack["失败行数"] = invalid
            pack["有效行数"] = valid

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def _raw_lines(self, pack_id: int) -> list[dict[str, Any]]:
        return [
            line for line in store.rows(LINE_MODULE)
            if int(line.get("规则包id", -1)) == pack_id
        ]

    def lines_for_pack(self, pack_id: int) -> list[dict[str, Any]]:
        return sorted(self._raw_lines(pack_id), key=lambda line: int(line["行号"]))

    def all_lines(
        self, *, table: str | None = None, date: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = list(store.rows(LINE_MODULE))
        if table:
            target = table.casefold()
            rows = [line for line in rows
                    if str(line.get("目标湖表", "")).casefold() == target]
        if date:
            rows = [line for line in rows if line.get("规范化日期") == date]
        return sorted(rows, key=lambda line: (int(line["规则包id"]), int(line["行号"])))

    def pack_summary(self, pack_id: int) -> dict[str, Any]:
        lines = self._raw_lines(pack_id)
        return {
            "total": len(lines),
            "valid": sum(1 for line in lines if line.get("有效")),
            "invalid": sum(1 for line in lines if not line.get("有效")),
            "pending": sum(1 for line in lines if line["status"] == LINE_PENDING),
            "registered": sum(1 for line in lines if line["status"] == LINE_REGISTERED),
            "superseded": sum(1 for line in lines if line["status"] == LINE_SUPERSEDED),
        }

    def partitions(
        self, *, table: str | None = None, date: str | None = None,
        pack_id: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = list(store.rows(PARTITION_MODULE))
        if table:
            target = table.casefold()
            rows = [row for row in rows
                    if str(row.get("湖表名", "")).casefold() == target]
        if date:
            rows = [row for row in rows if row.get("日期") == date]
        if pack_id is not None:
            rows = [row for row in rows if int(row.get("规则包id", -1)) == pack_id]
        return sorted(rows, key=lambda row: (row.get("湖表名", ""), row.get("日期", "")))

    def pending_lines(self, table: str, pack_id: int | None = None) -> list[dict[str, Any]]:
        self.recompute_rules()
        target = table.casefold()
        rows = [
            line for line in store.rows(LINE_MODULE)
            if line["status"] == LINE_PENDING
            and str(line.get("目标湖表", "")).casefold() == target
            and (pack_id is None or int(line.get("规则包id", -1)) == pack_id)
        ]
        return sorted(rows, key=lambda line: (line["规范化日期"], int(line["规则包id"])))

    def active_partition_key(self, table: str) -> str:
        """取该表当前生效规则的分区字段，供湖表注册写入分区键。"""
        pending = self.pending_lines(table)
        if pending:
            return str(pending[0]["分区字段"])
        parts = self.partitions(table=table)
        return str(parts[0]["分区字段"]) if parts else ""

    # ------------------------------------------------------------------
    # 湖表注册领取
    # ------------------------------------------------------------------
    def register_partitions(
        self,
        table: str,
        *,
        pack_id: int | None = None,
        dates: list[str] | None = None,
        limit: int | None = None,
        entry_id: int | None = None,
    ) -> dict[str, Any]:
        """把规则包中仍待登记的日期领取到湖表分区台账。

        只能领取当前生效且未登记的规则行；校验失败的行永远不会出现在这里，
        因此不会写入任何湖表记录。
        """
        pending = self.pending_lines(table, pack_id)
        if dates:
            wanted = {str(item).strip() for item in dates if str(item).strip()}
            pending = [line for line in pending if line["规范化日期"] in wanted]
        if limit is not None and limit >= 0:
            pending = pending[:limit]

        if not pending:
            return {
                "ok": True,
                "registered": [],
                "skipped": 0,
                "message": f"湖表 {table} 没有待登记的日期",
            }

        registered: list[dict[str, Any]] = []
        skipped = 0
        for line in pending:
            existing = next(
                (row for row in store.rows(PARTITION_MODULE)
                 if str(row.get("湖表名", "")).casefold() == table.casefold()
                 and row.get("日期") == line["规范化日期"]),
                None,
            )
            if existing is not None:
                skipped += 1
                continue
            pack = store.find(PACK_MODULE, int(line["规则包id"])) or {}
            partition = {
                "id": _next_id(PARTITION_MODULE),
                "status": "已登记",
                "pending": False,
                "abnormal": False,
                "湖表名": line["目标湖表"],
                "日期": line["规范化日期"],
                "分区粒度": line["规范化粒度"],
                "分区字段": line["分区字段"],
                "分区键": line["分区键"],
                "规则包id": line["规则包id"],
                "规则包名称": pack.get("规则包名称", ""),
                "规则行id": line["id"],
                "来源文件": line["来源文件"],
                "文件指纹": line["文件指纹"],
                "行号": line["行号"],
                "登记时间": _now(),
            }
            store.rows(PARTITION_MODULE).append(partition)
            line["status"] = LINE_REGISTERED
            line["pending"] = False
            line["登记记录id"] = partition["id"]
            registered.append(partition)
            store.log_event("lake_partition_registered", PACK_MODULE, "lake_table", {
                "pack_id": line["规则包id"],
                "file_name": line["来源文件"],
                "line_no": line["行号"],
                "table": line["目标湖表"],
                "date": line["规范化日期"],
                "partition_key": line["分区键"],
            })

        self.recompute_rules()
        self._sync_lake_table(table, entry_id=entry_id)
        return {
            "ok": True,
            "registered": registered,
            "skipped": skipped,
            "message": (
                f"湖表 {table} 本次登记 {len(registered)} 个日期分区"
                + (f"，跳过已登记 {skipped} 个" if skipped else "")
            ),
        }

    def _sync_lake_table(self, table: str, *, entry_id: int | None = None) -> None:
        """把已登记分区数、分区键回写到湖表注册记录。"""
        targets = store.find_by_field("lake_table", "表名", table)
        if entry_id is not None:
            entry = store.find("lake_table", entry_id)
            if entry is not None and entry not in targets:
                targets.append(entry)
        if not targets:
            return
        # 规则行里的目标湖表可能大小写不同，台账按 casefold 匹配。
        guards = [row for row in store.rows(PARTITION_MODULE)
                  if str(row.get("湖表名", "")).casefold() == table.casefold()]
        key_field = ""
        for row in sorted(guards, key=lambda item: item.get("日期", "")):
            if row.get("分区字段"):
                key_field = str(row["分区字段"])
        for entry in targets:
            if key_field:
                entry["分区键"] = key_field
            entry["已登记分区数"] = len(guards)
            entry["登记分区日期"] = sorted(row["日期"] for row in guards)

    # ------------------------------------------------------------------
    # 通用动作
    # ------------------------------------------------------------------

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"分区规则包 {entry_id} 不存在"
        if action == "登记分区":
            table = str(entry.get("目标湖表") or "")
            if not table or table == "多表规则包":
                targets = sorted({
                    line["目标湖表"] for line in self._raw_lines(entry_id)
                    if line["status"] == LINE_PENDING
                })
                if not targets:
                    return entry, "该规则包没有待登记的分区日期"
                results = [self.register_partitions(target, pack_id=entry_id)
                           for target in targets]
                total = sum(len(result["registered"]) for result in results)
                self.recompute_rules()
                return entry, f"规则包已登记 {total} 个日期分区"
            result = self.register_partitions(table, pack_id=entry_id)
            self.recompute_rules()
            return entry, str(result["message"])
        return super().run_action(entry_id, action)
