"""Resource CRUD and daily availability query endpoints."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from slotguard.api.deps import get_app_clock, require_roles
from slotguard.core.clock import Clock
from slotguard.core.database import get_db_session
from slotguard.models.resource import ResourceType
from slotguard.models.user import User, UserRole
from slotguard.schemas.resource import (
    PaginatedResourcesResponse,
    ResourceAvailabilityResponse,
    ResourceCreateRequest,
    ResourceResponse,
    ResourceUpdateRequest,
)
from slotguard.services.resource_service import ResourceService

router = APIRouter(prefix="/resources", tags=["Resources"])


@router.post(
    "",
    response_model=ResourceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a bookable resource (OWNER)",
)
async def create_resource(
    payload: ResourceCreateRequest,
    owner: Annotated[User, Depends(require_roles(UserRole.OWNER))],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    clock: Annotated[Clock, Depends(get_app_clock)],
) -> ResourceResponse:
    """Create a new bookable resource with operating hours and slot duration."""
    return await ResourceService(session=session, clock=clock).create_resource(owner, payload)


@router.get(
    "",
    response_model=PaginatedResourcesResponse,
    summary="List bookable resources",
)
async def list_resources(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    clock: Annotated[Clock, Depends(get_app_clock)],
    resource_type: ResourceType | None = None,
    active_only: bool = True,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaginatedResourcesResponse:
    """Return a paginated list of resources filtered by type and active status."""
    return await ResourceService(session=session, clock=clock).list_resources(
        resource_type=resource_type.value if resource_type is not None else None,
        active_only=active_only,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{resource_id}",
    response_model=ResourceResponse,
    summary="Get resource details",
)
async def get_resource(
    resource_id: int,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    clock: Annotated[Clock, Depends(get_app_clock)],
) -> ResourceResponse:
    """Retrieve metadata and operating window for a single resource."""
    return await ResourceService(session=session, clock=clock).get_resource(resource_id)


@router.patch(
    "/{resource_id}",
    response_model=ResourceResponse,
    summary="Update a resource (OWNER)",
)
async def update_resource(
    resource_id: int,
    payload: ResourceUpdateRequest,
    owner: Annotated[User, Depends(require_roles(UserRole.OWNER))],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    clock: Annotated[Clock, Depends(get_app_clock)],
) -> ResourceResponse:
    """Partially update a resource owned by the authenticated OWNER."""
    return await ResourceService(session=session, clock=clock).update_resource(
        resource_id, owner, payload
    )


@router.get(
    "/{resource_id}/availability",
    response_model=ResourceAvailabilityResponse,
    summary="Query daily available slots for a resource",
)
async def get_resource_availability(
    resource_id: int,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    clock: Annotated[Clock, Depends(get_app_clock)],
    target_date: date = Query(alias="date"),
) -> ResourceAvailabilityResponse:
    """Return discrete UTC slots on the given date with live availability flags."""
    return await ResourceService(session=session, clock=clock).get_availability(
        resource_id, target_date
    )
