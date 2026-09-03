"""SQL 校验器单元测试（不依赖数据库）

覆盖 P0 安全修复点：
- read_query 的 WITH 子句夹带写操作（旧版漏洞回归测试）
- 多语句注入（分号夹带第二条语句）
- write_query 白名单（EXEC/DROP/TRUNCATE 均被拒绝）
- 注释与字符串字面量不应影响校验
"""
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from server import (  # noqa: E402
    validate_read_query,
    validate_write_query,
    validate_create_table,
    validate_procedure_name,
)


class TestReadQuery:
    """read_query 校验：仅允许单条 SELECT / WITH"""

    def test_select_ok(self):
        validate_read_query("SELECT * FROM orders")

    def test_with_cte_ok(self):
        validate_read_query("WITH c AS (SELECT 1 AS x) SELECT * FROM c")

    def test_with_delete_rejected(self):
        # 旧版漏洞：WITH 前缀绕过 → CTE 夹带 DELETE
        with pytest.raises(ValueError, match="不允许包含写操作"):
            validate_read_query("WITH c AS (SELECT 1 AS x) DELETE FROM orders")

    def test_update_rejected(self):
        with pytest.raises(ValueError):
            validate_read_query("UPDATE orders SET qty = 1")

    def test_insert_rejected(self):
        with pytest.raises(ValueError):
            validate_read_query("INSERT INTO t VALUES (1)")

    def test_multi_statement_rejected(self):
        with pytest.raises(ValueError, match="单条语句"):
            validate_read_query("SELECT 1; DELETE FROM orders")

    def test_exec_rejected(self):
        with pytest.raises(ValueError):
            validate_read_query("EXEC xp_cmdshell 'dir'")

    def test_openrowset_rejected(self):
        with pytest.raises(ValueError):
            validate_read_query("SELECT a.* FROM OPENROWSET('x', 'y') AS a")

    def test_comment_prefix_select_ok(self):
        # 注释开头的 SELECT 不应被前缀判断误杀
        validate_read_query("/* analytics */ SELECT 1 AS n")

    def test_string_literal_with_keyword_ok(self):
        # 字符串字面量中出现 update 字样不应误报
        validate_read_query("SELECT note FROM t WHERE note = 'please update soon'")

    def test_empty_rejected(self):
        with pytest.raises(ValueError):
            validate_read_query("   ")

    def test_drop_in_cte_rejected(self):
        with pytest.raises(ValueError):
            validate_read_query("WITH x AS (SELECT 1) DROP TABLE orders")


class TestWriteQuery:
    """write_query 校验：白名单 INSERT/UPDATE/DELETE/MERGE"""

    def test_insert_ok(self):
        validate_write_query("INSERT INTO t (a) VALUES (1)")

    def test_update_ok(self):
        validate_write_query("UPDATE t SET a = 1 WHERE id = 2")

    def test_delete_ok(self):
        validate_write_query("DELETE FROM t WHERE id = 2")

    def test_merge_ok(self):
        validate_write_query("MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET t.a = s.a")

    def test_select_rejected(self):
        with pytest.raises(ValueError):
            validate_write_query("SELECT * FROM t")

    def test_drop_rejected(self):
        with pytest.raises(ValueError):
            validate_write_query("DROP TABLE t")

    def test_truncate_rejected(self):
        with pytest.raises(ValueError):
            validate_write_query("TRUNCATE TABLE t")

    def test_exec_rejected(self):
        with pytest.raises(ValueError):
            validate_write_query("EXEC sp_help")

    def test_alter_rejected(self):
        with pytest.raises(ValueError):
            validate_write_query("ALTER TABLE t ADD c INT")

    def test_merge_with_exec_rejected(self):
        # MERGE 内嵌 EXEC 语句被拒绝
        with pytest.raises(ValueError):
            validate_write_query("MERGE INTO t USING (EXEC sp) AS s ON 1=1 WHEN NOT MATCHED THEN INSERT VALUES (1)")

    def test_multi_statement_rejected(self):
        with pytest.raises(ValueError):
            validate_write_query("DELETE FROM t; DROP TABLE t")


class TestCreateTable:
    """create_table 校验：仅允许单条 CREATE TABLE"""

    def test_create_ok(self):
        validate_create_table("CREATE TABLE t (id INT PRIMARY KEY, name NVARCHAR(50))")

    def test_drop_rejected(self):
        with pytest.raises(ValueError):
            validate_create_table("DROP TABLE t")

    def test_create_index_rejected(self):
        with pytest.raises(ValueError):
            validate_create_table("CREATE INDEX ix ON t (id)")


class TestProcedureName:
    """存储过程名校验：过程名拼入 {CALL ...}，必须杜绝注入面"""

    def test_plain_name_ok(self):
        assert validate_procedure_name("my_proc") == "my_proc"

    def test_schema_qualified_ok(self):
        assert validate_procedure_name("dbo.my_proc") == "dbo.my_proc"

    def test_three_part_ok(self):
        assert validate_procedure_name("mydb.dbo.my_proc") == "mydb.dbo.my_proc"

    def test_bracketed_ok(self):
        assert validate_procedure_name("dbo.[my proc]") == "dbo.[my proc]"

    def test_whitespace_stripped(self):
        assert validate_procedure_name("  dbo.my_proc  ") == "dbo.my_proc"

    def test_semicolon_injected_rejected(self):
        with pytest.raises(ValueError):
            validate_procedure_name("dbo.sp; DROP TABLE x")

    def test_quote_injected_rejected(self):
        with pytest.raises(ValueError):
            validate_procedure_name("dbo.sp'}; DROP TABLE x; --")

    def test_four_parts_rejected(self):
        with pytest.raises(ValueError):
            validate_procedure_name("a.b.c.d")

    def test_empty_rejected(self):
        with pytest.raises(ValueError):
            validate_procedure_name("   ")

    def test_special_chars_rejected(self):
        for bad in ("dbo.sp-x", "sp()", "dbo.sp name", "[]", "dbo.[]"):
            with pytest.raises(ValueError):
                validate_procedure_name(bad)
