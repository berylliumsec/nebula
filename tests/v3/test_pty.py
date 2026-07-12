import asyncio

import pytest

from nebula.v3 import pty


class _ClosingWebSocket:
    def __init__(self) -> None:
        self.code = None
        self.reason = None

    async def close(self, *, code, reason):
        self.code = code
        self.reason = reason


def test_host_without_a_trusted_shell_fails_closed(tmp_path, monkeypatch):
    service = pty.HumanPtyService(tmp_path / "sessions")
    websocket = _ClosingWebSocket()
    monkeypatch.setattr(pty.Path, "is_file", lambda _path: False)

    asyncio.run(
        service.serve(
            websocket,
            session_id="human-session",
            columns=120,
            rows=40,
        )
    )

    assert websocket.code == 1013
    assert "unavailable" in websocket.reason


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (80, 80),
        (0, 0),
        (True, 0),
        (80.0, 0),
        ("80", 0),
        (None, 0),
    ],
)
def test_terminal_dimensions_do_not_coerce_untrusted_json(value, expected):
    assert pty._integer(value) == expected


def test_write_all_handles_partial_pty_writes(monkeypatch):
    received = bytearray()

    def partial_write(descriptor, value):
        assert descriptor == 42
        chunk = bytes(value[:2])
        received.extend(chunk)
        return len(chunk)

    monkeypatch.setattr(pty.os, "write", partial_write)

    pty._write_all(42, b"abcdef")

    assert received == b"abcdef"


@pytest.mark.parametrize(
    ("columns", "rows"),
    [(0, 40), (120, 0), (1001, 40), (120, 1001)],
)
def test_resize_rejects_dimensions_outside_the_protocol(columns, rows):
    with pytest.raises(pty.PtyError, match="between 1 and 1000"):
        pty._resize(-1, columns, rows)
