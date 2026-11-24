"""Refuse to start a release with a production target or unconfigured suffix."""

from pathlib import Path
import tomllib


def main() -> None:
    """Check the fixture's publication configuration before any release action."""
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["tool"]["nopt"]
    if config.get("repository") != "testpypi":
        raise ValueError("this repository may publish only to TestPyPI")
    username = config.get("testpypi-username")
    if not isinstance(username, str) or not username or username == "configure-me":
        raise ValueError("configure the owner's TestPyPI username before publishing")


if __name__ == "__main__":
    main()
