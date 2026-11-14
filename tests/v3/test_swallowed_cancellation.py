"""Owned loops stop even when one cancellation is swallowed below them.

Python 3.11's asyncio.wait_for (SQLAlchemy pool checkout) can drop a cancel
without raising (bpo-42130); a driver error during cancel cleanup can replace
it. Either way the loop's single cancel() is consumed and it runs on.
"""

import asyncio
from types import SimpleNamespace

import pytest

from nebula.v3.async_utils import cancel_and_join
from nebula.v3.chat_queue import ChatQueueService
from nebula.v3.storage_integrations import StorageIntegrationService

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def swallow_first_cancel():
    swallowed = []

    async def unit_of_work(*_args, **_kwargs):
        try:
            await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            if swallowed:
                raise
            # The cancellation is lost, exactly as in the pool checkout race.
            swallowed.append(True)
        return []

    return unit_of_work, swallowed


async def test_cancel_and_join_recancels_a_loop_that_swallowed_a_cancel():
    unit_of_work, swallowed = swallow_first_cancel()

    async def loop():
        while True:
            await unit_of_work()
            await asyncio.sleep(10)

    task = asyncio.create_task(loop())
    await asyncio.sleep(0.01)
    async with asyncio.timeout(3):
        await cancel_and_join(task, interval=0.1)
    assert swallowed and task.cancelled()


async def test_cancel_and_join_raises_a_real_task_failure():
    async def broken():
        await asyncio.sleep(0)
        raise LookupError("synthetic failure")

    task = asyncio.create_task(broken())
    await asyncio.sleep(0.01)
    with pytest.raises(LookupError):
        await cancel_and_join(task)


async def test_storage_dispatch_stops_after_a_swallowed_cancel():
    unit_of_work, swallowed = swallow_first_cancel()
    service = StorageIntegrationService(
        SimpleNamespace(), chat_queue=None, harness_runtime=None, credentials=None
    )
    service.dispatch_pending = unit_of_work
    await service.startup()
    await asyncio.sleep(0.01)
    async with asyncio.timeout(3):
        await service.shutdown()
    assert swallowed and service._task is None


async def test_chat_follow_ups_stop_after_a_swallowed_cancel():
    unit_of_work, swallowed = swallow_first_cancel()
    store = SimpleNamespace(on_follow_up_change=lambda _signal: None)
    service = ChatQueueService(store, chat=None, harness=None)
    service.queues = unit_of_work
    service.task = asyncio.create_task(service.run())
    await asyncio.sleep(0.01)
    async with asyncio.timeout(3):
        await service.shutdown()
    assert swallowed and service.task.done()
