"""Config 配置解析单元测试（不依赖数据库连接）

覆盖多数据库配置（P3）的解析逻辑：
- 旧版单库 database 节 → 自动包装为 "default" 连接（向后兼容）
- 多库 databases 字典 + default_database 显式指定
- 配置错误场景：缺少必备键、空 databases、default_database 指向不存在的连接
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from server import Config  # noqa: E402

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))


def _make_config(config: dict) -> str:
    """写入临时配置文件并返回路径（通过 MSSQL_MCP_CONFIG 环境变量加载）"""
    path = os.path.join(TESTS_DIR, '_unit_config.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False)
    return path


@pytest.fixture
def via_env(monkeypatch):
    """返回一个把配置文件挂到环境变量的工厂"""
    def _load(config: dict) -> Config:
        path = _make_config(config)
        monkeypatch.setenv('MSSQL_MCP_CONFIG', path)
        return Config()
    return _load


VALID_DB = {
    "driver": "ODBC Driver 17 for SQL Server",
    "server": "localhost",
    "database": "db1",
    "username": "sa",
    "password": "pwd",
}


class TestLegacySingleDatabase:
    """旧版单库格式完全向后兼容"""

    def test_wrapped_as_default(self, via_env):
        cfg = via_env({
            "database": {**VALID_DB},
            "server": {"name": "mssql-manager", "version": "0.2.0"},
        })
        assert list(cfg.databases) == ["default"]
        assert cfg.default_database == "default"
        assert cfg.db_config("default")["server"] == "localhost"

    def test_missing_both_sections_rejected(self, via_env):
        with pytest.raises(ValueError, match="缺少 database 或 databases"):
            via_env({"server": {"name": "x", "version": "0"}})


class TestMultiDatabase:
    """多库格式解析与默认连接解析"""

    def test_multiple_databases(self, via_env):
        cfg = via_env({
            "databases": {
                "dev": {**VALID_DB, "database": "dev_db"},
                "prod": {**VALID_DB, "database": "prod_db", "readonly": True},
            },
            "server": {"name": "mssql-manager", "version": "0.2.0"},
        })
        assert set(cfg.databases) == {"dev", "prod"}
        # 无 "default" 键且未显式指定时，取字典第一个连接作为默认
        assert cfg.default_database == "dev"
        assert cfg.db_config("prod")["readonly"] is True

    def test_explicit_default_database(self, via_env):
        cfg = via_env({
            "databases": {
                "dev": {**VALID_DB},
                "prod": {**VALID_DB},
            },
            "default_database": "prod",
            "server": {"name": "x", "version": "0"},
        })
        assert cfg.default_database == "prod"

    def test_default_database_unknown_rejected(self, via_env):
        with pytest.raises(ValueError, match="default_database"):
            via_env({
                "databases": {"dev": {**VALID_DB}},
                "default_database": "staging",
                "server": {"name": "x", "version": "0"},
            })

    def test_unknown_name_lists_available(self, via_env):
        cfg = via_env({
            "databases": {"dev": {**VALID_DB}, "prod": {**VALID_DB}},
            "server": {"name": "x", "version": "0"},
        })
        with pytest.raises(ValueError, match="dev, prod"):
            cfg.db_config("staging")

    def test_empty_databases_rejected(self, via_env):
        with pytest.raises(ValueError, match="非空"):
            via_env({
                "databases": {},
                "server": {"name": "x", "version": "0"},
            })

    def test_missing_required_keys_rejected(self, via_env):
        with pytest.raises(ValueError, match="server"):
            via_env({
                "databases": {"dev": {"driver": "x", "database": "db"}},
                "server": {"name": "x", "version": "0"},
            })


class TestConfigFileErrors:
    """配置文件本身的错误：缺失 / 非法 JSON"""

    def test_missing_file_clear_message(self, monkeypatch):
        """环境变量指向不存在的文件：报错列出查找顺序，便于排查"""
        monkeypatch.setenv('MSSQL_MCP_CONFIG',
                           os.path.join(TESTS_DIR, '_no_such_config.json'))
        with pytest.raises(FileNotFoundError, match="查找顺序"):
            Config()

    def test_malformed_json_clear_message(self, monkeypatch):
        """非法 JSON：报错定位到行列号"""
        path = os.path.join(TESTS_DIR, '_unit_config.json')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('{ "database": ')  # 截断的 JSON
        monkeypatch.setenv('MSSQL_MCP_CONFIG', path)
        with pytest.raises(ValueError, match="合法的 JSON"):
            Config()
