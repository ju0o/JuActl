import tomllib
from pathlib import Path

from actl import __version__


def test_version_matches_pyproject():
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    expected = tomllib.loads(pyproject.read_text())['project']['version']
    assert __version__ == expected
