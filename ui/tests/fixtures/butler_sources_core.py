"""Real Core with an inert URL-document transport for Sources acceptance.

The production API, persistence, index and pairing are used. The one upstream
document is synthetic; this fixture performs no external URL requests.
"""

from typing import Any
from urllib.parse import urlsplit

from nebula.v3 import cli
from nebula.v3.knowledge import FetchedUrlDocument, SourceFetchError


_create_app = cli.create_app


def fetch_document(url: str) -> FetchedUrlDocument:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "source-fixture.invalid"
        or parsed.path != "/document.txt"
    ):
        raise SourceFetchError("The inert document fixture is unavailable")
    return FetchedUrlDocument(
        data=b"Synthetic project document: immutable local acceptance content.",
        filename="url-document.txt",
        media_type="text/plain",
        source_url="https://source-fixture.invalid/document.txt",
    )


def create_fixture_app(*args: Any, **kwargs: Any) -> Any:
    kwargs["knowledge_url_fetcher"] = fetch_document
    return _create_app(*args, **kwargs)


if __name__ == "__main__":
    cli.create_app = create_fixture_app
    cli.main()
