"""Pydantic schemas for authentication and user identity endpoints."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class RegisterRequest(BaseModel):
    """Payload to register a new MEMBER account."""

    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=2, max_length=120)


class LoginRequest(BaseModel):
    """Credentials payload for password authentication."""

    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    """Payload carrying an opaque refresh token for rotation or revocation."""

    refresh_token: str = Field(min_length=16, max_length=512)


class UserResponse(BaseModel):
    """Public representation of a user profile."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    full_name: str
    role: str
    is_active: bool
    created_at: datetime


class AuthTokenResponse(BaseModel):
    """Token pair response returned upon registration, login, or refresh."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserResponse
