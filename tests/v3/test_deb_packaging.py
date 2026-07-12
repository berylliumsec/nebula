import shutil
import subprocess

import pytest

from scripts.repack_deb import DebPackagingError, debian_version


@pytest.mark.parametrize(
    ("semver", "expected"),
    [
        ("3.0.0-alpha.1", "3.0.0~alpha.1"),
        ("3.0.0-rc.2+build.7", "3.0.0~rc.2+build.7"),
        ("3.0.0", "3.0.0"),
    ],
)
def test_semver_maps_to_debian_upgrade_order(semver, expected):
    assert debian_version(semver) == expected


def test_debian_prerelease_sorts_before_stable():
    if shutil.which("dpkg") is None:
        pytest.skip("dpkg is unavailable on this platform")
    assert (
        subprocess.run(
            [
                "dpkg",
                "--compare-versions",
                debian_version("3.0.0-alpha.1"),
                "lt",
                "3.0.0",
            ],
            check=False,
        ).returncode
        == 0
    )


def test_debian_version_rejects_non_semver():
    with pytest.raises(DebPackagingError):
        debian_version("3.0")
