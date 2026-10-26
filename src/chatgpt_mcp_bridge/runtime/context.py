"""Process-scoped services and lifecycle."""

import asyncio

from .executor import ExecutionRuntime

executor: ExecutionRuntime | None = None
agent = None
_lock = asyncio.Lock()


async def initialize() -> None:
    global executor
    async with _lock:
        if executor is None:
            executor = ExecutionRuntime()


def get_executor() -> ExecutionRuntime:
    if executor is None:
        raise RuntimeError("Execution service is not initialized")
    return executor


async def get_agent():
    global agent
    async with _lock:
        if agent is None:
            from ..agent.service import AgentService

            agent = AgentService(get_executor().workspace)
    await agent.start()
    return agent


async def shutdown() -> None:
    global executor, agent
    if agent is not None:
        await agent.close()
        agent = None
    if executor is not None:
        await executor.close_all()
        executor = None
