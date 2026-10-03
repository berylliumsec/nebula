from html.parser import HTMLParser
from pathlib import Path


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self.current_href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.current_href = dict(attrs).get("href")

    def handle_data(self, data: str) -> None:
        if self.current_href and data.strip():
            self.links.append((data.strip(), self.current_href))

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self.current_href = None


def test_public_site_leads_to_signed_apt_installation() -> None:
    site = Path(__file__).resolve().parents[2] / "index.html"
    links = _Links()
    links.feed(site.read_text(encoding="utf-8"))

    assert (
        "Install Nebula",
        "https://berylliumsec.github.io/nebula-apt/",
    ) in links.links
