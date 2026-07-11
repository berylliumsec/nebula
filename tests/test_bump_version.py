import pytest

from bump_version import bump_version


@pytest.mark.parametrize(
    ("current", "bump_type", "expected"),
    [
        ("2.0.0b31", "prerelease", "2.0.0b32"),
        ("2.0.0b31", "release", "2.0.0"),
        ("2.0.0", "prerelease", "2.0.1b0"),
        ("2.0.0", "patch", "2.0.1"),
    ],
)
def test_bump_version(current, bump_type, expected):
    assert bump_version(current, bump_type) == expected


def test_bump_version_rejects_invalid_version():
    with pytest.raises(ValueError, match="does not match"):
        bump_version("not-a-version", "prerelease")
