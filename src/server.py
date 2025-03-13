import os
import sys
import json
import pyodbc
import logging
from contextlib import closing
from pathlib import Path
from mcp.server.models import InitializationOptions
import mcp.types as types
from mcp.server import NotificationOptions, Server
import mcp.server.stdio
from pydantic import AnyUrl
from typing import Any

# 配置Windows环境下的UTF-8编码
if sys.platform == "win32" and os.environ.get('PYTHONIOENCODING') is None:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

logger = logging.getLogger('mcp_mssql_server')
logger.info("Starting MCP MSSQL Server")

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

class Config:
    def __init__(self):
        # 修改配置文件路径为当前目录
        self.config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
        self.load_config()

    def load_config(self):
        """加载配置文件"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self.config = json.load(f)
                logger.info("配置文件加载成功")
                logger.debug(f"数据库配置: {self.config['database']}")
        except Exception as e:
            logger.error(f"加载配置文件失败: {e}")
            raise

    @property
    def connection_string(self) -> str:
        """构建数据库连接字符串"""
        db_config = self.config['database']
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

        return ";".join(conn_parts)

    @property
    def server_name(self) -> str:
        return self.config['server']['name']

    @property
    def server_version(self) -> str:
        return self.config['server']['version']

class MssqlDatabase:
    def __init__(self, config: Config):
        self.config = config
        self._init_database()
        self.insights: list[str] = []

    def _init_database(self):
        """初始化数据库连接"""
        logger.debug("初始化数据库连接")
        try:
            conn = pyodbc.connect(self.config.connection_string)
            conn.close()
            logger.debug("数据库连接测试成功")
        except Exception as e:
            logger.error(f"数据库连接初始化失败: {e}")
            raise

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

    def _execute_query(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """执行SQL查询并返回结果字典列表"""
        logger.debug(f"执行查询: {query}")
        try:
            with closing(pyodbc.connect(self.config.connection_string)) as conn:
                with closing(conn.cursor()) as cursor:
                    if params:
                        cursor.execute(query, params)
                    else:
                        cursor.execute(query)

                    if query.strip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'CREATE', 'DROP', 'ALTER')):
                        conn.commit()
                        affected = cursor.rowcount
                        logger.debug(f"写入查询影响了 {affected} 行")
                        return [{"affected_rows": affected}]

                    columns = [column[0] for column in cursor.description] if cursor.description else []
                    results = [dict(zip(columns, row)) for row in cursor.fetchall()]
                    logger.debug(f"读取查询返回了 {len(results)} 行")
                    return results

        except Exception as e:
            logger.error(f"数据库执行查询时出错: {e}")
            raise

async def main():
    """主入口函数"""
    logger.info("启动 MSSQL MCP 服务器")

    # 加载配置
    config = Config()
    db = MssqlDatabase(config)
    server = Server(config.server_name)

    # 注册处理程序
    logger.debug("注册处理程序")

    @server.list_resources()
    async def handle_list_resources() -> list[types.Resource]:
        logger.debug("处理 list_resources 请求")
        return [
            types.Resource(
                uri=AnyUrl("memo://insights"),
                name="业务洞察备忘录",
                description="一个实时更新的业务洞察文档",
                mimeType="text/plain",
            )
        ]

    @server.read_resource()
    async def handle_read_resource(uri: AnyUrl) -> str:
        logger.debug(f"处理 read_resource 请求，URI: {uri}")
        if uri.scheme != "memo":
            logger.error(f"不支持的 URI 协议: {uri.scheme}")
            raise ValueError(f"不支持的 URI 协议: {uri.scheme}")

        path = str(uri).replace("memo://", "")
        if not path or path != "insights":
            logger.error(f"未知的资源路径: {path}")
            raise ValueError(f"未知的资源路径: {path}")

        return db._synthesize_memo()

    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        """列出可用工具"""
        return [
            types.Tool(
                name="read_query",
                description="在 MSSQL 数据库上执行 SELECT 查询",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "要执行的 SELECT SQL 查询"},
                    },
                    "required": ["query"],
                },
            ),
            types.Tool(
                name="write_query",
                description="在 MSSQL 数据库上执行 INSERT、UPDATE 或 DELETE 查询",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "要执行的 SQL 查询"},
                    },
                    "required": ["query"],
                },
            ),
            types.Tool(
                name="create_table",
                description="在 MSSQL 数据库中创建新表",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "CREATE TABLE SQL 语句"},
                    },
                    "required": ["query"],
                },
            ),
            types.Tool(
                name="list_tables",
                description="列出 MSSQL 数据库中的所有表",
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
            types.Tool(
                name="describe_table",
                description="获取特定表的架构信息",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "table_name": {"type": "string", "description": "要描述的表名"},
                    },
                    "required": ["table_name"],
                },
            ),
            types.Tool(
                name="append_insight",
                description="向备忘录添加业务洞察",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "insight": {"type": "string", "description": "从数据分析中发现的业务洞察"},
                    },
                    "required": ["insight"],
                },
            ),
        ]

    @server.call_tool()
    async def handle_call_tool(
        name: str, arguments: dict[str, Any] | None
    ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
        """处理工具执行请求"""
        try:
            if name == "list_tables":
                results = db._execute_query(
                    """
                    SELECT TABLE_NAME as name 
                    FROM INFORMATION_SCHEMA.TABLES 
                    WHERE TABLE_TYPE = 'BASE TABLE'
                    """
                )
                return [types.TextContent(type="text", text=str(results))]

            elif name == "describe_table":
                if not arguments or "table_name" not in arguments:
                    raise ValueError("缺少 table_name 参数")
                results = db._execute_query(
                    """
                    SELECT 
                        COLUMN_NAME as name,
                        DATA_TYPE as type,
                        IS_NULLABLE as nullable,
                        COLUMN_DEFAULT as default_value
                    FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_NAME = ?
                    """,
                    {"table_name": arguments['table_name']}
                )
                return [types.TextContent(type="text", text=str(results))]

            elif name == "append_insight":
                if not arguments or "insight" not in arguments:
                    raise ValueError("缺少 insight 参数")

                db.insights.append(arguments["insight"])
                _ = db._synthesize_memo()

                # 通知客户端备忘录资源已更新
                await server.request_context.session.send_resource_updated(AnyUrl("memo://insights"))

                return [types.TextContent(type="text", text="洞察已添加到备忘录")]

            if not arguments:
                raise ValueError("缺少参数")

            if name == "read_query":
                if not arguments["query"].strip().upper().startswith("SELECT"):
                    raise ValueError("read_query 只允许 SELECT 查询")
                results = db._execute_query(arguments["query"])
                return [types.TextContent(type="text", text=str(results))]

            elif name == "write_query":
                if arguments["query"].strip().upper().startswith("SELECT"):
                    raise ValueError("write_query 不允许 SELECT 查询")
                results = db._execute_query(arguments["query"])
                return [types.TextContent(type="text", text=str(results))]

            elif name == "create_table":
                if not arguments["query"].strip().upper().startswith("CREATE TABLE"):
                    raise ValueError("只允许 CREATE TABLE 语句")
                db._execute_query(arguments["query"])
                return [types.TextContent(type="text", text="表创建成功")]

            else:
                raise ValueError(f"未知工具: {name}")

        except pyodbc.Error as e:
            return [types.TextContent(type="text", text=f"数据库错误: {str(e)}")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"错误: {str(e)}")]

    @server.list_prompts()
    async def handle_list_prompts() -> list[types.Prompt]:
        logger.debug("处理 list_prompts 请求")
        return [
            types.Prompt(
                name="mcp-demo",
                description="一个用于在 MSSQL 数据库中创建初始数据并演示 MSSQL MCP 服务器功能的提示",
                arguments=[
                    types.PromptArgument(
                        name="topic",
                        description="用于生成初始数据的主题",
                        required=True,
                    )
                ],
            )
        ]

    @server.get_prompt()
    async def handle_get_prompt(name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
        logger.debug(f"处理 get_prompt 请求，名称: {name}，参数: {arguments}")
        if name != "mcp-demo":
            logger.error(f"未知的提示: {name}")
            raise ValueError(f"未知的提示: {name}")

        if not arguments or "topic" not in arguments:
            logger.error("缺少必需的参数: topic")
            raise ValueError("缺少必需的参数: topic")

        topic = arguments["topic"]
        prompt = PROMPT_TEMPLATE.format(topic=topic)

        logger.debug(f"为主题 {topic} 生成提示模板")
        return types.GetPromptResult(
            description=f"主题 {topic} 的演示模板",
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(type="text", text=prompt.strip()),
                )
            ],
        )

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        logger.info("服务器正在使用 stdio 传输运行")
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name=config.server_name,
                server_version=config.server_version,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
