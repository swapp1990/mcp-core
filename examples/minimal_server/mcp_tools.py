"""
Example MCP tools: one free, one paid.
"""

import time

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

# Import core from main (in a real app, use dependency injection or module-level)
from main import core

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

# Register tool costs
core.billing.tool_costs.update({
    "list_items": 0,
    "generate_item": 5,
})
core.auth.read_only_tools.add("list_items")
core.billing.read_only_tools.add("list_items")


class ListItemsInput(BaseModel):
    category: str = Field(None, description="Optional category filter.")


class GenerateItemInput(BaseModel):
    prompt: str = Field(..., min_length=3, description="What to generate.")


@router.post("/list_items", operation_id="list_items",
             summary="List items in the catalog (free, no auth).")
async def list_items(inp: ListItemsInput, request: Request):
    user = await core.auth_and_bill(request, "list_items")
    return {"items": [{"id": 1, "name": "Example Item"}]}


@router.post("/generate_item", operation_id="generate_item",
             summary="Generate an item (costs 5 credits).")
async def generate_item(inp: GenerateItemInput, request: Request):
    t0 = time.time()
    user = await core.auth_and_bill(request, "generate_item")

    # Your product logic here
    result = {"id": 42, "name": f"Generated: {inp.prompt[:50]}"}

    duration = int((time.time() - t0) * 1000)
    await core.log_tool_call(
        request, "generate_item", user=user,
        duration_ms=duration, meta={"prompt_len": len(inp.prompt)},
    )
    return result
