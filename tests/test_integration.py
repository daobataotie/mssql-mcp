"""集成测试：通过 stdio 启动真实服务器进程，使用 MCP 客户端做端到端验证

测试使用 src/config.json 指向的真实数据库，所有写入仅操作专用的
mcp_upgrade_test 测试表 / 测试存储过程 / 测试视图，结束后自动清理。

配置分支（readonly / max_rows / confirm_writes / 多数据库）通过
MSSQL_MCP_CONFIG 环境变量指向运行时生成的临时配置文件来覆盖验证。
"""
import asyncio
import json
import os
import sys

import pytest
import pyodbc
import mcp.types as mcp_types
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(PROJECT_ROOT, 'src', 'server.py')
CONFIG = os.path.join(PROJECT_ROOT, 'src', 'config.json')
TEST_TABLE = "mcp_upgrade_test"
TEST_PARENT_TABLE = "mcp_upgrade_parent"
TEST_SP = "mcp_test_sp"
TEST_SP_PARAM = "mcp_test_sp_param"
TEST_SP_MULTI = "mcp_test_sp_multi"
TEST_SP_WRITE = "mcp_test_sp_write"
TEST_VIEW = "mcp_upgrade_test_view"


# ==================== 辅助函数 ====================

def _load_config() -> dict:
    with open(CONFIG, 'r', encoding='utf-8') as f:
        return json.load(f)


def _write_temp_config(config: dict) -> str:
    """写入完整配置对象到临时文件，返回路径"""
    path = os.path.join(PROJECT_ROOT, 'tests', '_temp_config.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=4)
    return path


def _primary_db_cfg(cfg: dict) -> dict:
    """取主连接配置：兼容旧版单库（database 键）与新版多库（databases 键）两种格式

    多库格式下取 default_database 指向的连接（缺省时取第一个）。
    """
    if 'database' in cfg:
        return cfg['database']
    databases = cfg['databases']
    name = cfg.get('default_database') or next(iter(databases))
    return databases[name]


def _write_temp_config_db(**overrides) -> str:
    """基于真实配置生成单库临时配置（覆盖 readonly / max_rows 等键）"""
    cfg = _load_config()
    _primary_db_cfg(cfg).update(overrides)
    return _write_temp_config(cfg)


def _write_temp_config_multi() -> str:
    """生成多库临时配置：default（可写）+ readonly_copy（同实例但只读）"""
    cfg = _load_config()
    primary = _primary_db_cfg(cfg)
    return _write_temp_config({
        "databases": {
            "default": {**primary},
            "readonly_copy": {**primary, "readonly": True},
        },
        "server": cfg['server'],
    })


def _direct_connection():
    """直连数据库（测试夹具建表/建SP/清理用，不经过 MCP）"""
    cfg = _primary_db_cfg(_load_config())
    conn_str = (
        f"DRIVER={{{cfg['driver']}}};SERVER={cfg['server']};DATABASE={cfg['database']};"
        f"UID={cfg['username']};PWD={cfg['password']}"
    )
    return pyodbc.connect(conn_str, timeout=10)


def _exec_direct(sql: str):
    """直连执行 DDL（建/删测试表、存储过程、视图）"""
    with _direct_connection() as conn:
        conn.autocommit = True
        conn.cursor().execute(sql)


@pytest.fixture(autouse=True)
def clean_test_objects():
    """每个测试前后清理测试表/存储过程/视图，保证隔离"""
    _cleanup()
    yield
    _cleanup()


def _cleanup():
    _exec_direct(f"IF OBJECT_ID('dbo.{TEST_VIEW}', 'V') IS NOT NULL DROP VIEW dbo.{TEST_VIEW}")
    for sp in (TEST_SP, TEST_SP_PARAM, TEST_SP_MULTI, TEST_SP_WRITE):
        _exec_direct(f"IF OBJECT_ID('dbo.{sp}', 'P') IS NOT NULL DROP PROCEDURE dbo.{sp}")
    for tbl in (TEST_TABLE, TEST_PARENT_TABLE):
        _exec_direct(f"IF OBJECT_ID('dbo.{tbl}', 'U') IS NOT NULL DROP TABLE dbo.{tbl}")


async def _run_session(session_fn, config_path: str | None = None,
                       elicitation_handler=None):
    """启动服务器子进程 → 初始化会话 → 执行会话级回调 → 返回其结果"""
    env = {**os.environ}
    if config_path:
        env['MSSQL_MCP_CONFIG'] = config_path
    params = StdioServerParameters(command=sys.executable, args=[SERVER], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write,
                                 elicitation_callback=elicitation_handler) as session:
            await session.initialize()
            return await session_fn(session)


def run(coro):
    return asyncio.run(coro)


def _call(name: str, arguments: dict, config_path: str | None = None,
          elicitation_handler=None):
    async def fn(session: ClientSession):
        return await session.call_tool(name, arguments)
    return run(_run_session(fn, config_path, elicitation_handler))


def _list_tools(config_path: str | None = None):
    async def fn(session: ClientSession):
        return (await session.list_tools()).tools
    return run(_run_session(fn, config_path))


def _read_resource(uri: str, config_path: str | None = None):
    async def fn(session: ClientSession):
        resource = await session.read_resource(uri)
        return str(resource.contents[0].text)
    return run(_run_session(fn, config_path))


def _read_resource_expect_error(uri: str, config_path: str | None = None):
    async def fn(session: ClientSession):
        with pytest.raises(Exception):
            await session.read_resource(uri)
        return "error-raised"
    return run(_run_session(fn, config_path))


def first_text(result) -> str:
    return result.content[0].text


# ==================== 工具清单与注解 ====================

ALL_TOOLS = {
    'read_query', 'write_query', 'create_table', 'list_tables', 'list_views',
    'describe_table', 'list_databases', 'list_procedures', 'execute_procedure',
    'append_insight',
}


def test_list_tools_and_annotations():
    """10 个工具全部注册，且安全注解正确标注"""
    tools = _list_tools()
    assert {t.name for t in tools} == ALL_TOOLS
    by_name = {t.name: t for t in tools}
    assert by_name['read_query'].annotations.readOnlyHint is True
    assert by_name['write_query'].annotations.destructiveHint is True
    assert by_name['execute_procedure'].annotations.destructiveHint is True
    assert by_name['list_tables'].annotations.readOnlyHint is True
    assert by_name['list_databases'].annotations.readOnlyHint is True


# ==================== 只读查询 ====================

def test_read_query_structured_output():
    """read_query 返回结构化输出（structuredContent）"""
    result = _call('read_query', {'query': 'SELECT 1 AS n'})
    assert not result.isError
    assert result.structuredContent['columns'] == ['n']
    assert result.structuredContent['rows'] == [[1]]
    assert result.structuredContent['truncated'] is False


def test_read_query_with_cte():
    """WITH 子句（上一版本新功能）仍正常工作"""
    result = _call('read_query',
                   {'query': 'WITH c AS (SELECT 1 AS x) SELECT * FROM c'})
    assert not result.isError
    assert result.structuredContent['rows'] == [[1]]


def test_read_query_rejects_with_delete():
    """P0 回归：WITH 夹带 DELETE 的注入语句被拒绝"""
    result = _call('read_query',
                   {'query': f'WITH c AS (SELECT 1 AS x) DELETE FROM dbo.{TEST_TABLE}'})
    assert result.isError
    assert '不允许' in first_text(result)


def test_read_query_rejects_multi_statement():
    """多语句注入被拒绝"""
    result = _call('read_query', {'query': 'SELECT 1; SELECT 2'})
    assert result.isError


# ==================== 写入链路（真实表） ====================

def test_full_write_roundtrip():
    """create_table → write_query(INSERT/UPDATE/DELETE) → read_query 全链路"""
    result = _call('create_table', {
        'query': f'CREATE TABLE dbo.{TEST_TABLE} (id INT PRIMARY KEY, name NVARCHAR(50))'
    })
    assert not result.isError, first_text(result)

    result = _call('write_query', {
        'query': f"INSERT INTO dbo.{TEST_TABLE} (id, name) VALUES (1, N'甲'), (2, N'乙')"
    })
    assert not result.isError, first_text(result)
    assert result.structuredContent['affected_rows'] == 2

    result = _call('read_query', {'query': f'SELECT id, name FROM dbo.{TEST_TABLE} ORDER BY id'})
    assert result.structuredContent['rows'] == [[1, '甲'], [2, '乙']]

    result = _call('write_query', {
        'query': f"UPDATE dbo.{TEST_TABLE} SET name = N'丙' WHERE id = 2"
    })
    assert result.structuredContent['affected_rows'] == 1

    result = _call('write_query', {'query': f'DELETE FROM dbo.{TEST_TABLE} WHERE id = 1'})
    assert result.structuredContent['affected_rows'] == 1

    result = _call('read_query', {'query': f'SELECT COUNT(*) AS cnt FROM dbo.{TEST_TABLE}'})
    assert result.structuredContent['rows'] == [[1]]


def test_write_query_rejects_dangerous_statements():
    """P0 回归：EXEC / DROP / TRUNCATE 均被白名单拒绝"""
    for sql in (
        f"EXEC sp_help 'dbo.{TEST_TABLE}'",
        f'DROP TABLE dbo.{TEST_TABLE}',
        f'TRUNCATE TABLE dbo.{TEST_TABLE}',
    ):
        result = _call('write_query', {'query': sql})
        assert result.isError, f"应当拒绝: {sql}"
        assert '只允许' in first_text(result)


def test_sql_error_reported_as_tool_error():
    """数据库错误以 isError=True 上报（而非伪装成成功文本）"""
    result = _call('read_query', {'query': 'SELECT * FROM dbo.__no_such_table__'})
    assert result.isError


# ==================== 结构浏览 ====================

def test_list_tables_and_describe_table():
    """list_tables 含 schema 列；describe_table 返回复合结构（列/主键/自增/外键/索引）"""
    # 父表 + 子表（外键 + 普通索引），覆盖 describe_table 的全部信息维度
    _call('create_table', {
        'query': f'CREATE TABLE dbo.{TEST_PARENT_TABLE} (id INT PRIMARY KEY)'
    })
    _call('create_table', {
        'query': (f'CREATE TABLE dbo.{TEST_TABLE} ('
                  f'cid INT PRIMARY KEY, '
                  f'pid INT REFERENCES dbo.{TEST_PARENT_TABLE}(id), '
                  f'name NVARCHAR(50) NULL)')
    })
    _exec_direct(f'CREATE INDEX ix_mcp_test_pid ON dbo.{TEST_TABLE}(pid)')

    result = _call('list_tables', {})
    assert not result.isError
    rows = result.structuredContent['rows']
    assert ['dbo', TEST_TABLE] in rows  # schema + 表名
    assert ['dbo', TEST_PARENT_TABLE] in rows

    result = _call('describe_table', {'table_name': TEST_TABLE})
    assert not result.isError, first_text(result)
    sc = result.structuredContent
    assert sc['table'] == f'dbo.{TEST_TABLE}'

    # 列信息：主键 + 可空
    cols = {c['name']: c for c in sc['columns']}
    assert cols['cid']['is_primary_key'] == 1
    assert cols['cid']['nullable'] == 'NO'
    assert cols['name']['nullable'] == 'YES'

    # 外键：pid → mcp_upgrade_parent.id
    fks = sc['foreign_keys']
    assert len(fks) == 1
    assert fks[0]['from_column'] == 'pid'
    assert fks[0]['to_table'] == TEST_PARENT_TABLE
    assert fks[0]['to_column'] == 'id'

    # 索引：主键索引（聚集）+ 普通索引 ix_mcp_test_pid
    indexes = {i['name']: i for i in sc['indexes']}
    assert indexes['ix_mcp_test_pid']['columns'] == ['pid']
    assert indexes['ix_mcp_test_pid']['is_primary_key'] in (0, False)
    pk_indexes = [i for i in sc['indexes'] if i['is_primary_key']]
    assert len(pk_indexes) == 1
    assert pk_indexes[0]['columns'] == ['cid']


def test_describe_table_not_found():
    """不存在的表给出明确错误"""
    result = _call('describe_table', {'table_name': '__no_such_table__'})
    assert result.isError
    assert '未找到表' in first_text(result)


def test_list_views():
    """list_views 返回视图清单，且视图不出现在 list_tables 中"""
    _exec_direct(f'CREATE VIEW dbo.{TEST_VIEW} AS SELECT 1 AS one')

    result = _call('list_views', {})
    assert not result.isError
    assert ['dbo', TEST_VIEW] in result.structuredContent['rows']

    tables = _call('list_tables', {}).structuredContent['rows']
    assert ['dbo', TEST_VIEW] not in tables


# ==================== 存储过程 ====================

def test_list_procedures():
    """list_procedures 返回过程名与参数签名"""
    _exec_direct(
        f"CREATE PROCEDURE dbo.{TEST_SP_PARAM} @x INT, @y NVARCHAR(20) AS SELECT @x AS x, @y AS y"
    )
    result = _call('list_procedures', {})
    assert not result.isError
    rows = result.structuredContent['rows']
    proc_rows = [r for r in rows if r[0] == TEST_SP_PARAM]
    # 两个参数各占一行：参数名 / 类型 / 方向
    assert len(proc_rows) == 2
    assert [r[1] for r in proc_rows] == ['@x', '@y']
    assert proc_rows[0][2] == 'int'


def test_execute_procedure_basic():
    """无参存储过程：单个结果集正常返回"""
    _exec_direct(f"CREATE PROCEDURE dbo.{TEST_SP} AS SELECT 1 AS n")
    result = _call('execute_procedure', {'procedure': f'dbo.{TEST_SP}'})
    assert not result.isError, first_text(result)
    sc = result.structuredContent
    assert sc['procedure'] == f'dbo.{TEST_SP}'
    assert len(sc['result_sets']) == 1
    assert sc['result_sets'][0]['rows'] == [[1]]


def test_execute_procedure_with_parameters():
    """带参存储过程：位置参数正确绑定"""
    _exec_direct(
        f"CREATE PROCEDURE dbo.{TEST_SP_PARAM} @x INT, @y NVARCHAR(20) AS SELECT @x AS x, @y AS y"
    )
    result = _call('execute_procedure', {
        'procedure': f'dbo.{TEST_SP_PARAM}',
        'parameters': [5, 'hi'],
    })
    assert not result.isError, first_text(result)
    rs = result.structuredContent['result_sets'][0]
    assert rs['columns'] == ['x', 'y']
    assert rs['rows'] == [[5, 'hi']]


def test_execute_procedure_multiple_result_sets():
    """多结果集存储过程：全部收割"""
    _exec_direct(
        f"CREATE PROCEDURE dbo.{TEST_SP_MULTI} AS SELECT 1 AS a; SELECT 2 AS b"
    )
    result = _call('execute_procedure', {'procedure': f'dbo.{TEST_SP_MULTI}'})
    assert not result.isError, first_text(result)
    sets = result.structuredContent['result_sets']
    assert len(sets) == 2
    assert sets[0]['rows'] == [[1]]
    assert sets[1]['rows'] == [[2]]


def test_execute_procedure_writes_data():
    """写库存储过程：事务正确提交，数据落地可见"""
    _exec_direct(f'CREATE TABLE dbo.{TEST_TABLE} (id INT, val NVARCHAR(20))')
    _exec_direct(
        f"CREATE PROCEDURE dbo.{TEST_SP_WRITE} @id INT, @val NVARCHAR(20) "
        f"AS INSERT INTO dbo.{TEST_TABLE} (id, val) VALUES (@id, @val); "
        f"SELECT COUNT(*) AS cnt FROM dbo.{TEST_TABLE}"
    )
    result = _call('execute_procedure', {
        'procedure': f'dbo.{TEST_SP_WRITE}',
        'parameters': [7, '落地'],
    })
    assert not result.isError, first_text(result)
    # SP 内的 SELECT 返回结果集
    assert result.structuredContent['result_sets'][0]['rows'] == [[1]]
    # 数据确实已提交
    check = _call('read_query', {'query': f'SELECT id, val FROM dbo.{TEST_TABLE}'})
    assert check.structuredContent['rows'] == [[7, '落地']]


def test_execute_procedure_rejects_injected_name():
    """过程名注入被拒绝（分号 / 引号 / 特殊字符）"""
    for bad in ("dbo.sp; DROP TABLE x", "sp'}; --", "a.b.c.d"):
        result = _call('execute_procedure', {'procedure': bad})
        assert result.isError, f"应当拒绝: {bad}"


def test_execute_procedure_blocked_in_readonly():
    """readonly 连接上存储过程被禁用（黑盒可能写库）"""
    cfg_path = _write_temp_config_db(readonly=True)
    try:
        result = _call('execute_procedure', {'procedure': 'dbo.sp_help'},
                       config_path=cfg_path)
        assert result.isError
        assert '只读模式' in first_text(result)
    finally:
        os.unlink(cfg_path)


# ==================== 多数据库 ====================

def test_list_databases_and_multi_db_routing():
    """多库配置：list_databases 列出连接，工具按 database 参数路由"""
    cfg_path = _write_temp_config_multi()
    try:
        # 连接清单
        result = _call('list_databases', {}, config_path=cfg_path)
        assert not result.isError
        dbs = {d['name']: d for d in result.structuredContent['databases']}
        assert set(dbs) == {'default', 'readonly_copy'}
        assert dbs['default']['is_default'] is True
        assert dbs['default']['readonly'] is False
        assert dbs['readonly_copy']['readonly'] is True

        # 两个连接都能查询（同一实例）
        for db_name in ('default', 'readonly_copy'):
            result = _call('read_query', {'query': 'SELECT 1 AS n', 'database': db_name},
                           config_path=cfg_path)
            assert not result.isError, first_text(result)
            assert result.structuredContent['rows'] == [[1]]

        # 可写连接写操作正常
        result = _call('create_table', {
            'query': f'CREATE TABLE dbo.{TEST_TABLE} (id INT)',
            'database': 'default',
        }, config_path=cfg_path)
        assert not result.isError, first_text(result)

        # 只读连接拒绝写操作
        result = _call('write_query', {
            'query': f'INSERT INTO dbo.{TEST_TABLE} (id) VALUES (1)',
            'database': 'readonly_copy',
        }, config_path=cfg_path)
        assert result.isError
        assert '只读模式' in first_text(result)

        # 未知连接名报错并列出可用连接
        result = _call('read_query', {'query': 'SELECT 1', 'database': 'bogus'},
                       config_path=cfg_path)
        assert result.isError
        assert 'default' in first_text(result) and 'readonly_copy' in first_text(result)
    finally:
        os.unlink(cfg_path)


def test_single_db_config_backward_compatible():
    """旧版单库配置：自动包装为 default 连接，database 参数可显式传 "default" """
    # 从真实配置提取主连接，拼成旧版单库格式（database 键）的临时配置
    cfg = _load_config()
    cfg_path = _write_temp_config({
        'database': {**_primary_db_cfg(cfg)},
        'server': cfg['server'],
    })
    try:
        result = _call('read_query', {'query': 'SELECT 1 AS n', 'database': 'default'},
                       config_path=cfg_path)
        assert not result.isError, first_text(result)
        assert result.structuredContent['rows'] == [[1]]
        # 不传 database 参数同样走默认连接
        result = _call('read_query', {'query': 'SELECT 2 AS n'}, config_path=cfg_path)
        assert not result.isError, first_text(result)
        assert result.structuredContent['rows'] == [[2]]
    finally:
        os.unlink(cfg_path)


# ==================== 资源模板 ====================

def test_resource_template_default_db():
    """schema://dbo/{table} 资源模板：默认库表结构按需拉取"""
    _exec_direct(f'CREATE TABLE dbo.{TEST_TABLE} (id INT PRIMARY KEY, name NVARCHAR(50) NULL)')
    content = _read_resource(f'schema://dbo/{TEST_TABLE}')
    data = json.loads(content)
    assert data['table'] == f'dbo.{TEST_TABLE}'
    cols = {c['name']: c for c in data['columns']}
    assert cols['id']['is_primary_key'] == 1
    assert cols['name']['nullable'] == 'YES'


def test_resource_template_named_db():
    """schema://{database}/dbo/{table} 资源模板：显式指定数据库连接"""
    _exec_direct(f'CREATE TABLE dbo.{TEST_TABLE} (id INT PRIMARY KEY)')
    # 连接名取自真实配置的默认连接（多库格式下不再固定叫 "default"）
    cfg = _load_config()
    conn_name = cfg.get('default_database') if 'databases' in cfg else 'default'
    content = _read_resource(f'schema://{conn_name}/dbo/{TEST_TABLE}')
    data = json.loads(content)
    assert data['database'] == conn_name
    assert data['columns'][0]['name'] == 'id'


def test_resource_template_unknown_table_errors():
    """不存在的表资源读取报错"""
    _read_resource_expect_error('schema://dbo/__no_such_table__')


def test_resource_template_unknown_db_errors():
    """不存在的数据库连接资源读取报错"""
    _read_resource_expect_error('schema://bogus/dbo/some_table')


# ==================== 安全护栏配置 ====================

def test_readonly_mode_blocks_writes():
    """readonly=true 时：读操作正常，写操作被拒绝"""
    cfg_path = _write_temp_config_db(readonly=True)
    try:
        result = _call('read_query', {'query': 'SELECT 1 AS n'}, config_path=cfg_path)
        assert not result.isError

        result = _call('write_query',
                       {'query': f'INSERT INTO dbo.{TEST_TABLE} (id) VALUES (1)'},
                       config_path=cfg_path)
        assert result.isError
        assert '只读模式' in first_text(result)

        result = _call('create_table',
                       {'query': f'CREATE TABLE dbo.{TEST_TABLE} (id INT)'},
                       config_path=cfg_path)
        assert result.isError
        assert '只读模式' in first_text(result)
    finally:
        os.unlink(cfg_path)


def test_max_rows_truncation():
    """max_rows 限制：结果超限时截断并标记 truncated"""
    cfg_path = _write_temp_config_db(max_rows=3)
    try:
        _call('create_table', {'query': f'CREATE TABLE dbo.{TEST_TABLE} (id INT)'})
        _call('write_query', {
            'query': f'INSERT INTO dbo.{TEST_TABLE} VALUES (1),(2),(3),(4),(5)'
        })
        result = _call('read_query',
                       {'query': f'SELECT id FROM dbo.{TEST_TABLE} ORDER BY id'},
                       config_path=cfg_path)
        sc = result.structuredContent
        assert sc['row_count'] == 3
        assert sc['rows'] == [[1], [2], [3]]
        assert sc['truncated'] is True
    finally:
        os.unlink(cfg_path)


# ==================== Elicitation 写确认 ====================

def _elicit_handler(action: str, confirm: bool = True):
    """构造客户端 elicitation 回调：模拟用户接受/拒绝确认表单"""
    async def handler(context, params):
        if action == "accept":
            return mcp_types.ElicitResult(action="accept", content={"confirm": confirm})
        return mcp_types.ElicitResult(action=action)
    return handler


def test_confirm_writes_accept():
    """confirm_writes=true 且用户确认：写操作正常执行"""
    cfg_path = _write_temp_config_db(confirm_writes=True)
    try:
        _call('create_table', {'query': f'CREATE TABLE dbo.{TEST_TABLE} (id INT)'},
              config_path=cfg_path)
        result = _call('write_query',
                       {'query': f'INSERT INTO dbo.{TEST_TABLE} VALUES (1)'},
                       config_path=cfg_path,
                       elicitation_handler=_elicit_handler("accept", confirm=True))
        assert not result.isError, first_text(result)
        assert result.structuredContent['affected_rows'] == 1
    finally:
        os.unlink(cfg_path)


def test_confirm_writes_decline():
    """confirm_writes=true 且用户拒绝：写操作被取消"""
    cfg_path = _write_temp_config_db(confirm_writes=True)
    try:
        _call('create_table', {'query': f'CREATE TABLE dbo.{TEST_TABLE} (id INT)'},
              config_path=cfg_path,
              elicitation_handler=_elicit_handler("accept", confirm=True))
        result = _call('write_query',
                       {'query': f'INSERT INTO dbo.{TEST_TABLE} VALUES (1)'},
                       config_path=cfg_path,
                       elicitation_handler=_elicit_handler("decline"))
        assert result.isError
        assert '未确认' in first_text(result)
        # 数据未落地
        check = _call('read_query', {'query': f'SELECT COUNT(*) AS c FROM dbo.{TEST_TABLE}'},
                      config_path=cfg_path)
        assert check.structuredContent['rows'] == [[0]]
    finally:
        os.unlink(cfg_path)


def test_confirm_writes_accept_but_unchecked():
    """用户提交表单但未勾选确认：写操作被取消"""
    cfg_path = _write_temp_config_db(confirm_writes=True)
    try:
        _call('create_table', {'query': f'CREATE TABLE dbo.{TEST_TABLE} (id INT)'})
        result = _call('write_query',
                       {'query': f'INSERT INTO dbo.{TEST_TABLE} VALUES (1)'},
                       config_path=cfg_path,
                       elicitation_handler=_elicit_handler("accept", confirm=False))
        assert result.isError
        assert '未确认' in first_text(result)
    finally:
        os.unlink(cfg_path)


def test_confirm_writes_fallback_without_client_support():
    """confirm_writes=true 但客户端不支持 elicitation：优雅回退为直接执行"""
    cfg_path = _write_temp_config_db(confirm_writes=True)
    try:
        _call('create_table', {'query': f'CREATE TABLE dbo.{TEST_TABLE} (id INT)'})
        # 不注册 elicitation_handler，模拟 Cursor 等不支持确认的客户端
        result = _call('write_query',
                       {'query': f'INSERT INTO dbo.{TEST_TABLE} VALUES (1)'},
                       config_path=cfg_path)
        assert not result.isError, first_text(result)
        assert result.structuredContent['affected_rows'] == 1
    finally:
        os.unlink(cfg_path)


def test_confirm_writes_applies_to_procedure():
    """confirm_writes 同样覆盖存储过程：拒绝时取消"""
    cfg_path = _write_temp_config_db(confirm_writes=True)
    try:
        _exec_direct(f"CREATE PROCEDURE dbo.{TEST_SP} AS SELECT 1 AS n")
        result = _call('execute_procedure', {'procedure': f'dbo.{TEST_SP}'},
                       config_path=cfg_path,
                       elicitation_handler=_elicit_handler("decline"))
        assert result.isError
        assert '未确认' in first_text(result)
    finally:
        os.unlink(cfg_path)


def test_confirm_writes_applies_to_create_table():
    """confirm_writes 同样覆盖建表（DDL）：拒绝时取消且表未创建"""
    cfg_path = _write_temp_config_db(confirm_writes=True)
    try:
        result = _call('create_table',
                       {'query': f'CREATE TABLE dbo.{TEST_TABLE} (id INT)'},
                       config_path=cfg_path,
                       elicitation_handler=_elicit_handler("decline"))
        assert result.isError
        assert '未确认' in first_text(result)
        # 表未落地（直连确认对象不存在）
        with _direct_connection() as conn:
            exists = conn.cursor().execute(
                f"SELECT OBJECT_ID('dbo.{TEST_TABLE}', 'U')").fetchone()[0]
        assert exists is None
    finally:
        os.unlink(cfg_path)


# ==================== 资源与提示模板 ====================

def test_memo_resource_updates():
    """append_insight 后 memo://insights 资源实时更新"""
    async def fn(session: ClientSession):
        result = await session.call_tool(
            'append_insight', {'insight': '华东区销售额同比增长 12%'}
        )
        assert not result.isError
        resource = await session.read_resource('memo://insights')
        return str(resource.contents[0].text)

    content = run(_run_session(fn))
    assert '华东区销售额同比增长 12%' in content


def test_prompt_template():
    """mcp-demo 提示模板按主题渲染"""
    async def fn(session: ClientSession):
        return await session.get_prompt('mcp-demo', {'topic': 'retail sales'})

    result = run(_run_session(fn))
    text = result.messages[0].content.text
    assert 'retail sales' in text
