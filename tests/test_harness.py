import os

import pytest

from internetnl_cli.cli import main


def test_home_is_isolated(isolated_home):
    assert os.environ["HOME"] == str(isolated_home)
    assert not any(k.startswith("INTERNETNL_") for k in os.environ)


def test_help_exits_zero():
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0


def test_no_subcommand_exits_two():
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2
