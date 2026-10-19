import unittest
from pathlib import Path
import tempfile

from deploy.autoscale_policy import Demand, Policy, admit, capacity, decision

GIB = 1024**3


class PolicyParsingTests(unittest.TestCase):
    def test_policy_when_toml_overrides_are_present_uses_distinct_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.toml"
            path.write_text("sample_seconds=2.5\nengine_rps=7\nguest_gib=4\nmaximum=9\n")
            policy = Policy.load(path)
        self.assertEqual((policy.sample_seconds, policy.engine_rps, policy.guest_gib, policy.maximum),
                         (2.5, 7, 4, 9))

    def test_policy_when_numbers_are_unsafe_refuses_before_scaling(self) -> None:
        for content in ("minimum=0", "maximum=17", "high_samples=1.5", "sample_seconds=nan",
                        "guest_gib=-1", "engine_rps=true", "low_samples=0"):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "policy.toml"
                path.write_text(content)
                with self.assertRaises(ValueError):
                    Policy.load(path)


class AdmissionTests(unittest.TestCase):
    def test_capacity_when_host_grows_past_sixteen_gib(self) -> None:
        # Given the default steady fixed services plus web replicas and guest floor.
        fixed = int(6.75 * GIB)
        # When independent daemon sizes are admitted.
        limits = [capacity(size * GIB, fixed, 2 * GIB, Policy()) for size in (16, 24, 32)]
        # Then larger hosts admit genuinely different API counts.
        self.assertEqual(limits, [2, 5, 9])

    def test_override_when_operator_reduces_capacity_and_guest_floor(self) -> None:
        # Given distinct overrides, not the defaults.
        policy = Policy(capacity_gib=20, guest_gib=4)
        # When using a 32 GiB daemon and 1 GiB replicas.
        count = capacity(32 * GIB, 6 * GIB, GIB, policy)
        # Then the operator cap and larger guest allocation both count.
        self.assertEqual(count, 7)

    def test_unknown_external_when_not_explicitly_accounted(self) -> None:
        # Given unknown usage rather than an RSS measurement.
        # When checking otherwise cheap growth.
        allowed = admit(32 * GIB, GIB, None, Policy())
        # Then growth is blocked, never treated as zero.
        self.assertFalse(allowed)

    def test_external_limits_when_larger_than_floor(self) -> None:
        # Given a 16 GiB daemon and 8 GiB of external hard limits.
        # When admitting an 8 GiB stack.
        allowed = admit(16 * GIB, 8 * GIB, 8 * GIB, Policy())
        # Then guest allocations are counted above the floor.
        self.assertFalse(allowed)


class DemandTests(unittest.TestCase):
    def test_growth_when_request_delta_exceeds_threshold(self) -> None:
        # Given high requests, not connection totals.
        demand = Demand(high=3)
        # When choosing at a valid sustained high sample.
        result = decision(demand, 1, Policy())
        # Then add exactly one replica.
        self.assertEqual(result, 1)

    def test_minimum_when_sustained_idle(self) -> None:
        # Given sustained idle with only one replica.
        # When making a decision.
        result = decision(Demand(low=30), 1, Policy())
        # Then never reduce below one.
        self.assertEqual(result, 0)

    def test_idle_when_sustained_and_above_minimum(self) -> None:
        # Given sustained idle at two replicas.
        # When making a decision.
        result = decision(Demand(low=30), 2, Policy())
        # Then reduce by just one.
        self.assertEqual(result, -1)
