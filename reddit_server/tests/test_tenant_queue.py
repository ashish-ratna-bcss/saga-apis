import asyncio

from reddit_app.reddit.tenant_queue import TenantRoundRobin


def test_round_robin_alternates_tenants_when_one_slot() -> None:
    gate = TenantRoundRobin(size=1)
    order: list[str] = []

    async def worker(name: str) -> None:
        await gate.acquire(name)
        order.append(name)
        await asyncio.sleep(0)
        await gate.release()

    async def scenario() -> None:
        # Hold the only slot so both tenants queue, then release and let them run.
        await gate.acquire("seed")
        pending = [asyncio.create_task(worker("odisha")), asyncio.create_task(worker("delhi"))]
        await asyncio.sleep(0)
        await gate.release()
        await asyncio.gather(*pending)

    asyncio.run(scenario())
    assert order == ["odisha", "delhi"]
