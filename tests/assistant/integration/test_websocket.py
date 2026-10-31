"""The streaming socket: the token in the first frame, the frame cap, the
connection cap, and what a turn looks like on the wire.

Real server, real socket, fake model client.
"""

from __future__ import annotations

import contextlib
import json
import uuid

import httpx
from websockets.sync.client import connect

from tests.assistant.conftest import TEST_WS_TOKEN
from tests.helpers import DEFAULT_LOAN, new_key

RECEIVE_TIMEOUT_SECONDS = 60


def socket_url(base_url: str) -> str:
    return base_url.replace("http://", "ws://") + "/assistant/ws"


def make_loan(mounted_client: httpx.Client) -> str:
    response = mounted_client.post(
        "/loans", json=DEFAULT_LOAN, headers={"Idempotency-Key": new_key()}
    )
    assert response.status_code == 201
    return str(response.json()["id"])


def session_id() -> str:
    return f"ws-{uuid.uuid4().hex[:10]}"


def recv(socket) -> dict:
    return json.loads(socket.recv(timeout=RECEIVE_TIMEOUT_SECONDS))


def test_a_good_token_opens_the_socket(mounted_app_url: str, mounted_client: httpx.Client) -> None:
    loan_id = make_loan(mounted_client)
    session = session_id()

    with connect(socket_url(mounted_app_url)) as socket:
        socket.send(
            json.dumps({"token": TEST_WS_TOKEN, "session_id": session, "loan_ids": [loan_id]})
        )
        ready = recv(socket)

    assert ready["type"] == "ready"
    assert ready["session_id"] == session
    assert ready["loan_ids"] == [loan_id]


def test_a_wrong_token_is_refused(mounted_app_url: str) -> None:
    with connect(socket_url(mounted_app_url)) as socket:
        socket.send(json.dumps({"token": "not-the-token", "session_id": session_id()}))
        frame = recv(socket)

    assert frame == {"type": "error", "reason": "bad_token"}


def test_a_missing_token_is_refused(mounted_app_url: str) -> None:
    with connect(socket_url(mounted_app_url)) as socket:
        socket.send(json.dumps({"session_id": session_id()}))
        assert recv(socket)["reason"] == "bad_token"


def test_the_first_frame_has_to_be_the_handshake(mounted_app_url: str) -> None:
    """A message frame sent first is rejected, not run."""
    with connect(socket_url(mounted_app_url)) as socket:
        socket.send(json.dumps({"message": "what is my balance"}))
        assert recv(socket)["reason"] == "bad_token"


def test_a_frame_over_the_cap_closes_the_socket(mounted_app_url: str) -> None:
    with connect(socket_url(mounted_app_url)) as socket:
        socket.send("x" * 2_000)  # the cap in the test fixture is 512 bytes
        assert recv(socket) == {"type": "error", "reason": "frame_too_large"}


def test_an_oversized_frame_after_the_handshake_also_closes_the_socket(
    mounted_app_url: str, mounted_client: httpx.Client
) -> None:
    loan_id = make_loan(mounted_client)
    with connect(socket_url(mounted_app_url)) as socket:
        socket.send(
            json.dumps({"token": TEST_WS_TOKEN, "session_id": session_id(), "loan_ids": [loan_id]})
        )
        assert recv(socket)["type"] == "ready"
        socket.send(json.dumps({"message": "x" * 2_000}))
        assert recv(socket) == {"type": "error", "reason": "frame_too_large"}


def test_a_frame_that_is_not_json_closes_the_socket(mounted_app_url: str) -> None:
    with connect(socket_url(mounted_app_url)) as socket:
        socket.send("this is not json")
        assert recv(socket)["reason"] == "frame_not_json"


def test_a_bad_loan_id_in_the_handshake_is_refused(mounted_app_url: str) -> None:
    with connect(socket_url(mounted_app_url)) as socket:
        socket.send(
            json.dumps({"token": TEST_WS_TOKEN, "session_id": session_id(), "loan_ids": ["nope"]})
        )
        assert recv(socket)["reason"] == "bad_loan_ids"


def test_past_the_connection_cap_a_socket_is_told_to_try_later(
    mounted_app_url: str, mounted_client: httpx.Client
) -> None:
    """The cap in the test fixture is two."""
    loan_id = make_loan(mounted_client)
    url = socket_url(mounted_app_url)
    held = []
    with contextlib.ExitStack() as stack:
        for _ in range(2):
            socket = stack.enter_context(connect(url))
            socket.send(
                json.dumps(
                    {"token": TEST_WS_TOKEN, "session_id": session_id(), "loan_ids": [loan_id]}
                )
            )
            assert recv(socket)["type"] == "ready"
            held.append(socket)

        with connect(url) as third:
            assert recv(third) == {"type": "error", "reason": "connection_limit_reached"}

    # Once the held sockets are closed, a new one is accepted again.
    with connect(url) as socket:
        socket.send(
            json.dumps({"token": TEST_WS_TOKEN, "session_id": session_id(), "loan_ids": [loan_id]})
        )
        assert recv(socket)["type"] == "ready"


def test_a_turn_streams_chunks_and_then_a_result(
    mounted_app_url: str, mounted_client: httpx.Client
) -> None:
    loan_id = make_loan(mounted_client)

    with connect(socket_url(mounted_app_url)) as socket:
        socket.send(
            json.dumps({"token": TEST_WS_TOKEN, "session_id": session_id(), "loan_ids": [loan_id]})
        )
        assert recv(socket)["type"] == "ready"

        socket.send(json.dumps({"message": "what is my balance"}))
        frames = []
        while True:
            frame = recv(socket)
            frames.append(frame)
            if frame["type"] == "done":
                break

    assert frames[0] == {"type": "start"}
    assert frames[-1]["type"] == "done"
    assert frames[-1]["outcome"] == "answered"
    assert frames[-1]["tool_calls"] == ["get_loan_summary"]
    assert frames[-1]["awaiting_confirmation"] is False

    streamed = " ".join(frame["text"] for frame in frames if frame["type"] == "token")
    assert "120000 cents" in streamed


def test_a_payment_over_the_socket_still_needs_a_separate_confirmation(
    mounted_app_url: str, mounted_client: httpx.Client, engine
) -> None:
    from sqlalchemy import text

    loan_id = make_loan(mounted_client)

    def run(socket, message: str) -> dict:
        socket.send(json.dumps({"message": message}))
        while True:
            frame = recv(socket)
            if frame["type"] == "done":
                return frame

    with connect(socket_url(mounted_app_url)) as socket:
        socket.send(
            json.dumps({"token": TEST_WS_TOKEN, "session_id": session_id(), "loan_ids": [loan_id]})
        )
        assert recv(socket)["type"] == "ready"

        prepared = run(socket, "pay 300 dollars")
        assert prepared["outcome"] == "prepared"
        assert prepared["awaiting_confirmation"] is True

        with engine.connect() as connection:
            assert int(connection.execute(text("SELECT COUNT(*) FROM payments")).scalar_one()) == 0

        applied = run(socket, "yes")
        assert applied["outcome"] == "applied"
        assert applied["awaiting_confirmation"] is False

    assert mounted_client.get(f"/loans/{loan_id}").json()["paid_cents"] == 30_000


def test_an_empty_message_frame_is_rejected_without_ending_the_socket(
    mounted_app_url: str, mounted_client: httpx.Client
) -> None:
    loan_id = make_loan(mounted_client)
    with connect(socket_url(mounted_app_url)) as socket:
        socket.send(
            json.dumps({"token": TEST_WS_TOKEN, "session_id": session_id(), "loan_ids": [loan_id]})
        )
        assert recv(socket)["type"] == "ready"

        socket.send(json.dumps({"message": ""}))
        assert recv(socket) == {"type": "error", "reason": "bad_message"}

        # Still usable.
        socket.send(json.dumps({"message": "what is my balance"}))
        assert recv(socket)["type"] == "start"
