"""Version 1 API router aggregation."""

from fastapi import APIRouter

from slotguard.api.v1.auth import router as auth_router
from slotguard.api.v1.bookings import router as bookings_router
from slotguard.api.v1.health import router as health_router
from slotguard.api.v1.resources import router as resources_router
from slotguard.api.v1.webhooks import router as webhooks_router

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(health_router)
api_v1_router.include_router(auth_router)
api_v1_router.include_router(resources_router)
api_v1_router.include_router(bookings_router)
api_v1_router.include_router(webhooks_router)
