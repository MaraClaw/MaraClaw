import pytest

pytest_plugins = ["pytester"]


def test_shards_partition_real_collection_without_omissions_or_duplicates(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
import pytest

@pytest.mark.parametrize("value", range(11))
def test_value(value):
    assert value >= 0
"""
    )
    collections = []
    for index in range(3):
        result = pytester.runpytest(
            "-p",
            "scripts.pytest_shard",
            "--collect-only",
            "-q",
            f"--test-shard-index={index}",
            "--test-shard-count=3",
        )
        assert result.ret == 0
        collections.append({line for line in result.stdout.lines if "::test_value[" in line})
    assert sum(map(len, collections)) == 11
    assert len(set.union(*collections)) == 11
    assert not collections[0] & collections[1]
    assert not collections[0] & collections[2]
    assert not collections[1] & collections[2]
    pytester.makeconftest(
        "import pytest\n"
        "@pytest.hookimpl(tryfirst=True)\n"
        "def pytest_collection_modifyitems(items):\n"
        "    items.reverse()\n"
    )
    reordered = pytester.runpytest(
        "-p",
        "scripts.pytest_shard",
        "--collect-only",
        "-q",
        "--test-shard-index=1",
        "--test-shard-count=3",
    )
    assert {line for line in reordered.stdout.lines if "::test_value[" in line} == collections[1]
    result = pytester.runpytest("-p", "scripts.pytest_shard", "--test-shard-index=1", "--test-shard-count=3")
    result.assert_outcomes(passed=len(collections[1]), deselected=11 - len(collections[1]))


@pytest.mark.parametrize(
    "options",
    [
        ["--test-shard-index=0"],
        ["--test-shard-count=3"],
        ["--test-shard-index=-1", "--test-shard-count=3"],
        ["--test-shard-index=3", "--test-shard-count=3"],
        ["--test-shard-index=0", "--test-shard-count=0"],
    ],
)
def test_invalid_shards_fail_instead_of_silently_excluding_tests(pytester: pytest.Pytester, options: list[str]) -> None:
    pytester.makepyfile("def test_value(): pass")
    result = pytester.runpytest("-p", "scripts.pytest_shard", *options)
    assert result.ret == pytest.ExitCode.USAGE_ERROR


def test_no_shard_options_preserve_normal_execution(pytester: pytest.Pytester) -> None:
    pytester.makepyfile("def test_value(): pass")
    result = pytester.runpytest("-p", "scripts.pytest_shard")
    result.assert_outcomes(passed=1)
