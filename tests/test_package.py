"""Check the importable integration package."""

from nopt_integration_test import VALUE


def test_package() -> None:
    """Check the integration package installed in the test environment."""
    assert VALUE == "nopt integration fixture"
