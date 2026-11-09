"""Async access to host session references, preserving local credential formats."""

import asyncio


async def resolve_credential_async(credentials, reference, *, purpose, profile_id=None):
    resolver = getattr(credentials, "resolve_runtime", None)
    if reference.startswith("session:") and callable(resolver):
        return await resolver(reference, purpose=purpose, profile_id=profile_id)
    return await asyncio.to_thread(credentials.resolve, reference)


async def credential_available_async(
    credentials, reference, *, purpose, profile_id=None
):
    observer = getattr(credentials, "runtime_available", None)
    if reference.startswith("session:") and callable(observer):
        return await observer(reference, purpose=purpose, profile_id=profile_id)
    return (await asyncio.to_thread(credentials.status, reference)).available
