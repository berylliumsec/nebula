"""Observe actual chat maintenance in the disposable readmission worker."""

from contextlib import asynccontextmanager

import acceptance_worker

inert_project = acceptance_worker.project


@asynccontextmanager
async def project(context, lease):
    async with inert_project(context, lease) as app:
        chat = app.state.control_assistant_work.chat

        async def maintenance(payload):
            if payload:
                raise ValueError("The maintenance observation accepts no arguments")
            return {
                "naming": sum(not task.done() for task in chat._naming_tasks),
                "precompactions": sum(
                    not item.task.done() for item in chat._precompactions.values()
                ),
            }

        context.rpc.register("acceptance.chat-maintenance/v1", maintenance)
        yield app


def main():
    acceptance_worker.project = project
    acceptance_worker.main()
