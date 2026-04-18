"""
Shared fixtures for mock tests.

Provides: fake JWKS keypair, JWT minting factory, mock MongoDB, test FastAPI app.
"""

import time
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from mcp_core import MCPCore
from mcp_core.auth import LogtoAuth
from mcp_core.billing import StripeBilling


# ── RSA keypair for signing test JWTs ─────────────────────

@pytest.fixture
def rsa_private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def rsa_public_key(rsa_private_key):
    return rsa_private_key.public_key()


@pytest.fixture
def rsa_public_jwk(rsa_public_key):
    """Public key as JWK dict (for mocking JWKS endpoint)."""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    # We'll use the PEM for PyJWKClient mocking
    return rsa_public_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)


# ── JWT minting factory ──────────────────────────────────

@pytest.fixture
def make_token(rsa_private_key):
    """Factory: mint a JWT with custom claims."""

    def _make(
        sub: str = "user_test_123",
        email: str = "test@example.com",
        aud: str = "https://api.test.app",
        iss: str = "https://test.logto.app/oidc",
        expired: bool = False,
        expires_in: int = 3600,
        client_id: str = "",
        **extra,
    ) -> str:
        now = int(time.time())
        payload: Dict[str, Any] = {
            "sub": sub,
            "email": email,
            "aud": aud,
            "iss": iss,
            "iat": now,
            "exp": now + (-3600 if expired else expires_in),
            **extra,
        }
        if client_id:
            payload["client_id"] = client_id
        pem = rsa_private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        return pyjwt.encode(payload, pem, algorithm="RS256")

    return _make


# ── Mock MongoDB ──────────────────────────────────────────

@pytest.fixture
def mock_db():
    """In-memory MongoDB mock using mongomock-motor."""
    try:
        from mongomock_motor import AsyncMongoMockClient
        client = AsyncMongoMockClient()
        return client["test_db"]
    except ImportError:
        pytest.skip("mongomock-motor not installed")


# ── LogtoAuth with mocked JWKS ───────────────────────────

@pytest.fixture
def auth(rsa_public_key):
    """LogtoAuth instance with mocked JWKS client."""
    instance = LogtoAuth(
        endpoint="https://test.logto.app",
        api_resource="https://api.test.app",
        free_credits=10,
        dev_bypass=True,
        read_only_tools={"free_tool"},
    )
    # Mock the JWKS client to return our test key
    mock_jwks = MagicMock()
    mock_signing_key = MagicMock()
    mock_signing_key.key = rsa_public_key
    mock_jwks.get_signing_key_from_jwt.return_value = mock_signing_key
    instance._jwks_client = mock_jwks
    instance._jwks_last_init = time.time()
    return instance


# ── StripeBilling ─────────────────────────────────────────

@pytest.fixture
def billing():
    return StripeBilling(
        stripe_secret_key="sk_test_fake",
        price_id="price_fake",
        meter_event="test_tool_calls",
        free_credits=10,
        tool_costs={"free_tool": 0, "paid_tool": 3, "expensive_tool": 8},
        read_only_tools={"free_tool"},
        success_url="https://test.app/billing/success",
    )


# ── Mock Stripe module ───────────────────────────────────

@pytest.fixture
def mock_stripe():
    """Patch stripe module. Returns list of recorded calls."""
    calls = []

    class FakeSession:
        url = "https://checkout.stripe.com/fake_session_123"

        @classmethod
        def create(cls, **kwargs):
            calls.append(("checkout.Session.create", kwargs))
            return cls()

    class FakeCheckout:
        Session = FakeSession

    class FakeMeterEvent:
        @classmethod
        def create(cls, **kwargs):
            calls.append(("billing.MeterEvent.create", kwargs))

    class FakeBilling:
        MeterEvent = FakeMeterEvent

    class FakeWebhook:
        @classmethod
        def construct_event(cls, payload, sig, secret):
            import json
            calls.append(("Webhook.construct_event", {}))
            return json.loads(payload)

    class FakeStripe:
        api_key = None
        checkout = FakeCheckout
        billing = FakeBilling
        Webhook = FakeWebhook

    return FakeStripe(), calls


# ── MCPCore with everything mocked ────────────────────────

@pytest.fixture
def core(auth, mock_db, mock_stripe):
    """Fully wired MCPCore with mock DB and mock Stripe."""
    fake_stripe, stripe_calls = mock_stripe
    instance = MCPCore(
        product_name="test-product",
        logto_endpoint="https://test.logto.app",
        logto_api_resource="https://api.test.app",
        free_credits=10,
        dev_auth_bypass=True,
        tool_costs={"free_tool": 0, "paid_tool": 3, "expensive_tool": 8},
        read_only_tools={"free_tool"},
    )
    instance.auth = auth
    instance.db = mock_db
    instance.billing._stripe = fake_stripe
    instance._stripe_calls = stripe_calls
    return instance


# ── FastAPI test app ──────────────────────────────────────

@pytest.fixture
def app(core):
    """FastAPI app with test routes wired to MCPCore."""
    app = FastAPI()
    core.install_routes(app)

    from fastapi import APIRouter
    router = APIRouter(prefix="/api/mcp", tags=["mcp"])

    @router.post("/free_tool")
    async def free_tool(request: Request):
        user = await core.auth_and_bill(request, "free_tool")
        t0 = time.time()
        await core.log_tool_call(request, "free_tool", user=user, duration_ms=10)
        return {"result": "ok", "user_id": user.get("logto_user_id", "")}

    @router.post("/paid_tool")
    async def paid_tool(request: Request):
        user = await core.auth_and_bill(request, "paid_tool")
        await core.log_tool_call(request, "paid_tool", user=user, duration_ms=50)
        return {
            "result": "ok",
            "user_id": user["logto_user_id"],
            "billing": user.get("_billing"),
        }

    app.include_router(router)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def auth_headers(make_token):
    return {"Authorization": f"Bearer {make_token()}"}


@pytest.fixture
def dev_headers():
    return {"Authorization": "Bearer dev-bypass"}
