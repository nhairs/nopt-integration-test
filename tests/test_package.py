"""Check the importable integration package."""

from nopt_integration_test import VALUE


def test_package() -> None:
    """Keep the distribution's import independent of its TestPyPI name suffix."""
    assert VALUE == "nopt integration fixture"
