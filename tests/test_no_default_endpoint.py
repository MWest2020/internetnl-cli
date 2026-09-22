from pathlib import Path


def test_no_hardcoded_internet_nl_endpoint():
    # `internet.nl` itself is the API's own product name and is fine to
    # mention (e.g. findings.py's `"internet.nl batch v2"` source label,
    # runs/04-herkomst-in-de-kop.md) — what must never be compiled in is
    # the actual default endpoint host, `batch.internet.nl` (see README's
    # `INTERNETNL_ENDPOINT=https://batch.internet.nl/...` example and
    # config.py's "no default endpoint is ever compiled in").
    src = Path(__file__).parent.parent / "src" / "internetnl_cli"
    for path in src.rglob("*.py"):
        text = path.read_text().lower()
        assert "batch.internet.nl" not in text, f"{path} contains a hardcoded endpoint reference"
