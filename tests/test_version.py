import re
from pathlib import Path

import pytest

import rowdiff
from rowdiff import cli


def _pyproject_version():
    text = Path("pyproject.toml").read_text()
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match
    return match.group(1)


def test_version_sync():
    assert _pyproject_version() == rowdiff.__version__


def test_version_cli_output(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert rowdiff.__version__ in out
    assert "Python" in out
    assert rowdiff.__version__ == _pyproject_version()
