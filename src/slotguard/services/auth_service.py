"""Authentication, JWT issuance, and refresh token rotation with reuse detection."""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from slotguard.core.clock import Clock
from slotguard.core.config import Settings
from slotguard.core.exceptions import ConflictError, UnauthorizedError
from slotguard.core.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_token,
    verify_password,
)
from slotguard.models.user import RefreshToken, User, UserRole
from slotguard.repositories.refresh_token_repository import RefreshTokenRepository
from slotguard.repositories.user_repository import UserRepository
from slotguard.schemas.auth import AuthTokenResponse, LoginRequest, RegisterRequest, UserResponse


class AuthService:
    """Business logic for user registration, login, token rotation, and logout."""

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        clock: Clock,
    ) -> None:
        self._session = session
        self._settings = settings
        self._clock = clock
        self._users = UserRepository(session)
        self._tokens = RefreshTokenRepository(session)

    async def _issue_token_pair(
        self,
        user: User,
        *,
        token_family: str | None = None,
    ) -> tuple[AuthTokenResponse, RefreshToken]:
        now = self._clock.now()
        family = token_family or str(uuid4())
        raw_refresh = generate_refresh_token()
        refresh_record = RefreshToken(
            user_id=user.id,
            token_hash=hash_token(raw_refresh),
            token_family=family,
            expires_at=now + timedelta(seconds=self._settings.refresh_token_ttl_seconds),
            created_at=now,
        )
        await self._tokens.create(refresh_record)

        access_token = create_access_token(
            user_id=user.id,
            email=user.email,
            role=user.role,
            settings=self._settings,
            clock=self._clock,
        )
        response = AuthTokenResponse(
            access_token=access_token,
            refresh_token=raw_refresh,
            expires_in=self._settings.access_token_ttl_seconds,
            user=UserResponse.model_validate(user),
        )
        return response, refresh_record

    async def register(self, payload: RegisterRequest) -> AuthTokenResponse:
        """Register a new MEMBER user and return an initial token pair."""
        normalized_email = payload.email.lower().strip()
        existing = await self._users.get_by_email(normalized_email)
        if existing is not None:
            raise ConflictError("An account with this email already exists.")

        user = User(
            email=normalized_email,
            password_hash=hash_password(payload.password),
            full_name=payload.full_name.strip(),
            role=UserRole.MEMBER.value,
            is_active=True,
            created_at=self._clock.now(),
        )
        try:
            await self._users.create(user)
            response, _ = await self._issue_token_pair(user)
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise ConflictError("An account with this email already exists.") from exc
        return response

    async def login(self, payload: LoginRequest) -> AuthTokenResponse:
        """Authenticate credentials and issue a fresh token family."""
        user = await self._users.get_by_email(payload.email)
        if user is None or not verify_password(payload.password, user.password_hash):
            raise UnauthorizedError("Invalid email or password.")
        if not user.is_active:
            raise UnauthorizedError("User account is inactive.")

        response, _ = await self._issue_token_pair(user)
        await self._session.commit()
        return response

    async def refresh(self, raw_refresh_token: str) -> AuthTokenResponse:
        """Rotate a valid refresh token and revoke the whole token family on reuse."""
        now = self._clock.now()
        stored = await self._tokens.get_by_hash(hash_token(raw_refresh_token))
        if stored is None:
            raise UnauthorizedError("Invalid refresh token.")

        if stored.revoked_at is not None:
            await self._tokens.revoke_family(stored.token_family, now)
            await self._session.commit()
            raise UnauthorizedError("Refresh token reuse detected. Token family revoked.")

        if stored.expires_at <= now:
            stored.revoked_at = now
            await self._session.commit()
            raise UnauthorizedError("Refresh token has expired.")

        user = await self._users.get_by_id(stored.user_id)
        if user is None or not user.is_active:
            await self._tokens.revoke_family(stored.token_family, now)
            await self._session.commit()
            raise UnauthorizedError("User account is unavailable.")

        response, successor = await self._issue_token_pair(user, token_family=stored.token_family)
        stored.revoked_at = now
        stored.replaced_by_token_id = successor.id
        await self._session.commit()
        return response

    async def logout(self, raw_refresh_token: str) -> None:
        """Revoke the token family associated with the given refresh token."""
        stored = await self._tokens.get_by_hash(hash_token(raw_refresh_token))
        if stored is None:
            return
        await self._tokens.revoke_family(stored.token_family, self._clock.now())
        await self._session.commit()
