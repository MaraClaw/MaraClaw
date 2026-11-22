from hashlib import sha256

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("CI sharding")
    group.addoption("--test-shard-index", type=int, default=None)
    group.addoption("--test-shard-count", type=int, default=None)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    index: object = config.getoption("test_shard_index")
    count: object = config.getoption("test_shard_count")
    if index is None and count is None:
        return
    if not isinstance(index, int) or not isinstance(count, int) or not 0 <= index < count:
        raise pytest.UsageError("Require 0 <= --test-shard-index < --test-shard-count")
    selected: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        destination = int.from_bytes(sha256(item.nodeid.encode()).digest()[:8]) % count
        (selected if destination == index else deselected).append(item)
    items[:] = selected
    config.hook.pytest_deselected(items=deselected)
