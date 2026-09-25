import re
from pathlib import Path

from actl import __version__


def test_version_matches_pyproject():
    # regex instead of tomllib so the suite runs on Python 3.10 (requires-python >= 3.10)
    pyproject = (Path(__file__).parents[1] / "pyproject.toml").read_text()
    expected = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M).group(1)
    assert __version__ == expected
