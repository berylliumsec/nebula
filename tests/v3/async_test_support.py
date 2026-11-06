"""Keep converted integration fixtures and their resources on one event loop."""

import pytest

from nebula.v3.chat import ChatService
from nebula.v3.database import Database


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
async def close_database_handles(monkeypatch, anyio_backend):
    databases = []
    services = []
    original_database = Database.__init__
    original_chat = ChatService.__init__

    def database(self, *args, **kwargs):
        original_database(self, *args, **kwargs)
        databases.append(self)

    def chat(self, *args, **kwargs):
        original_chat(self, *args, **kwargs)
        services.append(self)

    monkeypatch.setattr(Database, "__init__", database)
    monkeypatch.setattr(ChatService, "__init__", chat)
    yield
    for service in reversed(services):
        await service.shutdown()
    for database in reversed(databases):
        await database.dispose()
