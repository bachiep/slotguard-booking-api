"""Health check, OpenAPI security scheme, and RFC 7807 error formatting tests."""

from fastapi import APIRouter
from httpx import AsyncClient

from slotguard.core.exceptions import ConflictError


async def test_health_endpoint_returns_database_status(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["service"] == "SlotGuard Booking API"
    assert payload["environment"] == "test"
    assert payload["database_dialect"] == "sqlite"
    assert payload["rate_limit_backend"] == "memory"
    assert payload["timestamp"].startswith("2026-12-01T08:00:00")


async def test_unknown_route_returns_rfc7807_problem_detail(client: AsyncClient) -> None:
    response = await client.get("/api/v1/nonexistent")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == 404
    assert body["title"] == "Not Found"
    assert body["instance"] == "/api/v1/nonexistent"
    assert "timestamp" in body


async def test_app_error_renders_problem_detail(app, client: AsyncClient) -> None:
    test_router = APIRouter()

    @test_router.get("/api/v1/_test-conflict")
    async def trigger_conflict() -> None:
        raise ConflictError("Slot is already held.")

    app.include_router(test_router)

    response = await client.get("/api/v1/_test-conflict")

    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["type"] == "https://slotguard.dev/errors/conflict"
    assert body["title"] == "Conflict"
    assert body["detail"] == "Slot is already held."


async def test_openapi_exposes_bearer_auth_scheme(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    bearer = schema["components"]["securitySchemes"]["BearerAuth"]
    assert bearer["type"] == "http"
    assert bearer["scheme"] == "bearer"
    assert bearer["bearerFormat"] == "JWT"
