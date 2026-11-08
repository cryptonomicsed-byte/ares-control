"""P1-16: the Vantage trading-integration services are registered, none EXECUTION.

Acceptance (handoff rule 3): every new service is INTEL, BRIDGE or PAPER,
never EXECUTION. Paper first; nothing here can place a live order.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from service.daemons import Category, DAEMON_REGISTRY, requires_approval  # noqa: E402

P1_16 = {
    "ares-crucible.service": Category.BRIDGE,
    "ares-curve-listener.service": Category.INTEL,
    "ares-runner-jobs.service": Category.INTEL,
    "ares-confluence.service": Category.INTEL,
    "ares-guild-router.service": Category.BRIDGE,
    "ares-mycelium-resolver.service": Category.BRIDGE,
    "ares-quant-julia.service": Category.INTEL,
}


class TestP116Registrations(unittest.TestCase):
    def test_all_seven_are_registered_with_the_expected_category(self):
        for unit, cat in P1_16.items():
            self.assertEqual(DAEMON_REGISTRY.get(unit), cat, unit)

    def test_none_are_execution(self):
        for unit in P1_16:
            self.assertNotEqual(DAEMON_REGISTRY[unit], Category.EXECUTION, unit)
            self.assertFalse(requires_approval(unit), unit)

    def test_every_registration_ends_in_service(self):
        for unit in P1_16:
            self.assertTrue(unit.endswith(".service"), unit)


if __name__ == "__main__":
    unittest.main()
