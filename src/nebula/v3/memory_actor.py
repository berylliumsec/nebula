"""Trusted server-side attribution for Butler's calls to the shared API."""

from contextvars import ContextVar

# Never populated from a public HTTP header or tool argument. The Core-owned
# settings gateway binds the selected project while performing its ASGI call.
memory_actor: ContextVar[str | None] = ContextVar("nebula_memory_actor", default=None)
