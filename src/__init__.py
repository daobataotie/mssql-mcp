from . import server
import asyncio
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger('mcp_mssql_server')

def main():
    """MSSQL MCP服务的主入口点"""
    try:
        # 运行服务器
        asyncio.run(server.main())
    except Exception as e:
        logger.error(f"服务启动失败: {str(e)}")
        sys.exit(1)

# 包级别导出
__all__ = ["main", "server"]