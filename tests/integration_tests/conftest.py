"""Fixtures for integration tests."""
import re

import pytest
import typer
import typer.testing

from rstcheck import _cli


ERROR_CODE_REGEX = re.compile(r"\([A-Z]*?/\d\)")


@pytest.fixture(name="cli_app")
def cli_app_fixture() -> typer.Typer:
    """Create typer app from ``cli`` function for testing."""
    return _cli.app


@pytest.fixture(name="cli_runner")
def cli_runner_fixture() -> typer.testing.CliRunner:
    """Create CLI Test Runner."""
    return typer.testing.CliRunner(mix_stderr=True)
