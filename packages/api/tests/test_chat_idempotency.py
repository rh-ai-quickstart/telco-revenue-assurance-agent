from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest
from api.db.models import ChatRequest
from api.services.chat import ChatMessageResponse, ChatService
from api.services.ogx_client import OgxReply, OgxRequestRejected, OgxResponsesClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import ClauseElement, Select

from common.errors import ErrorCode, ServiceError, UpstreamError


class PersistentChatRows:
    """Shared row store that survives recreation of a ChatService instance."""

    def __init__(self) -> None:
        self.rows: dict[tuple[UUID, UUID], ChatRequest] = {}


class FakeSession:
    def __init__(self, database: PersistentChatRows) -> None:
        self._database = database

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    async def get(
        self,
        model: type[ChatRequest],
        identity: tuple[UUID, UUID],
        **kwargs: object,
    ) -> ChatRequest | None:
        del model, kwargs
        return self._database.rows.get(identity)

    async def scalar(self, statement: ClauseElement) -> UUID | dict[str, object] | None:
        compiled = statement.compile(dialect=postgresql.dialect())  # type: ignore[no-untyped-call]
        if isinstance(statement, Select):
            session_id = compiled.params["session_id_1"]
            completed = [
                row
                for row in self._database.rows.values()
                if row.session_id == session_id and row.status == "completed"
            ]
            if not completed:
                return None
            latest = max(
                completed,
                key=lambda row: (
                    cast(datetime, row.completed_at),
                    row.created_at,
                    str(row.request_id),
                ),
            )
            return cast(dict[str, object], latest.response_payload)
        assert "ON CONFLICT (session_id, request_id) DO UPDATE" in str(compiled)
        params = cast(dict[str, object], compiled.params)
        session_id = cast(UUID, params["session_id"])
        request_id = cast(UUID, params["request_id"])
        message_sha256 = cast(str, params["message_sha256"])
        key = (session_id, request_id)
        if key in self._database.rows:
            row = self._database.rows[key]
            if row.message_sha256 == message_sha256 and row.status == "rejected":
                row.status = "in_progress"
                row.response_payload = None
                row.completed_at = None
                return request_id
            return None
        self._database.rows[key] = ChatRequest(
            session_id=session_id,
            request_id=request_id,
            message_sha256=message_sha256,
            status=cast(str, params["status"]),
            response_payload=None,
            created_at=datetime.now(UTC),
            completed_at=None,
        )
        return request_id

    async def execute(self, statement: ClauseElement) -> object:
        compiled = statement.compile(dialect=postgresql.dialect())  # type: ignore[no-untyped-call]
        assert str(compiled).startswith("UPDATE app.chat_requests SET status=")
        params = cast(dict[str, object], compiled.params)
        key = (
            cast(UUID, params["session_id_1"]),
            cast(UUID, params["request_id_1"]),
        )
        row = self._database.rows.get(key)
        if (
            row is not None
            and row.message_sha256 == params["message_sha256_1"]
            and row.status == params["status_1"]
        ):
            row.status = cast(str, params["status"])
        return object()

    async def commit(self) -> None:
        return None


class FakeSessionFactory:
    def __init__(self, database: PersistentChatRows) -> None:
        self._database = database

    def __call__(self) -> FakeSession:
        return FakeSession(self._database)


class FakeSessionLock:
    def __init__(self) -> None:
        self._locks: dict[UUID, asyncio.Lock] = {}

    @asynccontextmanager
    async def acquire(self, session_id: UUID) -> AsyncIterator[None]:
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            raise UpstreamError(
                ErrorCode.UPSTREAM_UNAVAILABLE,
                "A request for this chat session is already in progress.",
                retryable=True,
            )
        await lock.acquire()
        try:
            yield
        finally:
            lock.release()


class FakeOgxClient:
    def __init__(self, outcomes: list[OgxReply | Exception] | None = None) -> None:
        self.calls = 0
        self.previous_response_ids: list[str | None] = []
        self.outcomes = list(outcomes or [])
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.wait_for_release = False

    async def create_response(
        self, session_id: UUID, message: str, *, previous_response_id: str | None = None
    ) -> OgxReply:
        del session_id, message
        self.previous_response_ids.append(previous_response_id)
        self.calls += 1
        self.started.set()
        if self.wait_for_release:
            await self.release.wait()
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return OgxReply("resp_default", "session", "answer")


def _service(
    database: PersistentChatRows,
    ogx_client: FakeOgxClient,
    lock: FakeSessionLock | None = None,
) -> ChatService:
    return ChatService(
        cast(async_sessionmaker[AsyncSession], FakeSessionFactory(database)),
        cast(OgxResponsesClient, ogx_client),
        lock or FakeSessionLock(),
    )


def _reply(response_id: str, text: str) -> OgxReply:
    return OgxReply(response_id=response_id, conversation_id="session", text=text)


@pytest.mark.asyncio
async def test_completed_request_replays_exact_persisted_response_after_service_recreation() -> (
    None
):
    database = PersistentChatRows()
    session_id = uuid4()
    request_id = uuid4()
    original_client = FakeOgxClient([_reply("resp_first", "original answer")])
    first_service = _service(database, original_client)

    original = await first_service.send_message(session_id, request_id, "same message")
    stored_row = database.rows[(session_id, request_id)]
    stored_payload = dict(cast(dict[str, object], stored_row.response_payload))
    stored_completed_at = stored_row.completed_at

    recreated_client = FakeOgxClient()
    recreated_service = _service(database, recreated_client)
    replay = await recreated_service.send_message(session_id, request_id, "same message")

    assert replay.to_wire() == stored_payload
    assert replay.to_wire() == original.to_wire()
    assert replay.created_at == original.created_at
    assert database.rows[(session_id, request_id)].completed_at == stored_completed_at
    assert original_client.calls == 1
    assert recreated_client.calls == 0


@pytest.mark.asyncio
async def test_changed_message_for_same_session_and_request_id_conflicts() -> None:
    database = PersistentChatRows()
    session_id = uuid4()
    request_id = uuid4()
    client = FakeOgxClient([_reply("resp_first", "original answer")])
    service = _service(database, client)
    await service.send_message(session_id, request_id, "first message")

    with pytest.raises(ServiceError) as caught:
        await service.send_message(session_id, request_id, "changed message")

    assert caught.value.code == ErrorCode.IDEMPOTENCY_CONFLICT
    assert client.calls == 1


@pytest.mark.asyncio
async def test_in_progress_duplicate_does_not_invoke_ogx_again() -> None:
    database = PersistentChatRows()
    session_id = uuid4()
    request_id = uuid4()
    message = "already claimed"
    database.rows[(session_id, request_id)] = ChatRequest(
        session_id=session_id,
        request_id=request_id,
        message_sha256=hashlib.sha256(message.encode()).hexdigest(),
        status="in_progress",
        response_payload=None,
        created_at=datetime.now(UTC),
        completed_at=None,
    )
    client = FakeOgxClient()

    with pytest.raises(UpstreamError) as caught:
        await _service(database, client).send_message(session_id, request_id, message)

    assert caught.value.code == ErrorCode.UPSTREAM_UNAVAILABLE
    assert caught.value.retryable is True
    assert client.calls == 0


@pytest.mark.asyncio
async def test_ambiguous_upstream_failure_keeps_claim_and_suppresses_retry() -> None:
    database = PersistentChatRows()
    session_id = uuid4()
    request_id = uuid4()
    ambiguous_client = FakeOgxClient(
        [UpstreamError(ErrorCode.UPSTREAM_UNAVAILABLE, "timeout", retryable=True)]
    )

    with pytest.raises(UpstreamError):
        await _service(database, ambiguous_client).send_message(
            session_id, request_id, "message with ambiguous result"
        )
    assert database.rows[(session_id, request_id)].status == "in_progress"

    retry_client = FakeOgxClient()
    with pytest.raises(UpstreamError) as caught:
        await _service(database, retry_client).send_message(
            session_id, request_id, "message with ambiguous result"
        )

    assert caught.value.retryable is True
    assert ambiguous_client.calls == 1
    assert retry_client.calls == 0


@pytest.mark.asyncio
async def test_explicit_rejection_persists_claim_and_allows_same_content_retry() -> None:
    database = PersistentChatRows()
    session_id = uuid4()
    request_id = uuid4()
    client = FakeOgxClient(
        [
            OgxRequestRejected(
                ErrorCode.UPSTREAM_UNAVAILABLE,
                "request rejected",
                retryable=False,
            ),
            _reply("resp_second", "answer after retry"),
        ]
    )
    service = _service(database, client)

    with pytest.raises(OgxRequestRejected):
        await service.send_message(session_id, request_id, "same message")
    assert database.rows[(session_id, request_id)].status == "rejected"

    response = await service.send_message(session_id, request_id, "same message")

    assert response.assistant_message == "answer after retry"
    assert database.rows[(session_id, request_id)].status == "completed"
    assert client.calls == 2


@pytest.mark.asyncio
async def test_changed_content_after_explicit_rejection_conflicts() -> None:
    database = PersistentChatRows()
    session_id = uuid4()
    request_id = uuid4()
    client = FakeOgxClient(
        [
            OgxRequestRejected(
                ErrorCode.UPSTREAM_UNAVAILABLE,
                "request rejected",
                retryable=False,
            )
        ]
    )
    service = _service(database, client)

    with pytest.raises(OgxRequestRejected):
        await service.send_message(session_id, request_id, "first message")

    with pytest.raises(ServiceError) as caught:
        await service.send_message(session_id, request_id, "changed message")

    assert caught.value.code == ErrorCode.IDEMPOTENCY_CONFLICT
    assert database.rows[(session_id, request_id)].status == "rejected"
    assert client.calls == 1


@pytest.mark.asyncio
async def test_concurrent_duplicate_during_ogx_call_gets_retryable_unavailable() -> None:
    database = PersistentChatRows()
    session_id = uuid4()
    request_id = uuid4()
    lock = FakeSessionLock()
    client = FakeOgxClient([_reply("resp_first", "answer")])
    client.wait_for_release = True
    service = _service(database, client, lock)

    first_task = asyncio.create_task(service.send_message(session_id, request_id, "same message"))
    await client.started.wait()
    with pytest.raises(UpstreamError) as caught:
        await service.send_message(session_id, request_id, "same message")
    client.release.set()
    response = await first_task

    assert caught.value.retryable is True
    assert isinstance(response, ChatMessageResponse)
    assert client.calls == 1


@pytest.mark.asyncio
async def test_distinct_request_for_active_session_uses_nonblocking_try_lock() -> None:
    database = PersistentChatRows()
    session_id = uuid4()
    first_request_id = uuid4()
    second_request_id = uuid4()
    lock = FakeSessionLock()
    client = FakeOgxClient([_reply("resp_first", "answer")])
    client.wait_for_release = True
    service = _service(database, client, lock)

    first_task = asyncio.create_task(
        service.send_message(session_id, first_request_id, "first message")
    )
    await client.started.wait()
    with pytest.raises(UpstreamError) as caught:
        await service.send_message(session_id, second_request_id, "second message")
    client.release.set()
    await first_task

    assert caught.value.retryable is True
    assert (session_id, second_request_id) not in database.rows
    assert client.calls == 1


@pytest.mark.asyncio
async def test_followup_uses_last_completed_response_after_service_recreation() -> None:
    database = PersistentChatRows()
    session_id = uuid4()
    first_client = FakeOgxClient([_reply("resp_first", "first answer")])
    await _service(database, first_client).send_message(session_id, uuid4(), "first message")

    second_client = FakeOgxClient([_reply("resp_second", "followup answer")])
    await _service(database, second_client).send_message(session_id, uuid4(), "followup")
    assert first_client.previous_response_ids == [None]
    assert second_client.previous_response_ids == ["resp_first"]

    unrelated = FakeOgxClient([_reply("resp_other", "other session")])
    await _service(database, unrelated).send_message(uuid4(), uuid4(), "unrelated")
    assert unrelated.previous_response_ids == [None]


@pytest.mark.asyncio
async def test_rejected_retry_keeps_last_completed_history() -> None:
    database = PersistentChatRows()
    session_id, rejected_id = uuid4(), uuid4()
    client = FakeOgxClient(
        [
            _reply("resp_first", "first answer"),
            OgxRequestRejected(ErrorCode.UPSTREAM_UNAVAILABLE, "rejected", retryable=False),
            _reply("resp_retry", "retry answer"),
        ]
    )
    await _service(database, client).send_message(session_id, uuid4(), "first message")
    with pytest.raises(OgxRequestRejected):
        await _service(database, client).send_message(session_id, rejected_id, "retry message")
    await _service(database, client).send_message(session_id, rejected_id, "retry message")
    assert client.previous_response_ids == [None, "resp_first", "resp_first"]
