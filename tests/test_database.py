"""MssqlDatabase 连接级行为测试（直连真实数据库，不经过 MCP 子进程）

覆盖错误处理与连接恢复策略：
- 语法错误：连接对象保留（不销毁重建），连接后续仍可用
- 连接被外部关闭：自动重连恢复
- 查询超时：不重试（耗时约等于单倍超时），连接后续仍可用
- 存储过程参数标量校验
"""
import os
import sys
import time

import pyodbc
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'src'))

from server import Config, MssqlDatabase  # noqa: E402


@pytest.fixture
def db() -> MssqlDatabase:
    """基于真实配置建立直连数据库实例（配置的默认连接）"""
    config = Config()
    name = config.default_database
    return MssqlDatabase(name, config.db_config(name))


class TestConnectionResilience:
    """连接级错误处理策略（重试判据 _is_connection_error）"""

    def test_syntax_error_preserves_connection(self, db: MssqlDatabase):
        """语法错误（SQLSTATE 42xxx）不应销毁健康连接"""
        db.execute_read("SELECT 1 AS n")  # 建立连接
        conn_id_before = id(db._conn)

        with pytest.raises(pyodbc.Error):
            db.execute_read("SELECT FROM WHERE")

        # 连接对象未重建，且后续查询仍然可用
        assert id(db._conn) == conn_id_before
        assert db.execute_read("SELECT 2 AS n")["rows"] == [[2]]

    def test_closed_connection_auto_recovers(self, db: MssqlDatabase):
        """连接被外部关闭后：下一次查询自动重连恢复"""
        db.execute_read("SELECT 1 AS n")  # 建立连接
        db._conn.close()  # 模拟连接被关闭（如对端重启、连接池回收）

        result = db.execute_read("SELECT 3 AS n")  # 应自动重连并成功
        assert result["rows"] == [[3]]

    def test_timeout_not_retried(self, db: MssqlDatabase):
        """查询超时（HYT00）不重试：总耗时约等于单倍超时，而非双倍"""
        # 用独立实例设置 1 秒超时，避免污染其他测试的连接配置
        config = Config()
        name = config.default_database
        slow = MssqlDatabase('timeout_test',
                             {**config.db_config(name), 'query_timeout': 1})
        t0 = time.monotonic()
        with pytest.raises(pyodbc.Error):
            slow.execute_read("WAITFOR DELAY '00:00:05'")
        elapsed = time.monotonic() - t0
        # 超时 1s；若发生重试会达到 2s 以上。留 0.8s 余量防抖动
        assert elapsed < 1.8, f"超时后疑似发生了重试（耗时 {elapsed:.1f}s）"
        # 超时后连接仍可用于正常查询
        assert slow.execute_read("SELECT 4 AS n")["rows"] == [[4]]


class TestProcedureParameterValidation:
    """存储过程参数仅允许标量（pyodbc 无法绑定 JSON 对象/数组）"""

    @pytest.mark.parametrize("bad_param", [
        {"x": 1},           # JSON 对象
        [1, 2],             # JSON 数组
        {"a": [1]},         # 嵌套结构
    ])
    def test_non_scalar_rejected(self, db: MssqlDatabase, bad_param):
        with pytest.raises(ValueError, match="标量"):
            db.execute_procedure("dbo.some_proc", [bad_param])

    def test_scalar_accepted(self, db: MssqlDatabase):
        """标量参数（含 None/bool）通过校验后才尝试执行（过程不存在报数据库错误）"""
        with pytest.raises(pyodbc.Error):
            db.execute_procedure("dbo.__no_such_proc__", [1, "a", None, True])
