"""
Minimal MCP server using mcp-core.

Run:
    pip install mcp-core fastapi-mcp uvicorn
    uvicorn main:app --reload --port 8001
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from mcp_core import MCPCore
from mcp_tools import router as mcp_router

core = MCPCore(
    product_name="my-product",
    # All other config comes from MCP_CORE_* env vars or .env
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await core.connect_db()
    core.health.add_check("db", lambda: core.db.command("ping") if core.db else None)
    yield


app = FastAPI(title="My MCP Server", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(mcp_router)
core.install_routes(app)

# Optional: wire up fastapi-mcp for Claude Code
try:
    from fastapi_mcp import FastApiMCP

    auth_config = core.mcp_auth_config()
    mcp = FastApiMCP(
        app,
        name=core.product_name,
        description="My MCP-first server.",
        include_tags=["mcp"],
        auth_config=auth_config,
    )
    mcp.mount(mount_path="/mcp")
except ImportError:
    pass  # fastapi-mcp not installed
