"""The hang backstop the Python suites beside this file wait against.

A backstop's valid values are 0 - the assertion is synchronous - and INFINITE,
meaning wait until the system above gives up. A value chosen to be fast turns
runner contention into a flake, while a generous one leaves the bug-detection
signal identical, because a wait that never ends still fails.

THE VALUE IS BOUNDED BY THE BAZEL TEST TIMEOUT OF EACH SUITE THAT WAITS ON IT.
Such a suite's `timeout` in tests/BUILD.bazel sits above one full backstop plus
the suite's own run, so a wait that reaches this value fails in the test that
armed it and names itself rather than being killed with the test.

NOT `test_timeouts.py`: every `test_*.py` here is a suite.
"""

# Sized from contended subprocess and readiness waits on Linux and macOS;
# the enclosing suite timeouts also reserve the measured suite runtime.
INFINITE = 120
