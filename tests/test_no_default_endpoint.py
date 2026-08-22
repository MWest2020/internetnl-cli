from pathlib import Path


def test_no_hardcoded_internet_nl_endpoint():
    src = Path(__file__).parent.parent / "src" / "internetnl_cli"
    for path in src.rglob("*.py"):
        text = path.read_text().lower()
        assert "internet.nl" not in text, f"{path} contains a hardcoded endpoint reference"
