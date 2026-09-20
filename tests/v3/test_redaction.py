import pytest

from nebula.v3.redaction import redact_text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "GET https://generativelanguage.googleapis.com/v1beta/models"
            "?key=AIzaSyD-abcdefghijklmnopqrstuvwxyz01234 404",
            "GET https://generativelanguage.googleapis.com/v1beta/models"
            "?key=[REDACTED TOKEN] 404",
        ),
        (
            "fetch https://api.example.test/v1/items?token=9f8e7d6c5b4a3f2e1d0c&page=2",
            "fetch https://api.example.test/v1/items?token=[REDACTED]&page=2",
        ),
        (
            "curl -H 'Authorization: Basic dXNlcjpwYXNzd29yZA==' https://example.test",
            "curl -H 'Authorization: Basic [REDACTED]' https://example.test",
        ),
        (
            "Authorization: Basic dXNlcjpwYXNz",
            "Authorization: Basic [REDACTED]",
        ),
        (
            "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "AWS_SECRET_ACCESS_KEY=[REDACTED]",
        ),
        (
            "slack token xoxb-1234567890-abcdefghijkl expired",
            "slack token [REDACTED TOKEN] expired",
        ),
        (
            "use github_pat_11ABCDEFG0123456789abcdefghijklmnopqrstuvwxyz to clone",
            "use [REDACTED TOKEN] to clone",
        ),
        (
            "token: 1234567890abcdef",
            "token: [REDACTED]",
        ),
    ],
)
def test_common_secret_shapes_are_redacted(text: str, expected: str) -> None:
    assert redact_text(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Basic authentication is enabled for the proxy",
        "HTTP Basic Authentication failed",
        "max_tokens=4096 and the token count exceeded the window",
        "https://example.test/search?q=firewall&page=2",
        "ghp_abcdefghijklmnopqrstuvwxyz0123 stays a known token",
    ],
)
def test_ordinary_text_is_left_alone(text: str) -> None:
    expected = text.replace("ghp_abcdefghijklmnopqrstuvwxyz0123", "[REDACTED TOKEN]")
    assert redact_text(text) == expected
