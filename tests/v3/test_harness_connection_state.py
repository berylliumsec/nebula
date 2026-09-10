"""Transport observation never equates an idle turn with a disconnected peer."""

import asyncio
from types import SimpleNamespace

import pytest

from nebula.v3.harnesses import (
    CodexAppServerConnection,
    GrokAcpConnection,
    _CodexRpc,
    _AcpRpc,
)


@pytest.mark.parametrize(
    "rpc_class,connection_class",
    [(_CodexRpc, CodexAppServerConnection), (_AcpRpc, GrokAcpConnection)],
)
def test_rpc_reports_unstarted_alive_exited_and_closed_transport(
    rpc_class, connection_class
):
    async def scenario():
        process = SimpleNamespace(returncode=None)
        rpc = rpc_class(process=process)
        connection = connection_class(
            rpc, external_session_id="fixture", permission_handler=None
        )
        assert connection.connection_state == "unknown"
        reader = asyncio.get_running_loop().create_future()
        rpc._reader_task = reader
        assert connection.connection_state == "connected"
        process.returncode = 0
        assert connection.connection_state == "disconnected"
        process.returncode = None
        reader.set_result(None)
        assert connection.connection_state == "disconnected"
        rpc._reader_task = None
        rpc._closing = True
        assert connection.connection_state == "disconnected"

    asyncio.run(scenario())
