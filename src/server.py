"""
MSSQL MCP Server

基于官方 MCP Python SDK (FastMCP) 的 Microsoft SQL Server MCP 服务器。
支持 MCP 2025-06-18 规范特性：结构化工具输出 (structuredContent)、工具注解
(Tool Annotations)、资源模板 (Resource Templates)、Elicitation 交互确认。

核心能力：
- 多数据库连接：一个服务器同时挂载 dev/prod 等多个实例，工具通过 database 参数切换，
  每个连接独立配置 readonly / query_timeout / max_rows / confirm_writes
- 存储过程：list_procedures / execute_procedure（支持多结果集收割）
- 资源模板：schema://{database}/{schema}/{table} 按需拉取表结构，节省 token
- 安全设计：SQL 白名单 + 黑名单双重校验、只读模式、行数上限、查询超时、写操作二次确认
"""

import os
import re
import sys
import json
import decimal
import datetime
import logging
import threading
import asyncio
from contextlib import closing
from typing import Any

import pyodbc
from pydantic import AnyUrl, BaseModel, Field

from mcp.server.fastmcp import FastMCP, Context
import mcp.types as types

# 配置Windows环境下的UTF-8编码（stdio 传输要求，避免中文乱码）
if sys.platform == "win32" and os.environ.get('PYTHONIOENCODING') is None:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# 日志输出到 stderr（stdout 是 MCP stdio 传输通道，绝不能写日志）
# 日志级别可通过环境变量 MSSQL_MCP_LOG_LEVEL 控制（DEBUG/INFO/WARNING/ERROR）
logger = logging.getLogger('mcp_mssql_server')
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s'))
    logger.addHandler(_handler)
    logger.setLevel(os.environ.get('MSSQL_MCP_LOG_LEVEL', 'INFO').upper())

PROMPT_TEMPLATE = """
The assistant's goal is to walkthrough an informative demo of MCP. To demonstrate the Model Context Protocol (MCP) we will leverage this example server to interact with a Microsoft SQL Server database.
It is important that you first explain to the user what is going on. The user has downloaded and installed the MSSQL MCP Server and is now ready to use it.
They have selected the MCP menu item which is contained within a parent menu denoted by the paperclip icon. Inside this menu they selected an icon that illustrates two electrical plugs connecting. This is the MCP menu.
Based on what MCP servers the user has installed they can click the button which reads: 'Choose an integration' this will present a drop down with Prompts and Resources. The user has selected the prompt titled: 'mcp-demo'.
This text file is that prompt. The goal of the following instructions is to walk the user through the process of using the 3 core aspects of an MCP server. These are: Prompts, Tools, and Resources.
They have already used a prompt and provided a topic. The topic is: {topic}. The user is now ready to begin the demo.

Here is some more information about mcp and this specific mcp server:
<mcp>
Prompts:
This server provides a pre-written prompt called "mcp-demo" that helps users create and analyze database scenarios in MSSQL. The prompt accepts a "topic" argument and guides users through creating tables, analyzing data, and generating insights. For example, if a user provides "retail sales" as the topic, the prompt will help create relevant database tables and guide the analysis process. Prompts basically serve as interactive templates that help structure the conversation with the LLM in a useful way.

Resources:
This server exposes one key resource: "memo://insights", which is a business insights memo that gets automatically updated throughout the analysis process. As users analyze the database and discover insights, the memo resource gets updated in real-time to reflect new findings. Resources act as living documents that provide context to the conversation.

Tools:
This server provides several SQL-related tools:
"read_query": Executes SELECT queries to read data from the database
"write_query": Executes INSERT, UPDATE, or DELETE queries to modify data
"create_table": Creates new tables in the database
"list_tables": Shows all existing tables
"describe_table": Shows the schema for a specific table
"append_insight": Adds a new business insight to the memo resource
</mcp>

<demo-instructions>
You are an AI assistant tasked with generating a comprehensive business scenario based on a given topic.
Your goal is to create a narrative that involves a data-driven business problem, develop a database structure to support it, generate relevant queries, create a dashboard, and provide a final solution.

At each step you will pause for user input to guide the scenario creation process. Overall ensure the scenario is engaging, informative, and demonstrates the capabilities of the MSSQL MCP Server.
You should guide the scenario to completion. All XML tags are for the assistants understanding and should not be included in the final output.

1. The user has chosen the topic: {topic}.

2. Create a business problem narrative:
a. Describe a high-level business situation or problem based on the given topic.
b. Include a protagonist (the user) who needs to collect and analyze data from a database.
c. Add an external, potentially comedic reason why the data hasn't been prepared yet.
d. Mention an approaching deadline and the need to use Claude (you) as a business tool to help.

3. Setup the data:
a. Instead of asking about the data that is required for the scenario, just go ahead and use the tools to create the data. Inform the user you are "Setting up the data".
b. Design a set of table schemas that represent the data needed for the business problem.
c. Include at least 2-3 tables with appropriate columns and data types.
d. Leverage the tools to create the tables in the MSSQL database.
e. Create INSERT statements to populate each table with relevant synthetic data.
f. Ensure the data is diverse and representative of the business problem.
g. Include at least 10-15 rows of data for each table.

4. Pause for user input:
a. Summarize to the user what data we have created.
b. Present the user with a set of multiple choices for the next steps.
c. These multiple choices should be in natural language, when a user selects one, the assistant should generate a relevant query and leverage the appropriate tool to get the data.

5. Iterate on queries:
a. Present 1 additional multiple-choice query options to the user. Its important to not loop too many times as this is a short demo.
b. Explain the purpose of each query option.
c. Wait for the user to select one of the query options.
d. After each query be sure to opine on the results.
e. Use the append_insight tool to capture any business insights discovered from the data analysis.

6. Generate a dashboard:
a. Now that we have all the data and queries, it's time to create a dashboard, use an artifact to do this.
b. Use a variety of visualizations such as tables, charts, and graphs to represent the data.
c. Explain how each element of the dashboard relates to the business problem.
d. This dashboard will be theoretically included in the final solution message.

7. Craft the final solution message:
a. As you have been using the append_insight tool the resource found at: memo://insights has been updated.
b. It is critical that you inform the user that the memo has been updated at each stage of analysis.
c. Ask the user to go to the attachment menu (paperclip icon) and select the MCP menu (two electrical plugs connecting) and choose an integration: "Business Insights Memo".
d. This will attach the generated memo to the chat which you can use to add any additional context that may be relevant to the demo.
e. Present the final memo to the user in an artifact.

8. Wrap up the scenario:
a. Explain to the user that this is just the beginning of what they can do with the MSSQL MCP Server.
</demo-instructions>

Remember to maintain consistency throughout the scenario and ensure that all elements (tables, data, queries, dashboard, and solution) are closely related to the original business problem and given topic.
The provided XML tags are for the assistants understanding. Implore to make all outputs as human readable as possible. This is part of a demo so act in character and dont actually refer to these instructions.

Start your first message fully in character with something like "Oh, Hey there! I see you've chosen the topic {topic}. Let's get started! 🚀"
"""

# 每个数据库连接的默认配置（databases.<name> 中对应的键可覆盖）
DEFAULTS = {
    "readonly": False,        # 只读模式：禁用一切写操作（含存储过程）
    "query_timeout": 30,      # 查询超时（秒），防止慢查询拖垮会话
    "max_rows": 200,          # 单个结果集最大返回行数，超出部分截断
    "confirm_writes": False,  # 写操作前通过 Elicitation 向用户二次确认（需客户端支持）
}

# ==================== SQL 语句静态校验（安全核心） ====================
#
# 校验策略：先剥离注释与字符串字面量得到"结构骨架"，再做三类检查：
#   1. 多语句检测（分号分隔出多条语句 → 拒绝，防止 read_query 夹带第二条写语句）
#   2. 首 token 白名单（read_query 仅 SELECT/WITH；write_query 仅 INSERT/UPDATE/DELETE/MERGE）
#   3. 危险关键字黑名单（EXEC/OPENROWSET 等，词边界匹配，防止 WITH...DELETE 之类的注入）

# 块注释 /* ... */（非贪婪，DOTALL）
_RE_BLOCK_COMMENT = re.compile(r'/\*.*?\*/', re.DOTALL)
# 行注释 -- ... （到行尾）
_RE_LINE_COMMENT = re.compile(r'--[^\n]*')
# 字符串字面量 '...'（含 '' 转义）
_RE_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")


def _skeleton(sql: str) -> str:
    """剥离注释与字符串字面量，返回用于安全校验的语句骨架"""
    s = _RE_BLOCK_COMMENT.sub(' ', sql)
    s = _RE_LINE_COMMENT.sub(' ', s)
    s = _RE_STRING_LITERAL.sub(' ', s)
    return s


def _first_keyword(sql: str) -> str:
    """提取语句骨架中的第一个有效关键字（大写）"""
    tokens = _skeleton(sql).split()
    return tokens[0].upper() if tokens else ""


def _is_single_statement(sql: str) -> bool:
    """校验是否为单条语句（忽略末尾多余分号），防止批处理夹带"""
    parts = [p for p in _skeleton(sql).split(';') if p.strip()]
    return len(parts) <= 1


# 危险关键字黑名单：存储过程执行、越权查询、破坏性 DDL/DCL、备份恢复等
_RE_DANGEROUS = re.compile(
    r'\b('
    r'EXEC|EXECUTE|'
    r'INSERT|UPDATE|DELETE|MERGE|'
    r'DROP|ALTER|CREATE|TRUNCATE|'
    r'GRANT|REVOKE|DENY|'
    r'BACKUP|RESTORE|'
    r'OPENROWSET|OPENQUERY|OPENDATASOURCE|OPENXML'
    r')\b',
    re.IGNORECASE,
)

# write_query 允许的首关键字白名单（INSERT/UPDATE/DELETE/MERGE）
_WRITE_WHITELIST = {"INSERT", "UPDATE", "DELETE", "MERGE"}


def validate_read_query(sql: str) -> None:
    """校验 read_query 的 SQL：仅允许单条 SELECT / WITH 查询，且不含任何写/执行类关键字

    说明：此前版本只检查前缀，`WITH cte AS (...) DELETE FROM t` 这类语句可以绕过
    校验进入只读通道；现在对整个语句骨架做危险关键字扫描，彻底封堵该漏洞。
    """
    if not sql or not sql.strip():
        raise ValueError("SQL 语句不能为空")
    if not _is_single_statement(sql):
        raise ValueError("read_query 只允许执行单条语句（检测到多条由分号分隔的语句）")
    kw = _first_keyword(sql)
    if kw not in ("SELECT", "WITH"):
        raise ValueError("read_query 只允许 SELECT 查询和 WITH 子句")
    matched = _RE_DANGEROUS.search(_skeleton(sql))
    if matched:
        raise ValueError(
            f"read_query 不允许包含写操作或存储过程关键字: {matched.group(0).upper()}"
        )


def validate_write_query(sql: str) -> None:
    """校验 write_query 的 SQL：白名单限制为 INSERT/UPDATE/DELETE/MERGE 单条语句

    说明：此前版本仅排除 SELECT 开头，EXEC xp_cmdshell / DROP TABLE / TRUNCATE 等
    均可执行；现在改为白名单 + 危险关键字双重校验。
    """
    if not sql or not sql.strip():
        raise ValueError("SQL 语句不能为空")
    if not _is_single_statement(sql):
        raise ValueError("write_query 只允许执行单条语句（检测到多条由分号分隔的语句）")
    kw = _first_keyword(sql)
    if kw not in _WRITE_WHITELIST:
        raise ValueError(
            "write_query 只允许 INSERT、UPDATE、DELETE、MERGE 语句"
            f"（当前语句以 {kw or '<空>'} 开头）；建表请使用 create_table"
        )
    # MERGE 语句可能内嵌 EXEC，统一拦截
    matched = re.search(r'\b(EXEC|EXECUTE|OPENROWSET|OPENQUERY|OPENDATASOURCE)\b',
                        _skeleton(sql), re.IGNORECASE)
    if matched:
        raise ValueError(f"write_query 不允许包含存储过程或越权查询关键字: {matched.group(0).upper()}")


def validate_create_table(sql: str) -> None:
    """校验 create_table 的 SQL：仅允许单条 CREATE TABLE 语句"""
    if not sql or not sql.strip():
        raise ValueError("SQL 语句不能为空")
    if not _is_single_statement(sql):
        raise ValueError("create_table 只允许执行单条语句")
    kw = _first_keyword(sql)
    if kw != "CREATE" or not re.match(r'^\s*CREATE\s+TABLE\b', _skeleton(sql), re.IGNORECASE):
        raise ValueError("只允许 CREATE TABLE 语句")


# 存储过程名标识符：普通标识符（字母/下划线开头）或 [方括号转义] 形式，1~3 段（库.架构.名）
_RE_PLAIN_IDENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def _is_connection_error(e: pyodbc.Error) -> bool:
    """判断是否为连接级错误（值得重连重试）

    重试判据（实测错误形态）：
      - SQLSTATE 08xxx 类（如 08S01 通信链路失败：网络断开、服务重启）
      - pyodbc 抛出的 "Attempt to use a closed connection"（连接已被关闭）
    不重试（避免副作用）：
      - 42xxx 语法/语义错误 —— 重试必然再失败，白白销毁健康连接
      - HYT00 查询超时 —— 重试会让用户等待双倍超时时间
    """
    first = e.args[0] if e.args else None
    if isinstance(first, str) and first.startswith('08'):
        return True
    return 'closed connection' in str(e).lower()


def validate_procedure_name(name: str) -> str:
    """校验存储过程名并返回规范化后的名称

    过程名会拼入 {CALL ...} 语句，必须严格限制为 1~3 段点分标识符
    （如 sp_name / dbo.sp_name / db.dbo.sp_name），每段为普通标识符或
    [方括号转义] 形式，从根源上杜绝通过过程名注入 SQL。
    """
    if not name or not name.strip():
        raise ValueError("存储过程名不能为空")
    name = name.strip()
    parts = name.split('.')
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"存储过程名格式无效: {name}（应为 1~3 段点分标识符，如 dbo.sp_name）")
    for part in parts:
        part = part.strip()
        if part.startswith('[') and part.endswith(']'):
            inner = part[1:-1]
            # 方括号内不允许再出现 ] 或空串
            if not inner or ']' in inner:
                raise ValueError(f"存储过程名格式无效: {name}")
        elif not _RE_PLAIN_IDENT.match(part):
            raise ValueError(
                f"存储过程名格式无效: {name}（每段须为普通标识符或 [方括号转义] 形式）"
            )
    return name


class Config:
    """配置加载与多数据库配置解析

    配置文件查找顺序（找到即停）：
      1. 环境变量 MSSQL_MCP_CONFIG 指定的路径
      2. 模块同目录下的 config.json（源码运行方式，兼容历史行为）
      3. 当前工作目录下的 config.json（pip 安装后的运行方式）

    支持两种配置格式：
      - 多库格式：顶层 "databases" 为命名字典，每个连接独立配置
      - 单库格式（向后兼容）：顶层 "database" 会被包装为 {"default": ...}
    """

    def __init__(self):
        module_dir = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            os.environ.get('MSSQL_MCP_CONFIG'),
            os.path.join(module_dir, 'config.json'),
            os.path.join(os.getcwd(), 'config.json'),
        ]
        # 环境变量指定的路径必须存在（显式配置错误应立即暴露）；其余候选存在即用
        self.config_path = next(
            (p for p in candidates if p and (p == candidates[0] or os.path.exists(p))),
            candidates[1],
        )
        self.load_config()
        self._init_databases()

    def load_config(self):
        """加载配置文件；不存在时给出明确提示（列出查找过的位置）"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self.config = json.load(f)
                logger.info(f"配置文件加载成功: {self.config_path}")
        except FileNotFoundError:
            raise FileNotFoundError(
                f"未找到配置文件: {self.config_path}\n"
                f"查找顺序: 1) MSSQL_MCP_CONFIG 环境变量 2) server.py 同目录的 config.json "
                f"3) 当前工作目录的 config.json\n"
                f"请创建配置文件（格式参见 README），或通过 MSSQL_MCP_CONFIG 指定路径"
            )
        except json.JSONDecodeError as e:
            raise ValueError(f"配置文件不是合法的 JSON（第 {e.lineno} 行第 {e.colno} 列）: {e.msg}")
        except Exception as e:
            logger.error(f"加载配置文件失败: {e}")
            raise

    def _init_databases(self):
        """解析多数据库配置：databases 字典优先，兼容旧版单库 database 节"""
        if 'databases' in self.config:
            databases = self.config['databases']
            if not isinstance(databases, dict) or not databases:
                raise ValueError("databases 配置必须是非空对象（命名的连接字典）")
        elif 'database' in self.config:
            # 旧版单库配置 → 包装为名为 "default" 的单连接（完全向后兼容）
            databases = {"default": self.config['database']}
            logger.info("检测到单库配置格式，已包装为连接名: default")
        else:
            raise ValueError("配置文件缺少 database 或 databases 节")

        # 校验每个连接的必备键，尽早暴露配置错误
        required = ('driver', 'server', 'database')
        for name, db_cfg in databases.items():
            if not isinstance(db_cfg, dict):
                raise ValueError(f"连接 {name} 的配置必须是对象")
            missing = [k for k in required if k not in db_cfg]
            if missing:
                raise ValueError(f"连接 {name} 的配置缺少必备字段: {', '.join(missing)}")

        self.databases: dict[str, dict] = databases
        # 默认连接：显式 default_database > 名为 "default" 的连接 > 字典第一个连接
        self.default_database = (
            self.config.get('default_database')
            or ("default" if "default" in databases else next(iter(databases)))
        )
        if self.default_database not in databases:
            raise ValueError(
                f"default_database 指向的连接不存在: {self.default_database}"
                f"（可用: {', '.join(databases)}）"
            )

    def db_config(self, name: str) -> dict:
        """按名称获取数据库连接配置；未知名时报错并列出可用连接"""
        if name not in self.databases:
            raise ValueError(
                f"未知数据库连接: {name}（可用: {', '.join(self.databases)}）"
            )
        return self.databases[name]

    @property
    def server_name(self) -> str:
        return self.config['server']['name']

    @property
    def server_version(self) -> str:
        return self.config['server']['version']


def _connection_string(db_config: dict) -> str:
    """构建数据库连接字符串

    兼容 ODBC Driver 18：支持可选 encrypt / trust_server_certificate 配置项，
    Driver 18 默认强制加密，连接旧服务器时通常需要 trust_server_certificate=true
    """
    conn_parts = [
        f"DRIVER={{{db_config['driver']}}}",
        f"SERVER={db_config['server']}",
        f"DATABASE={db_config['database']}"
    ]

    if db_config.get('trusted_connection', False):
        conn_parts.append("Trusted_Connection=yes")
    else:
        conn_parts.extend([
            f"UID={db_config['username']}",
            f"PWD={db_config['password']}"
        ])

    # ODBC Driver 18 相关的可选加密配置
    if 'encrypt' in db_config and db_config['encrypt'] is not None:
        conn_parts.append(f"Encrypt={'yes' if db_config['encrypt'] else 'no'}")
    if db_config.get('trust_server_certificate', False):
        conn_parts.append("TrustServerCertificate=yes")

    return ";".join(conn_parts)


def _serialize_value(value: Any) -> Any:
    """将 pyodbc 返回的非 JSON 原生类型序列化为 JSON 兼容值（结构化输出需要）"""
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)  # 数值语义优先；极端精度场景可按需改为 str
    if isinstance(value, bytes):
        return f"<binary {len(value)} bytes>"
    if value is None:
        return None
    return value


class MssqlDatabase:
    """单个数据库连接的访问层：连接复用 + 线程安全 + 只读护栏 + 行数/超时限制

    连接策略：惰性建立长连接并复用，避免每次查询重复 TCP 握手 + SQL Server
    登录协商（约 50~150ms 开销）；查询失败且判定为连接失效时可自动重建重试。
    """

    def __init__(self, name: str, db_config: dict):
        self.name = name
        self.db_config = db_config
        self._conn: pyodbc.Connection | None = None
        self._lock = threading.Lock()  # 保护共享连接（线程池并发调用时串行化）

    # ---------- 每连接配置项（带默认值回退） ----------

    def _setting(self, key: str):
        return self.db_config.get(key, DEFAULTS[key])

    @property
    def readonly(self) -> bool:
        return bool(self._setting('readonly'))

    @property
    def query_timeout(self) -> int:
        return int(self._setting('query_timeout'))

    @property
    def max_rows(self) -> int:
        return int(self._setting('max_rows'))

    @property
    def confirm_writes(self) -> bool:
        return bool(self._setting('confirm_writes'))

    @property
    def connection_string(self) -> str:
        return _connection_string(self.db_config)

    def check_writable(self):
        """只读模式下拒绝一切写操作（含存储过程黑盒调用）"""
        if self.readonly:
            raise ValueError(f"数据库连接 [{self.name}] 处于只读模式 (readonly=true)，写操作已被禁用")

    # ---------- 连接管理 ----------

    def test_connection(self):
        """验证数据库连通性，尽早暴露配置错误（不保留连接）"""
        logger.debug(f"[{self.name}] 测试数据库连接")
        try:
            with closing(pyodbc.connect(self.connection_string, timeout=10)):
                logger.debug(f"[{self.name}] 数据库连接测试成功")
        except Exception as e:
            logger.error(f"[{self.name}] 数据库连接失败: {e}")
            raise

    def _get_connection(self) -> pyodbc.Connection:
        """获取（或惰性建立）复用连接，并设置查询超时"""
        if self._conn is None:
            conn = pyodbc.connect(self.connection_string, timeout=10)
            conn.autocommit = False
            # 连接后设置 timeout 即为查询超时（秒），防止慢查询拖垮会话
            # 注意：不设置 SET NOCOUNT ON —— 开启后 pyodbc 的 rowcount 会
            # 恒为 -1，导致写操作无法返回受影响行数
            conn.timeout = self.query_timeout
            self._conn = conn
            logger.debug(f"[{self.name}] 已建立复用数据库连接")
        return self._conn

    def _reset_connection(self):
        """丢弃失效连接，下次调用时重建"""
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass  # 连接已断开，关闭失败可忽略

    def _with_cursor(self, action, *, commit: bool, retry: bool):
        """在锁保护下执行游标操作

        action(cursor) 返回业务结果；commit=True 提交事务（写路径），
        否则回滚（读路径，清理可能残留的隐式事务）。
        retry=True 时（读路径）仅对连接级错误（网络断开/连接已关闭）重建连接
        并重试一次；语法错误与查询超时不重试（重试必然再失败或双倍等待）。
        写路径一律不重试——避免网络抖动场景下重复执行已部分生效的写语句。
        """
        with self._lock:
            for attempt in (1, 2):
                try:
                    conn = self._get_connection()
                    with closing(conn.cursor()) as cursor:
                        result = action(cursor)
                    conn.commit() if commit else conn.rollback()
                    return result
                except pyodbc.Error as e:
                    if attempt == 2 or not retry or not _is_connection_error(e):
                        raise
                    # 连接失效（网络中断、服务重启等）：丢弃旧连接后重试一次
                    self._reset_connection()

    # ---------- 查询执行 ----------

    def execute_read(self, query: str, params: tuple | list | None = None) -> dict[str, Any]:
        """执行只读查询，返回结构化结果（列名 + 行数据 + 截断标记）

        行数受 max_rows 限制：fetchmany(max_rows + 1) 探测是否还有剩余行，
        超限时截断并置 truncated=True，防止大表结果撑爆 LLM 上下文窗口。
        """
        logger.debug(f"[{self.name}] 执行只读查询: {query}")
        max_rows = self.max_rows

        def action(cursor: pyodbc.Cursor):
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)
            columns = [c[0] for c in cursor.description] if cursor.description else []
            rows = cursor.fetchmany(max_rows + 1)  # 多取一行用于探测截断
            return columns, rows[:max_rows], len(rows) > max_rows

        columns, rows, truncated = self._with_cursor(action, commit=False, retry=True)

        data = [[_serialize_value(v) for v in row] for row in rows]
        logger.debug(f"[{self.name}] 读取查询返回了 {len(data)} 行（截断: {truncated}）")
        return {
            "columns": columns,
            "rows": data,
            "row_count": len(data),
            "truncated": truncated,
        }

    def execute_write(self, query: str, params: tuple | list | None = None) -> dict[str, Any]:
        """执行写语句（INSERT/UPDATE/DELETE/MERGE/DDL），返回受影响行数"""
        logger.debug(f"[{self.name}] 执行写入查询: {query}")

        def action(cursor: pyodbc.Cursor):
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)
            return cursor.rowcount  # 先取 rowcount 再 commit

        affected = self._with_cursor(action, commit=True, retry=False)
        logger.debug(f"[{self.name}] 写入查询影响了 {affected} 行")
        return {"affected_rows": affected if affected and affected > 0 else 0}

    def execute_procedure(self, procedure: str, parameters: list[Any] | None = None) -> dict[str, Any]:
        """执行存储过程，收割全部结果集（SP 可能返回多个）

        过程名经过 validate_procedure_name 严格校验后拼入 ODBC 转义序列
        {CALL name(?,...)}，参数以绑定值传递，无注入面。
        参数仅允许标量（str/int/float/bool/None）—— pyodbc 无法绑定
        JSON 对象/数组，提前校验以给出清晰错误而非晦涩的驱动报错。
        """
        procedure = validate_procedure_name(procedure)
        params = [p for p in (parameters or [])]
        for i, p in enumerate(params):
            if not isinstance(p, (str, int, float, bool, type(None))):
                raise ValueError(
                    f"存储过程参数 [{i}] 必须是标量值（字符串/数字/布尔/null），"
                    f"收到: {type(p).__name__}；如需传对象请先序列化为 JSON 字符串"
                )
        placeholders = ",".join("?" * len(params))
        call_sql = f"{{CALL {procedure}({placeholders})}}" if params else f"{{CALL {procedure}}}"
        logger.debug(f"[{self.name}] 执行存储过程: {call_sql}，参数: {params}")
        max_rows = self.max_rows

        def action(cursor: pyodbc.Cursor):
            result_sets = []
            if params:
                cursor.execute(call_sql, params)
            else:
                cursor.execute(call_sql)
            # 循环收割全部结果集（nextset 返回 False 表示没有更多）
            while True:
                if cursor.description:
                    columns = [c[0] for c in cursor.description]
                    rows = cursor.fetchmany(max_rows + 1)
                    truncated = len(rows) > max_rows
                    data = [[_serialize_value(v) for v in row] for row in rows[:max_rows]]
                    result_sets.append({
                        "columns": columns,
                        "rows": data,
                        "row_count": len(data),
                        "truncated": truncated,
                    })
                if not cursor.nextset():
                    break
            # 无结果集时 rowcount 反映受影响行数；有结果集时无意义，置 0
            affected = cursor.rowcount if not result_sets else 0
            return result_sets, affected

        result_sets, affected = self._with_cursor(action, commit=True, retry=False)
        logger.debug(f"[{self.name}] 存储过程返回 {len(result_sets)} 个结果集")
        return {
            "procedure": procedure,
            "database": self.name,
            "result_sets": result_sets,
            "affected_rows": affected if affected and affected > 0 else 0,
        }

    # ---------- 结构描述（工具与资源模板共用） ----------

    def describe_table(self, table_name: str, schema: str = "dbo") -> dict[str, Any]:
        """获取表的完整结构：列（含主键/自增）、外键、索引，复合结构化输出"""
        columns_result = self.execute_read(
            """
            SELECT
                c.COLUMN_NAME AS name,
                c.DATA_TYPE AS type,
                c.IS_NULLABLE AS nullable,
                c.COLUMN_DEFAULT AS default_value,
                c.CHARACTER_MAXIMUM_LENGTH AS max_length,
                CASE WHEN pk.COLUMN_NAME IS NOT NULL THEN 1 ELSE 0 END AS is_primary_key,
                COLUMNPROPERTY(OBJECT_ID(QUOTENAME(c.TABLE_SCHEMA) + '.' + QUOTENAME(c.TABLE_NAME)),
                               c.COLUMN_NAME, 'IsIdentity') AS is_identity
            FROM INFORMATION_SCHEMA.COLUMNS c
            LEFT JOIN (
                SELECT ku.TABLE_SCHEMA, ku.TABLE_NAME, ku.COLUMN_NAME
                FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE ku
                JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                  ON tc.CONSTRAINT_NAME = ku.CONSTRAINT_NAME
                 AND tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
            ) pk ON pk.TABLE_SCHEMA = c.TABLE_SCHEMA
                 AND pk.TABLE_NAME = c.TABLE_NAME
                 AND pk.COLUMN_NAME = c.COLUMN_NAME
            WHERE c.TABLE_NAME = ? AND c.TABLE_SCHEMA = ?
            ORDER BY c.ORDINAL_POSITION
            """,
            (table_name, schema),
        )
        if not columns_result["rows"]:
            raise ValueError(f"未找到表: {schema}.{table_name}")

        col_names = columns_result["columns"]
        columns = [dict(zip(col_names, row)) for row in columns_result["rows"]]

        # 外键：父表 → 引用表 的逐列映射
        fk_result = self.execute_read(
            """
            SELECT fk.name AS constraint_name,
                   cp.name AS from_column,
                   tr.name AS to_table,
                   cr.name AS to_column
            FROM sys.foreign_keys fk
            JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
            JOIN sys.tables tp ON tp.object_id = fkc.parent_object_id
            JOIN sys.columns cp ON cp.object_id = fkc.parent_object_id AND cp.column_id = fkc.parent_column_id
            JOIN sys.tables tr ON tr.object_id = fkc.referenced_object_id
            JOIN sys.columns cr ON cr.object_id = fkc.referenced_object_id AND cr.column_id = fkc.referenced_column_id
            WHERE tp.name = ? AND SCHEMA_NAME(tp.schema_id) = ?
            ORDER BY fk.name, cp.name
            """,
            (table_name, schema),
        )
        fk_names = fk_result["columns"]
        foreign_keys = [dict(zip(fk_names, row)) for row in fk_result["rows"]]

        # 索引：逐列返回后在 Python 侧按索引名聚合（不依赖 SQL Server 2017+ 的 STRING_AGG）
        idx_result = self.execute_read(
            """
            SELECT i.name AS index_name,
                   i.type_desc AS type,
                   i.is_primary_key,
                   i.is_unique,
                   c.name AS column_name
            FROM sys.indexes i
            JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
            JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
            JOIN sys.tables t ON t.object_id = i.object_id
            WHERE t.name = ? AND SCHEMA_NAME(t.schema_id) = ?
            ORDER BY i.name, ic.key_ordinal
            """,
            (table_name, schema),
        )
        indexes: list[dict[str, Any]] = []
        by_name: dict[str, dict[str, Any]] = {}
        for index_name, type_desc, is_pk, is_unique, column_name in idx_result["rows"]:
            if index_name not in by_name:
                by_name[index_name] = {
                    "name": index_name,
                    "type": type_desc,
                    "is_primary_key": bool(is_pk),
                    "is_unique": bool(is_unique),
                    "columns": [],
                }
                indexes.append(by_name[index_name])
            by_name[index_name]["columns"].append(column_name)

        return {
            "table": f"{schema}.{table_name}",
            "database": self.name,
            "columns": columns,
            "foreign_keys": foreign_keys,
            "indexes": indexes,
        }


class DatabaseManager:
    """多数据库连接管理器：按名称持有 MssqlDatabase 实例 + 服务器级业务洞察备忘录

    启动时仅对默认连接做连通性测试（快速暴露主配置错误）；
    其余连接惰性建立（prod 等可选连接不可达时不阻塞服务器启动）。
    """

    def __init__(self, config: Config):
        self.config = config
        self.insights: list[str] = []  # 业务洞察（服务器级，不区分数据库）
        self._databases: dict[str, MssqlDatabase] = {
            name: MssqlDatabase(name, cfg) for name, cfg in config.databases.items()
        }
        self._databases[config.default_database].test_connection()

    def get(self, name: str | None = None) -> MssqlDatabase:
        """按名称获取数据库连接（None 时返回默认连接）；未知名时报错并列出可用连接"""
        name = name or self.config.default_database
        db = self._databases.get(name)
        if db is None:
            raise ValueError(
                f"未知数据库连接: {name}（可用: {', '.join(self._databases)}）"
            )
        return db

    def list_databases(self) -> list[dict[str, Any]]:
        """列出全部已配置连接及各自的只读状态（供 LLM 发现可用的 database 参数取值）"""
        return [
            {
                "name": name,
                "readonly": db.readonly,
                "is_default": name == self.config.default_database,
            }
            for name, db in self._databases.items()
        ]

    def _synthesize_memo(self) -> str:
        """合成业务洞察备忘录"""
        logger.debug(f"合成备忘录，包含 {len(self.insights)} 条洞察")
        if not self.insights:
            return "尚未发现业务洞察。"

        insights = "\n".join(f"- {insight}" for insight in self.insights)

        memo = "📊 业务洞察备忘录 📊\n\n"
        memo += "发现的关键洞察：\n\n"
        memo += insights

        if len(self.insights) > 1:
            memo += "\n总结：\n"
            memo += f"分析发现了 {len(self.insights)} 条关键业务洞察，这些洞察表明了战略优化和增长的机会。"

        logger.debug("生成了基本的备忘录格式")
        return memo


class WriteConfirmInput(BaseModel):
    """写操作确认表单（Elicitation，MCP 2025-06-18 规范）"""
    confirm: bool = Field(default=False, description="确认执行该写操作")


async def _confirm_write(ctx: Context, db_name: str, description: str) -> None:
    """写操作前的 Elicitation 二次确认

    用户确认（accept + confirm=true）→ 放行；拒绝/取消 → 抛错中止。
    客户端不支持 elicitation 时记录警告并放行，保持与旧行为兼容。
    """
    message = f"即将在数据库 [{db_name}] 上执行写操作，请确认：\n\n{description[:800]}"
    try:
        result = await ctx.elicit(message, WriteConfirmInput)
        if result.action != "accept" or not (result.data and result.data.confirm):
            raise ValueError("用户未确认写操作，已取消执行")
    except ValueError:
        raise  # 用户明确拒绝，正常中止
    except Exception as e:
        # 客户端不支持 elicitation（如 Cursor）→ 记录警告后直接执行
        logger.warning(f"客户端不支持写确认 (elicitation)，跳过确认: {e}")


def build_server(config: Config, manager: DatabaseManager) -> FastMCP:
    """构建 FastMCP 服务器并注册资源 / 资源模板 / 工具 / 提示模板"""
    mcp = FastMCP(
        config.server_name,
        instructions=(
            "MSSQL MCP Server：提供 SQL Server 多数据库查询、写入、建表、存储过程、"
            "结构浏览与业务洞察记录能力。工具的 database 参数可切换连接（可用连接"
            "通过 list_databases 查询，不传时使用默认连接）。read_query 仅允许"
            "SELECT/WITH；write_query 仅允许 INSERT/UPDATE/DELETE/MERGE。"
        ),
    )

    # ==================== 资源（Resources） ====================

    @mcp.resource("memo://insights", name="业务洞察备忘录",
                  description="一个实时更新的业务洞察文档")
    def insights_memo() -> str:
        """memo://insights —— 实时更新的业务洞察备忘录资源"""
        logger.debug("读取 memo://insights 资源")
        return manager._synthesize_memo()

    # ==================== 资源模板（Resource Templates） ====================
    # 表结构按需拉取（不占用每轮对话的工具 schema token），支持两种 URI：
    #   schema://dbo/orders            → 默认数据库的表结构
    #   schema://prod/dbo/orders       → 指定数据库的表结构

    @mcp.resource("schema://{database}/{schema}/{table_name}",
                  name="表结构（指定数据库）",
                  description="按需获取指定数据库中某张表的完整结构（列/主键/自增/外键/索引），返回 JSON")
    async def table_schema_resource(database: str, schema: str, table_name: str) -> str:
        db = manager.get(database)
        result = await asyncio.to_thread(db.describe_table, table_name, schema)
        return json.dumps(result, ensure_ascii=False, default=str)

    @mcp.resource("schema://{schema}/{table_name}",
                  name="表结构（默认数据库）",
                  description="按需获取默认数据库中某张表的完整结构（列/主键/自增/外键/索引），返回 JSON")
    async def table_schema_default_resource(schema: str, table_name: str) -> str:
        db = manager.get()
        result = await asyncio.to_thread(db.describe_table, table_name, schema)
        return json.dumps(result, ensure_ascii=False, default=str)

    # ==================== 工具（Tools） ====================

    @mcp.tool(
        name="read_query",
        description="在 MSSQL 数据库上执行 SELECT / WITH 查询，返回结构化结果（受 max_rows 行数限制）",
        annotations=types.ToolAnnotations(readOnlyHint=True),
    )
    async def read_query(query: str, database: str | None = None) -> dict[str, Any]:
        """执行只读查询。结果包含 columns / rows / row_count / truncated。"""
        validate_read_query(query)  # 安全校验：白名单 + 危险关键字扫描
        db = manager.get(database)
        # pyodbc 为同步库，放入工作线程执行，避免阻塞事件循环
        return await asyncio.to_thread(db.execute_read, query)

    @mcp.tool(
        name="write_query",
        description="在 MSSQL 数据库上执行 INSERT、UPDATE、DELETE 或 MERGE 语句",
        annotations=types.ToolAnnotations(destructiveHint=True, idempotentHint=False),
    )
    async def write_query(query: str, database: str | None = None,
                          ctx: Context = None) -> dict[str, Any]:
        """执行写语句，返回 affected_rows。confirm_writes 开启时先向用户二次确认。"""
        db = manager.get(database)
        db.check_writable()  # 只读模式护栏
        validate_write_query(query)  # 安全校验：白名单 + 危险关键字扫描
        if db.confirm_writes and ctx is not None:
            await _confirm_write(ctx, db.name, query)  # Elicitation 二次确认
        return await asyncio.to_thread(db.execute_write, query)

    @mcp.tool(
        name="create_table",
        description="在 MSSQL 数据库中执行 CREATE TABLE 建表语句；confirm_writes 开启时先二次确认",
        annotations=types.ToolAnnotations(destructiveHint=True, idempotentHint=False),
    )
    async def create_table(query: str, database: str | None = None,
                           ctx: Context = None) -> dict[str, Any]:
        """执行建表语句，返回 affected_rows。confirm_writes 开启时先向用户二次确认。

        建表属于 DDL（schema 变更），与 DML 一样是状态变更操作，
        因此同样纳入 confirm_writes 的 Elicitation 二次确认范围。
        """
        db = manager.get(database)
        db.check_writable()  # 只读模式护栏
        validate_create_table(query)  # 安全校验：仅允许单条 CREATE TABLE
        if db.confirm_writes and ctx is not None:
            await _confirm_write(ctx, db.name, query)  # Elicitation 二次确认
        return await asyncio.to_thread(db.execute_write, query)

    @mcp.tool(
        name="list_tables",
        description="列出 MSSQL 数据库中的所有用户表（含所属 schema）",
        annotations=types.ToolAnnotations(readOnlyHint=True),
    )
    async def list_tables(database: str | None = None) -> dict[str, Any]:
        """返回表清单（schema + 表名）。"""
        db = manager.get(database)
        return await asyncio.to_thread(
            db.execute_read,
            """
            SELECT TABLE_SCHEMA AS schema_name, TABLE_NAME AS name
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_TYPE = 'BASE TABLE'
              AND TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA')
            ORDER BY TABLE_SCHEMA, TABLE_NAME
            """,
        )

    @mcp.tool(
        name="list_views",
        description="列出 MSSQL 数据库中的所有视图（含所属 schema）",
        annotations=types.ToolAnnotations(readOnlyHint=True),
    )
    async def list_views(database: str | None = None) -> dict[str, Any]:
        """返回视图清单（schema + 视图名）。"""
        db = manager.get(database)
        return await asyncio.to_thread(
            db.execute_read,
            """
            SELECT TABLE_SCHEMA AS schema_name, TABLE_NAME AS name
            FROM INFORMATION_SCHEMA.VIEWS
            WHERE TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA')
            ORDER BY TABLE_SCHEMA, TABLE_NAME
            """,
        )

    @mcp.tool(
        name="describe_table",
        description="获取特定表的完整结构信息（列、类型、可空、默认值、主键、自增、外键、索引）",
        annotations=types.ToolAnnotations(readOnlyHint=True),
    )
    async def describe_table(table_name: str, schema: str = "dbo",
                             database: str | None = None) -> dict[str, Any]:
        """返回指定表（默认 dbo schema）的完整结构。

        参数:
            table_name: 要描述的表名
            schema: 表所属 schema，默认 dbo；非 dbo schema 的表需显式指定
            database: 目标数据库连接名，不传时使用默认连接
        """
        if not table_name or not table_name.strip():
            raise ValueError("缺少 table_name 参数")
        db = manager.get(database)
        return await asyncio.to_thread(db.describe_table, table_name, schema)

    @mcp.tool(
        name="list_databases",
        description="列出当前 MCP 服务器配置的全部数据库连接（名称、只读状态、是否默认）",
        annotations=types.ToolAnnotations(readOnlyHint=True),
    )
    async def list_databases() -> dict[str, Any]:
        """返回可用连接清单，作为其他工具 database 参数的取值依据。"""
        return {"databases": manager.list_databases()}

    @mcp.tool(
        name="list_procedures",
        description="列出 MSSQL 数据库中指定 schema 的存储过程及其参数签名",
        annotations=types.ToolAnnotations(readOnlyHint=True),
    )
    async def list_procedures(schema: str = "dbo", database: str | None = None) -> dict[str, Any]:
        """返回存储过程清单（过程名 + 逐参数的类型与方向）。"""
        db = manager.get(database)
        return await asyncio.to_thread(
            db.execute_read,
            """
            SELECT r.ROUTINE_NAME AS procedure_name,
                   p.PARAMETER_NAME AS parameter_name,
                   p.DATA_TYPE AS parameter_type,
                   p.PARAMETER_MODE AS mode
            FROM INFORMATION_SCHEMA.ROUTINES r
            LEFT JOIN INFORMATION_SCHEMA.PARAMETERS p
              ON p.SPECIFIC_SCHEMA = r.ROUTINE_SCHEMA
             AND p.SPECIFIC_NAME = r.ROUTINE_NAME
             AND p.ORDINAL_POSITION > 0
            WHERE r.ROUTINE_TYPE = 'PROCEDURE' AND r.ROUTINE_SCHEMA = ?
            ORDER BY r.ROUTINE_NAME, p.ORDINAL_POSITION
            """,
            (schema,),
        )

    @mcp.tool(
        name="execute_procedure",
        description="执行 MSSQL 存储过程（支持多结果集）；readonly 连接上不可用，confirm_writes 开启时先二次确认",
        annotations=types.ToolAnnotations(destructiveHint=True, idempotentHint=False),
    )
    async def execute_procedure(procedure: str,
                                parameters: list[Any] | None = None,
                                database: str | None = None,
                                ctx: Context = None) -> dict[str, Any]:
        """执行存储过程并收割全部结果集。

        参数:
            procedure: 存储过程名（如 dbo.my_proc，1~3 段点分标识符）
            parameters: 位置参数值列表，按过程签名顺序传入
            database: 目标数据库连接名，不传时使用默认连接
        """
        db = manager.get(database)
        db.check_writable()  # 存储过程是黑盒（内部可能写库），只读模式下禁用
        procedure = validate_procedure_name(procedure)  # 过程名严格校验，杜绝拼接注入
        if db.confirm_writes and ctx is not None:
            await _confirm_write(ctx, db.name, f"EXEC {procedure} {parameters or ''}")  # Elicitation 二次确认
        return await asyncio.to_thread(db.execute_procedure, procedure, parameters)

    @mcp.tool(
        name="append_insight",
        description="向业务洞察备忘录资源 (memo://insights) 添加一条洞察",
        annotations=types.ToolAnnotations(readOnlyHint=True),
    )
    async def append_insight(insight: str, ctx: Context = None) -> str:
        """记录一条业务洞察并通知客户端资源已更新。"""
        if not insight or not insight.strip():
            raise ValueError("缺少 insight 参数")

        manager.insights.append(insight)
        _ = manager._synthesize_memo()

        # 通知客户端备忘录资源已更新（MCP 资源变更通知）
        if ctx is not None:
            await ctx.request_context.session.send_resource_updated(AnyUrl("memo://insights"))

        return "洞察已添加到备忘录"

    # ==================== 提示模板（Prompts） ====================

    @mcp.prompt(name="mcp-demo", description="一个用于在 MSSQL 数据库中创建初始数据并演示 MSSQL MCP 服务器功能的提示")
    def mcp_demo(topic: str) -> str:
        """mcp-demo 演示提示：按主题生成业务场景并引导演示三大能力"""
        logger.debug(f"为主题 {topic} 生成提示模板")
        return PROMPT_TEMPLATE.format(topic=topic).strip()

    return mcp


def _create_components() -> tuple[Config, DatabaseManager, FastMCP]:
    """加载配置 → 建立数据库管理器 → 构建服务器（供 main 与测试复用）"""
    config = Config()
    manager = DatabaseManager(config)
    return config, manager, build_server(config, manager)


async def main():
    """主入口函数：以 stdio 传输运行 MCP 服务器"""
    logger.info("启动 MSSQL MCP 服务器")
    _, _, mcp = _create_components()
    logger.info("服务器正在使用 stdio 传输运行")
    await mcp.run_stdio_async()


def main_sync():
    """同步入口（pyproject.toml 的 console_scripts 使用）"""
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
