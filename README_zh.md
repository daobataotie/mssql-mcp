# MSSQL MCP Server

## 概述

MSSQL MCP Server, 提供数据库交互和商业智能功能。此服务器支持运行 SQL 查询、分析业务数据和自动生成业务洞察备忘录。  
参照官网的 SQLite 进行修改以适配MSSQL。

基于官方 MCP Python SDK（FastMCP）构建，支持 MCP 2025-06-18 规范特性：结构化工具输出（`structuredContent`）、工具注解、资源模板（Resource Templates）、Elicitation 写操作确认。

核心亮点：

- **多数据库连接** — 一个服务器同时挂载 dev/staging/prod，所有工具通过可选的 `database` 参数切换，每个连接独立配置 `readonly` / `query_timeout` / `max_rows` / `confirm_writes`
- **存储过程** — `list_procedures` 查看签名、`execute_procedure` 执行并完整收割多结果集
- **资源模板** — `schema://{database}/{schema}/{table}` 按需拉取表结构，不占用每轮对话的工具 schema token
- **安全护栏** — SQL 白名单校验、只读模式、行数上限、查询超时、写操作可选二次确认

## 工具

- `read_query`
   - 在 MSSQL 数据库上执行 SELECT / WITH 查询，返回结构化结果（columns / rows / row_count / truncated），受 `max_rows` 行数限制
- `write_query`
   - 在 MSSQL 数据库上执行 INSERT、UPDATE、DELETE 或 MERGE 语句，返回受影响行数
- `create_table`
   - 在 MSSQL 数据库中创建新表
- `list_tables`
   - 列出 MSSQL 数据库中的所有表（schema + 表名）
- `list_views`
   - 列出 MSSQL 数据库中的所有视图
- `describe_table`
   - 获取特定表的完整结构（类型、可空、默认值、主键、自增、外键、索引）
- `list_databases`
   - 列出已配置的数据库连接（名称、只读状态、是否默认）
- `list_procedures`
   - 列出存储过程及参数签名
- `execute_procedure`
   - 执行存储过程（支持多结果集）
- `append_insight`
   - 向备忘录添加业务洞察

## 资源

- `memo://insights` — 实时更新的业务洞察备忘录，通过 `append_insight` 追加
- `schema://{schema}/{table}` — 默认数据库的表结构（JSON：列 / 主键 / 自增 / 外键 / 索引）
- `schema://{database}/{schema}/{table}` — 指定数据库连接的表结构

资源模板按需拉取：与工具 schema 不同，它不会每轮对话都重发给 LLM，长会话更省 token。

## 安全性

所有 SQL 在执行前均经过静态校验：

- `read_query` 仅接受单条 SELECT / WITH 语句，拒绝任何写操作或 EXEC 关键字（可拦截 `WITH c AS (...) DELETE FROM t` 这类注入）
- `write_query` 采用白名单（仅 INSERT / UPDATE / DELETE / MERGE）；`EXEC`、`DROP`、`TRUNCATE`、`ALTER` 均被拒绝
- 拒绝分号分隔的多语句批处理
- `trusted_connection: true` 时切换为 Windows 集成认证（连接串拼 `Trusted_Connection=yes`，无需账号密码）；保持 `false` 则使用配置中的账号密码登录
- `readonly: true` 时禁用一切写操作（含存储过程——它是可能写库的黑盒）
- 存储过程名严格校验为 1~3 段点分标识符后才拼入 `{CALL ...}`
- 结果超过 `max_rows` 行自动截断；查询超过 `query_timeout` 秒自动中止
- `confirm_writes: true` 时所有状态变更操作（`write_query` 写入、`create_table` 建表、`execute_procedure` 存储过程）先通过 MCP elicitation 向用户二次确认

  > ⚠️ **重要限制**：二次确认依赖客户端实现 MCP Elicitation 协议。**不支持该协议的客户端（如 TraeWork / Cursor ...）会跳过确认直接执行写操作**，即 `confirm_writes: true` 在此类客户端上不产生任何保护效果（服务器日志会记录跳过警告）。若需要在任何客户端上都获得可靠的写保护，请改用 `readonly: true`（硬阻断一切写入，与客户端能力无关）
- 校验前先剥离注释与字符串字面量，无法通过注释或字符串绕过检查

## 演示
数据库表如下，列名不规范，AI会自行匹配，执行Sql过程中出错会自纠正。

![Table](imgs/table.png)

以下是视频演示。

![Demo](imgs/demo.gif)

## 运行环境

- `Python 3.10+`
- `包`
   - pyodbc>=4.0.39
   - pydantic>=2.0.0
   - mcp>=1.9.0,<2.0.0
- `ODBC Driver 17 / 18 for SQL Server`

## 使用

### 安装依赖包

源码方式：

```bash
git clone <本仓库>
CD /d ~/mssql-mcp  
pip install -r requirements.txt  
```

或按包安装（pip >= 21.3）：

```bash
pip install .
```

### 配置文件

在 server.py 同目录创建 `config.json`。单库格式：

```json
{
    "database": {
        "driver": "ODBC Driver 17 for SQL Server",
        "server": "你的服务器ip",
        "database": "数据库名",
        "username": "账号",
        "password": "密码",
        "trusted_connection": false,
        "readonly": false,
        "query_timeout": 30,
        "max_rows": 200,
        "confirm_writes": false
    },
    "server": {
        "name": "mssql-manager",
        "version": "0.2.0"
    }
}
```

多库格式（推荐 —— 每个连接独立配置，例如生产库只读）：

```json
{
    "databases": {
        "default": { "server": "localhost", "database": "dev_db", "...": "..." },
        "prod":    { "server": "10.0.0.5", "database": "prod_db", "readonly": true, "...": "..." }
    },
    "default_database": "default",
    "server": { "name": "mssql-manager", "version": "0.2.0" }
}
```

**连接名与默认连接**：`databases` 字典里的键名就是连接的"名字"，AI 调用工具时通过 `database` 参数传入该名字切换连接，**键名是任意的**（不强制叫 `default`）。`default_database` 决定不带 `database` 参数时使用哪个连接，按以下顺序解析：

1. 配置中显式写了 `default_database: "xxx"` → 使用该名字指向的连接
2. 字典中存在名为 `default` 的连接 → 使用它
3. 都没有 → 使用字典中第一个连接

因此推荐写法是显式指定 `default_database`，并在 `databases` 中同时保留一个名为 `default` 的连接作为双保险（即使加新连接时排到了第一个，默认也不会被"顶掉"）。

配置文件查找顺序：`MSSQL_MCP_CONFIG` 环境变量 → server.py 同目录的 `config.json` → 当前工作目录的 `config.json`。

可选配置项（每个连接独立）：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `readonly` | `false` | 只读模式：禁用一切写操作（含存储过程） |
| `query_timeout` | `30` | 查询超时（秒） |
| `max_rows` | `200` | 单个结果集最大返回行数，超出部分截断 |
| `confirm_writes` | `false` | 所有状态变更操作（写入 / 建表 / 存储过程）前通过 Elicitation 向用户二次确认；**需客户端支持 Elicitation，不支持的客户端（如 TraeWork / Cursor）会直接执行，硬保护请用 `readonly`** |
| `encrypt` | （不设置） | ODBC 加密开关，适配 Driver 18（`true` / `false`） |
| `trust_server_certificate` | `false` | 信任自签名证书，Driver 18 连旧服务器时常用 |

环境变量：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `MSSQL_MCP_CONFIG` | （不设置） | 指定替代的配置文件路径 |
| `MSSQL_MCP_LOG_LEVEL` | `INFO` | 日志级别（`DEBUG` / `INFO` / `WARNING` / `ERROR`） |

### 客户端配置（Claude Desktop / Cursor / Windsurf 等）

主流 stdio 客户端（Claude Desktop、Cursor、Windsurf、Cline 等）均使用相同的 `mcpServers` JSON 格式。此处以 Claude Desktop 为例，其他客户端将同样的条目添加到各自的 MCP 配置文件中即可：

```bash
# 添加到 claude_desktop_config.json。注意路径替换为你的实际路径  
{
    "mcpServers": {
        "mssql": {
            "command": "python",
            "args": [
                # 你的实际路径，如："C:\\mssql-mcp\\src\\server.py"
                "~/server.py"
            ]
        }
    }
}
```

若以包方式安装，command 可直接写 `mssql-mcp`（无需 args）。

### MCP Inspector

```bash
# 注意路径替换为你的实际路径  
npx -y @modelcontextprotocol/inspector python C:\\mssql-mcp\\src\\server.py
```

### 运行测试

```bash
pip install pytest
python -m pytest tests -q
```

- `tests/test_validation.py` — SQL / 存储过程名校验（无需数据库）
- `tests/test_config.py` — 配置解析：单库向后兼容、多库、错误场景（无需数据库）
- `tests/test_integration.py` — 使用 `src/config.json` 的数据库做端到端验证（写入仅涉及专用的 `mcp_upgrade_*` 测试对象，测试后自动清理）

## 项目结构

```
mssql-mcp
├── .git
├── .gitignore
├── LICENSE
├── README.md
├── README_en.md
├── README_zh.md
├── imgs
│   ├── table.png
│   └── demo.gif
├── pyproject.toml        （打包配置：src/ 安装为 mssql_mcp 包）
├── requirements.txt
├── src
│   ├── __init__.py
│   ├── config.json      （已被 gitignore，本地数据库配置）
│   └── server.py
└── tests
    ├── test_validation.py
    ├── test_config.py
    └── test_integration.py
```

## License

MIT License
