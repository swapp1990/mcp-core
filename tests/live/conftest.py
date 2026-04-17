"""
Live test fixtures -- real Logto, real MongoDB, real Stripe (test mode).

Gated by RUN_LIVE_TESTS=1. Requires tests/.env.live with real credentials.

Auth approach: uses M2M client_credentials grant (no browser, no refresh tokens).
The test MCPCore sets reject_m2m=False so M2M tokens are accepted.
"""

import os
import pathlib

import pytest

# Skip all tests in this directory unless RUN_LIVE_TESTS=1
pytestmark = pytest.mark.live


def pytest_configure(config):
    env_file = pathlib.Path(__file__).parent.parent / ".env.live"
    if env_file.exists():
        from dotenv import load_dotenv
        load_dotenv(env_file)


def pytest_collection_modifyitems(config, items):
    if not os.getenv("RUN_LIVE_TESTS"):
        skip = pytest.mark.skip(reason="RUN_LIVE_TESTS not set")
        for item in items:
            if "live" in str(item.fspath):
                item.add_marker(skip)


@pytest.fixture(scope="session")
def live_env():
    """Verify required env vars are present."""
    required = [
        "LOGTO_ENDPOINT", "LOGTO_API_RESOURCE",
        "MONGODB_URI",
    ]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        pytest.skip(f"Missing env vars: {', '.join(missing)}")

    return {k: os.environ[k] for k in required}


@pytest.fixture
def live_core(live_env):
    """MCPCore wired to real services.

    reject_m2m=False so M2M tokens from client_credentials grant are accepted.
    """
    from mcp_core import MCPCore

    return MCPCore(
        product_name="mcp-core-live-test",
        logto_endpoint=os.environ["LOGTO_ENDPOINT"],
        logto_api_resource=os.environ["LOGTO_API_RESOURCE"],
        mongodb_uri=os.environ["MONGODB_URI"],
        db_name=os.environ.get("MONGO_DB_NAME", "mcp_core_test"),
        stripe_secret_key=os.environ.get("STRIPE_SECRET_KEY", ""),
        stripe_price_id=os.environ.get("STRIPE_PRICE_ID", ""),
        stripe_meter_event=os.environ.get("STRIPE_METER_EVENT_NAME", "test_tool_calls"),
        free_credits=10,
        tool_costs={"free_tool": 0, "paid_tool": 2},
        read_only_tools={"free_tool"},
        reject_m2m=False,  # Accept M2M tokens for testing
        billing_success_url="https://test.app/billing/success",
    )


@pytest.fixture
async def live_db(live_core):
    """Real MongoDB connection."""
    db = await live_core.connect_db()
    yield db
    try:
        if db is not None:
            await db["users"].delete_many({"logto_user_id": {"$regex": "^mcp-core-test-"}})
            await db["tool_logs"].delete_many({"product": "mcp-core-live-test"})
    except RuntimeError:
        pass  # Event loop closed by TestClient -- harmless for test DB


@pytest.fixture
async def live_user_token():
    """Mint a real Logto access token via client_credentials grant.

    Uses M2M app credentials -- no browser, no refresh tokens, fully automated.
    Token is minted fresh every test (they're short-lived, ~1h).
    """
    app_id = os.getenv("M2M_LOGTO_APP_ID")
    app_secret = os.getenv("M2M_LOGTO_APP_SECRET")
    endpoint = os.getenv("LOGTO_ENDPOINT")
    resource = os.getenv("LOGTO_API_RESOURCE")

    if not all([app_id, app_secret, endpoint, resource]):
        pytest.skip("M2M_LOGTO_APP_ID / M2M_LOGTO_APP_SECRET not configured")

    import httpx

    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.post(
            f"{endpoint}/oidc/token",
            data={
                "grant_type": "client_credentials",
                "client_id": app_id,
                "client_secret": app_secret,
                "resource": resource,
                "scope": "openid",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    if resp.status_code != 200:
        pytest.fail(f"M2M token mint failed: {resp.status_code} {resp.text[:200]}")

    data = resp.json()
    return data["access_token"]
