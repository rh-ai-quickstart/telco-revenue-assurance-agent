"""Durable, per-session chat request idempotency."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from pydantic import Field
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from common.errors import ErrorCode, ServiceError, UpstreamError
from common.models import CommonModel

from ..db.models import ChatRequest
from .ogx_client import OgxRequestRejected, OgxResponsesClient

logger = logging.getLogger(__name__)


class ChatMessageRequest(CommonModel):
    request_id: UUID = Field(
        description="Idempotency identifier scoped to session_id.",
        examples=["f28fb20a-03b3-4d71-8a5a-6a29ab5cf93c"],
    )
    message: str = Field(
        min_length=1,
        max_length=4000,
        description=(
            "Analyst question; arbitrary SQL and endpoint URLs are not interpreted as tool "
            "configuration."
        ),
        examples=["Which customers have high churn class scores in the latest run?"],
    )


class ChatMessageResponse(CommonModel):
    session_id: UUID = Field(
        description="Session specified in the request path.",
        examples=["8239f3a1-94f0-4e9a-9179-2a7b10ef45b8"],
    )
    request_id: UUID = Field(
        description="Request id echoed for retry deduplication.",
        examples=["f28fb20a-03b3-4d71-8a5a-6a29ab5cf93c"],
    )
    response_id: str = Field(
        description="OGX response identifier used for correlation.",
        examples=["resp_01J8Z2N5M9K7"],
    )
    assistant_message: str = Field(
        description="Final assistant text after any allowlisted MCP tool calls.",
        examples=["I found 18 customers in churn class 1 for the latest run."],
    )
    created_at: datetime = Field(
        description="UTC response completion time.",
        examples=["2026-10-21T11:10:00Z"],
    )


class _SessionLock(Protocol):
    def acquire(self, session_id: UUID) -> AbstractAsyncContextManager[None]: ...


class PostgresSessionLock:
    """A PostgreSQL session advisory lock that serializes OGX calls by session ID."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @asynccontextmanager
    async def acquire(self, session_id: UUID) -> AsyncIterator[None]:
        try:
            connection = await self._engine.connect()
        except SQLAlchemyError as exc:
            raise UpstreamError(
                ErrorCode.UPSTREAM_UNAVAILABLE,
                "The chat session could not be locked in the database.",
                retryable=True,
            ) from exc
        acquired_lock = False
        try:
            try:
                acquired = await connection.scalar(
                    text("SELECT pg_try_advisory_lock(hashtextextended(:session_id, 0))"),
                    {"session_id": str(session_id)},
                )
                await connection.commit()
            except SQLAlchemyError as exc:
                # A network error can occur after PostgreSQL acquired the lock. Discard
                # this connection so a session-level lock cannot leak back into the pool.
                await connection.invalidate()
                raise UpstreamError(
                    ErrorCode.UPSTREAM_UNAVAILABLE,
                    "The chat session could not be locked in the database.",
                    retryable=True,
                ) from exc
            if acquired is not True:
                raise UpstreamError(
                    ErrorCode.UPSTREAM_UNAVAILABLE,
                    "A request for this chat session is already in progress.",
                    retryable=True,
                )
            acquired_lock = True
            yield
        finally:
            try:
                if acquired_lock:
                    unlocked = await connection.scalar(
                        text("SELECT pg_advisory_unlock(hashtextextended(:session_id, 0))"),
                        {"session_id": str(session_id)},
                    )
                    await connection.commit()
                    if unlocked is False:
                        logger.error(
                            "chat session advisory lock was not held during release",
                            extra={"component": "api", "session_id": str(session_id)},
                        )
            except SQLAlchemyError:
                logger.exception(
                    "failed to release chat session advisory lock",
                    extra={"component": "api", "session_id": str(session_id)},
                )
                await connection.invalidate()
            finally:
                await connection.close()


class ChatService:
    """Persist request identity and response before exposing replay semantics."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        ogx_client: OgxResponsesClient,
        session_lock: _SessionLock,
    ) -> None:
        self._session_factory = session_factory
        self._ogx_client = ogx_client
        self._session_lock = session_lock

    async def send_message(
        self,
        session_id: UUID,
        request_id: UUID,
        message: str,
    ) -> ChatMessageResponse:
        """Return the persisted response or create one completed OGX response."""

        message_sha256 = hashlib.sha256(message.encode("utf-8")).hexdigest()
        existing = await self._load(session_id, request_id)
        if existing is not None and (
            existing.message_sha256 != message_sha256 or existing.status != "rejected"
        ):
            return _existing_response(existing, message_sha256)

        async with self._session_lock.acquire(session_id):
            previous_response_id = await self._latest_response_id(session_id)
            row, created = await self._reserve(
                session_id=session_id,
                request_id=request_id,
                message_sha256=message_sha256,
            )
            if not created:
                return _existing_response(row, message_sha256)

            try:
                reply = await self._ogx_client.create_response(
                    session_id, message, previous_response_id=previous_response_id
                )
            except OgxRequestRejected:
                # Persist the rejection so the request ID remains bound to this message.
                # A same-content retry can reclaim it; changed content must still conflict.
                await self._mark_rejected(session_id, request_id, message_sha256)
                raise
            # Keep the reservation after ambiguous failures. A timeout or 5xx may follow a
            # remote side effect, so repeating the request could advance the conversation twice.
            response = ChatMessageResponse(
                session_id=session_id,
                request_id=request_id,
                response_id=reply.response_id,
                assistant_message=reply.text,
                created_at=datetime.now(UTC),
            )
            await self._complete(row, response)
            return response

    async def _load(self, session_id: UUID, request_id: UUID) -> ChatRequest | None:
        try:
            async with self._session_factory() as session:
                return await session.get(ChatRequest, (session_id, request_id))
        except SQLAlchemyError as exc:
            raise _database_error() from exc

    async def _latest_response_id(self, session_id: UUID) -> str | None:
        """Load durable response history while the caller holds the session lock."""

        statement = (
            select(ChatRequest.response_payload)
            .where(ChatRequest.session_id == session_id, ChatRequest.status == "completed")
            .order_by(
                ChatRequest.completed_at.desc(),
                ChatRequest.created_at.desc(),
                ChatRequest.request_id.desc(),
            )
            .limit(1)
        )
        try:
            async with self._session_factory() as session:
                payload = await session.scalar(statement)
        except SQLAlchemyError as exc:
            raise _database_error() from exc
        if payload is None:
            return None
        try:
            previous = ChatMessageResponse.model_validate(payload)
        except ValueError as exc:
            raise ServiceError(
                ErrorCode.INTERNAL_ERROR,
                "The stored chat history is not valid.",
            ) from exc
        if previous.session_id != session_id or not previous.response_id.strip():
            raise ServiceError(
                ErrorCode.INTERNAL_ERROR,
                "The stored chat history does not match this session.",
            )
        return previous.response_id

    async def _mark_rejected(
        self,
        session_id: UUID,
        request_id: UUID,
        message_sha256: str,
    ) -> None:
        statement = (
            update(ChatRequest)
            .where(
                ChatRequest.session_id == session_id,
                ChatRequest.request_id == request_id,
                ChatRequest.message_sha256 == message_sha256,
                ChatRequest.status == "in_progress",
            )
            .values(status="rejected")
        )
        try:
            async with self._session_factory() as session:
                await session.execute(statement)
                await session.commit()
        except SQLAlchemyError as exc:
            raise _database_error() from exc

    async def _reserve(
        self,
        session_id: UUID,
        request_id: UUID,
        message_sha256: str,
    ) -> tuple[ChatRequest, bool]:
        statement = (
            pg_insert(ChatRequest)
            .values(
                session_id=session_id,
                request_id=request_id,
                message_sha256=message_sha256,
                status="in_progress",
            )
            .on_conflict_do_update(
                index_elements=[ChatRequest.session_id, ChatRequest.request_id],
                set_={
                    "status": "in_progress",
                    "response_payload": None,
                    "completed_at": None,
                },
                where=(
                    (ChatRequest.message_sha256 == message_sha256)
                    & (ChatRequest.status == "rejected")
                ),
            )
            .returning(ChatRequest.request_id)
        )
        try:
            async with self._session_factory() as session:
                inserted_id = await session.scalar(statement)
                await session.commit()
                row = await session.get(ChatRequest, (session_id, request_id))
        except SQLAlchemyError as exc:
            raise _database_error() from exc
        if row is None:
            raise ServiceError(
                ErrorCode.INTERNAL_ERROR,
                "The chat request could not be read after it was recorded.",
            )
        return row, inserted_id is not None

    async def _complete(
        self,
        reservation: ChatRequest,
        response: ChatMessageResponse,
    ) -> None:
        try:
            async with self._session_factory() as session:
                row = await session.get(
                    ChatRequest,
                    (reservation.session_id, reservation.request_id),
                    with_for_update=True,
                )
                if row is None or row.status != "in_progress":
                    raise ServiceError(
                        ErrorCode.INTERNAL_ERROR,
                        "The chat request reservation is no longer available.",
                    )
                row.status = "completed"
                row.response_payload = response.to_wire()
                row.completed_at = datetime.now(UTC)
                await session.commit()
        except SQLAlchemyError as exc:
            raise _database_error() from exc


def _existing_response(row: ChatRequest, message_sha256: str) -> ChatMessageResponse:
    if row.message_sha256 != message_sha256:
        raise ServiceError(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            "request_id is already used for a different message.",
        )
    if row.status == "in_progress":
        raise UpstreamError(
            ErrorCode.UPSTREAM_UNAVAILABLE,
            "The original chat request is still in progress and may be retried.",
            retryable=True,
        )
    if row.status == "rejected":
        raise UpstreamError(
            ErrorCode.UPSTREAM_UNAVAILABLE,
            "The rejected chat request could not be reclaimed and may be retried.",
            retryable=True,
        )
    if row.status != "completed" or row.response_payload is None:
        raise ServiceError(
            ErrorCode.INTERNAL_ERROR,
            "The stored chat response is not valid.",
        )

    # The response body includes the original created_at value, so Pydantic
    # serialization after validation preserves the timestamp on every replay.
    return ChatMessageResponse.model_validate(row.response_payload)


def _database_error() -> UpstreamError:
    return UpstreamError(
        ErrorCode.UPSTREAM_UNAVAILABLE,
        "The chat request could not be persisted.",
        retryable=True,
    )
