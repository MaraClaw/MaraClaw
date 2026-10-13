"""One-shot production seed lifecycle; schema DDL runs in the engine entrypoint."""

import asyncio


async def main() -> None:
    from app.runtime.lifecycle import Runtime

    runtime = Runtime()
    try:
        await runtime.start(frozenset({"bootstrap"}))
    finally:
        await runtime.stop()


if __name__ == "__main__":
    asyncio.run(main())
