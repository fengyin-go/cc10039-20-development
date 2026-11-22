"""注册分区规则包：文件导入、逐行校验、规则仲裁与湖表分区登记测试。"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.partition_file import (
    normalize_date,
    normalize_granularity,
    parse_and_validate,
)
from app.services.partition_rule_pack import (
    LINE_INVALID,
    LINE_PENDING,
    LINE_REGISTERED,
    LINE_SUPERSEDED,
    PartitionRulePackService,
)
from app.store import store


@pytest.fixture()
def clean_pack_state():
    """规则包相关模块是全局单例，测试前清空以避免互相影响。"""
    modules = (
        "partition_rule_pack", "partition_rule_line", "lake_partition", "lake_table",
    )
    saved = {module: [dict(row) for row in store.rows(module)] for module in modules}
    for module in modules:
        store.rows(module).clear()
    yield
    for module, rows in saved.items():
        store.rows(module)[:] = [dict(row) for row in rows]


@pytest.fixture()
def service():
    return PartitionRulePackService()


FILE_A = (
    "目标表,日期,分区粒度\n"
    "dwd_order,2026-09-01,day\n"
    "dwd_order,2026-09-02,day\n"
    "dwd_order,2026-09-03,day\n"
)
# 与 A 交叠 09-02/09-03，新增 09-04；差异在于来源文件。
FILE_B = (
    "目标表,日期,分区粒度\n"
    "dwd_order,2026-09-02,day\n"
    "dwd_order,2026-09-03,day\n"
    "dwd_order,2026-09-04,day\n"
)


class TestParsing:
    def test_granularity_aliases(self):
        assert normalize_granularity("day") == "day"
        assert normalize_granularity("按天") == "day"
        assert normalize_granularity("1d") == "day"
        assert normalize_granularity("月") == "month"
        assert normalize_granularity("fortnight") is None

    def test_date_must_match_granularity(self):
        assert normalize_date("2026-09-01", "day") == "2026-09-01"
        assert normalize_date("2026-09", "month") == "2026-09"
        assert normalize_date("2026-09-01T08", "hour") == "2026-09-01T08"
        assert normalize_date("2026-09-01", "month") is None
        assert normalize_date("2026-02-30", "day") is None

    def test_csv_row_numbers_are_physical(self):
        text = "表名,日期,粒度\n\ndwd_x,2026-09-01,day\n"
        rows, errors = parse_and_validate(text)
        assert errors == []
        assert [row["行号"] for row in rows] == [3]

    def test_json_lines_keep_element_index(self):
        text = (
            '[{"目标表":"dwd_x","日期":"2026-09-01","分区粒度":"day"},'
            '{"目标表":"dwd_x","日期":"2026-09-02","分区粒度":"month"}]'
        )
        rows, errors = parse_and_validate(text)
        assert errors == []
        assert [row["行号"] for row in rows] == [1, 2]
        assert rows[0]["有效"] is True
        assert rows[1]["有效"] is False
        assert "不匹配" in rows[1]["失败原因"]

    def test_missing_column_reports_reason(self):
        rows, errors = parse_and_validate("表名,日期\ndwd_x,2026-09-01\n")
        assert errors == []
        assert rows[0]["有效"] is False


class TestImport:
    def test_import_records_pack_and_lines(self, service, clean_pack_state):
        result = service.import_file(file_name="a.csv", content=FILE_A)
        assert result["ok"] is True
        assert result["duplicate"] is False
        assert result["pack"]["来源文件"] == "a.csv"
        assert result["pack"]["数据行数"] == 3
        assert result["pack"]["有效行数"] == 3
        assert [line["行号"] for line in result["lines"]] == [2, 3, 4]
        assert all(line["status"] == LINE_PENDING for line in result["lines"])
        assert result["lines"][0]["分区键"] == "dt=2026-09-01"

    def test_duplicate_file_is_idempotent(self, service, clean_pack_state):
        first = service.import_file(file_name="a.csv", content=FILE_A)
        second = service.import_file(file_name="a.csv", content=FILE_A)
        assert second["duplicate"] is True
        assert second["pack"]["id"] == first["pack"]["id"]
        assert len(store.rows("partition_rule_pack")) == 1
        assert len(store.rows("partition_rule_line")) == 3

    def test_same_content_different_name_is_also_duplicate(self, service, clean_pack_state):
        service.import_file(file_name="a.csv", content=FILE_A)
        again = service.import_file(file_name="a-copy.csv", content=FILE_A)
        assert again["duplicate"] is True
        assert len(store.rows("partition_rule_pack")) == 1

    def test_bad_granularity_row_never_registers(self, service, clean_pack_state):
        text = (
            "目标表,日期,分区粒度\n"
            "dwd_order,2026-09-01,day\n"
            "dwd_order,2026-09-02,半周\n"
        )
        result = service.import_file(file_name="bad.csv", content=text)
        invalid = [line for line in result["lines"] if not line["有效"]]
        assert len(invalid) == 1
        assert invalid[0]["status"] == LINE_INVALID
        assert invalid[0]["分区键"] == ""

        outcome = service.register_partitions("dwd_order")
        assert [item["日期"] for item in outcome["registered"]] == ["2026-09-01"]
        assert all(item["日期"] != "2026-09-02" for item in service.partitions())
        assert service.partitions()[0]["行号"] == 2

    def test_empty_file_rejected(self, service, clean_pack_state):
        result = service.import_file(file_name="empty.csv", content="")
        assert result["ok"] is False
        assert store.rows("partition_rule_pack") == []

    def test_manual_create_is_rejected(self, service, clean_pack_state):
        entry, errors = service.create_entry({"规则包名称": "x"})
        assert entry is None
        assert errors


class TestOverlapArbitration:
    def test_later_pack_takes_over_pending_dates(self, service, clean_pack_state):
        first = service.import_file(file_name="a.csv", content=FILE_A)
        # 两次导入之间先登记第一片分区（09-01）。
        slice_result = service.register_partitions("dwd_order", limit=1)
        assert [item["日期"] for item in slice_result["registered"]] == ["2026-09-01"]

        service.import_file(file_name="b.csv", content=FILE_B)

        rules = {line["规范化日期"]: line for line in service.all_lines(table="dwd_order")}
        # 已登记日期锁定到第一份文件，不会被 B 改写。
        assert rules["2026-09-01"]["status"] == LINE_REGISTERED
        assert rules["2026-09-01"]["来源文件"] == "a.csv"
        # 待登记日期由较晚导入的 B 提供唯一生效规则。
        assert rules["2026-09-02"]["status"] == LINE_PENDING
        assert rules["2026-09-02"]["来源文件"] == "b.csv"
        assert rules["2026-09-04"]["来源文件"] == "b.csv"
        # A 的同一日期行被标记失效，且各自有解释。
        a_lines = service.lines_for_pack(first["pack"]["id"])
        superseded = [line for line in a_lines if line["status"] == LINE_SUPERSEDED]
        assert {line["规范化日期"] for line in superseded} == {"2026-09-02", "2026-09-03"}
        assert all("覆盖" in line["状态说明"] for line in superseded)

    def test_registered_date_keeps_original_source_when_new_pack_overlaps(
        self, service, clean_pack_state
    ):
        service.import_file(file_name="a.csv", content=FILE_A)
        service.register_partitions("dwd_order", dates=["2026-09-02"])
        service.import_file(file_name="b.csv", content=FILE_B)

        partitions = {item["日期"]: item for item in service.partitions(table="dwd_order")}
        assert set(partitions) == {"2026-09-02"}
        assert partitions["2026-09-02"]["来源文件"] == "a.csv"
        assert partitions["2026-09-02"]["行号"] == 3
        assert partitions["2026-09-02"]["分区键"] == "dt=2026-09-02"

        line = next(
            line for line in service.all_lines(table="dwd_order", date="2026-09-02")
            if line["来源文件"] == "b.csv"
        )
        assert line["status"] == LINE_SUPERSEDED
        assert "已由 a.csv 第 3 行登记" in line["状态说明"]

    def test_backfill_remaining_dates_then_rewrite_second_file(
        self, service, clean_pack_state
    ):
        """完整场景：导入 A → 登记第一片 → 导入 B → 补录 → 重传 B。"""
        service.import_file(file_name="a.csv", content=FILE_A)
        service.register_partitions("dwd_order", limit=1)

        second = service.import_file(file_name="b.csv", content=FILE_B)
        assert second["pack"]["待登记行数"] == 3

        pending = service.pending_lines("dwd_order")
        assert [line["规范化日期"] for line in pending] == [
            "2026-09-02", "2026-09-03", "2026-09-04",
        ]
        assert {line["来源文件"] for line in pending} == {"b.csv"}

        backfill = service.register_partitions("dwd_order")
        assert [item["日期"] for item in backfill["registered"]] == [
            "2026-09-02", "2026-09-03", "2026-09-04",
        ]

        partitions = {item["日期"]: item for item in service.partitions(table="dwd_order")}
        assert sorted(partitions) == [
            "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04",
        ]
        assert partitions["2026-09-01"]["来源文件"] == "a.csv"
        assert partitions["2026-09-01"]["行号"] == 2
        for date in ("2026-09-02", "2026-09-03", "2026-09-04"):
            assert partitions[date]["来源文件"] == "b.csv"
        assert partitions["2026-09-04"]["行号"] == 4

        # 重传 B：只记一次，不新增规则包/分区。
        before_packs = len(store.rows("partition_rule_pack"))
        before_parts = len(store.rows("lake_partition"))
        replay = service.import_file(file_name="b.csv", content=FILE_B)
        assert replay["duplicate"] is True
        assert len(store.rows("partition_rule_pack")) == before_packs
        assert len(store.rows("lake_partition")) == before_parts
        assert replay["pack"]["已登记行数"] == 3
        assert service.pending_lines("dwd_order") == []

    def test_registration_is_idempotent(self, service, clean_pack_state):
        service.import_file(file_name="a.csv", content=FILE_A)
        first = service.register_partitions("dwd_order")
        second = service.register_partitions("dwd_order")
        assert len(first["registered"]) == 3
        assert second["registered"] == []
        assert len(service.partitions()) == 3

    def test_single_pack_duplicate_dates_deduplicated(self, service, clean_pack_state):
        text = (
            "目标表,日期,分区粒度\n"
            "dwd_order,2026-09-01,day\n"
            "dwd_order,2026/09/01,按天\n"
        )
        result = service.import_file(file_name="dup.csv", content=text)
        rules = service.all_lines(table="dwd_order", date="2026-09-01")
        statuses = sorted(line["status"] for line in rules)
        assert set(statuses) == {LINE_PENDING, LINE_SUPERSEDED}
        outstanding = service.register_partitions("dwd_order")
        assert len(outstanding["registered"]) == 1
        assert outstanding["registered"][0]["行号"] == 2
        assert result["pack"]["覆盖日期数"] == 1


class TestLakeTableIntegration:
    def test_lake_table_registration_claims_first_slice(
        self, service, clean_pack_state
    ):
        from app.services.lake_table import LakeTableService

        store.rows("lake_table").append({
            "id": 991,
            "status": "创建中",
            "pending": True,
            "abnormal": False,
            "表名": "dwd_order",
            "所属业务域": "交易域",
            "存储格式": "parquet",
            "分区键": "",
            "当前分片数": "1",
        })
        service.import_file(file_name="a.csv", content=FILE_A)

        lake = LakeTableService()
        entry, message = lake.register_table(991)
        assert entry is not None, message
        assert entry["status"] == "已注册"
        assert entry["分区键"] == "dt"
        assert entry["已登记分区数"] == 1

        partitions = service.partitions(table="dwd_order")
        assert len(partitions) == 1
        assert partitions[0]["日期"] == "2026-09-01"
        assert partitions[0]["来源文件"] == "a.csv"

    def test_partition_records_trace_to_file_and_line(self, service, clean_pack_state):
        service.import_file(file_name="a.csv", content=FILE_A)
        service.register_partitions("dwd_order", dates=["2026-09-03"])
        record = service.partitions(table="dwd_order")[0]
        assert record["来源文件"] == "a.csv"
        assert record["行号"] == 4
        assert record["规则包名称"].startswith("规则包-")
        assert record["分区键"] == "dt=2026-09-03"
