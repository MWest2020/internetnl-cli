import os

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for key in [k for k in os.environ if k.startswith("INTERNETNL_")]:
        monkeypatch.delenv(key, raising=False)
    yield home
    leftovers = sorted(str(p.relative_to(home)) for p in home.rglob("*"))
    assert leftovers == [], f"test wrote into $HOME: {leftovers}"
