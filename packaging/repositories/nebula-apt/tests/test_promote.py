import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PromotionTests(unittest.TestCase):
    def run_promotion(
        self,
        channels: Path,
        *,
        channel: str,
        tag: str,
        digest: str = "a" * 64,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                ROOT / "scripts" / "promote.py",
                "--channels",
                channels,
                "--channel",
                channel,
                "--tag",
                tag,
                "--sha256",
                digest,
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_promotion_keeps_current_and_previous(self):
        with tempfile.TemporaryDirectory() as directory:
            channels = Path(directory) / "channels.json"
            channels.write_text((ROOT / "channels.json").read_text(), encoding="utf-8")

            for version, digest in (
                ("3.0.0-alpha.7", "a" * 64),
                ("3.0.0-alpha.9", "b" * 64),
                ("3.0.0-alpha.10", "c" * 64),
            ):
                result = self.run_promotion(
                    channels,
                    channel="prerelease",
                    tag=f"nebula-v{version}",
                    digest=digest,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

            entries = json.loads(channels.read_text())["channels"]["prerelease"]
            self.assertEqual(
                [entry["version"] for entry in entries],
                ["3.0.0-alpha.10", "3.0.0-alpha.9"],
            )

    def test_stable_and_prerelease_channels_must_match_version(self):
        with tempfile.TemporaryDirectory() as directory:
            channels = Path(directory) / "channels.json"
            channels.write_text((ROOT / "channels.json").read_text(), encoding="utf-8")

            stable = self.run_promotion(
                channels,
                channel="stable",
                tag="nebula-v3.1.0",
            )
            self.assertEqual(stable.returncode, 0, stable.stderr)

            mismatch = self.run_promotion(
                channels,
                channel="stable",
                tag="nebula-v3.1.1-rc.1",
            )
            self.assertNotEqual(mismatch.returncode, 0)
            self.assertIn("does not belong in stable", mismatch.stderr)

    def test_invalid_tag_and_digest_are_rejected_without_changing_channels(self):
        with tempfile.TemporaryDirectory() as directory:
            channels = Path(directory) / "channels.json"
            original = (ROOT / "channels.json").read_text()
            channels.write_text(original, encoding="utf-8")

            bad_tag = self.run_promotion(
                channels,
                channel="stable",
                tag="nebula-v2.9.0",
            )
            self.assertNotEqual(bad_tag.returncode, 0)

            bad_digest = self.run_promotion(
                channels,
                channel="stable",
                tag="nebula-v3.1.0",
                digest="ABC",
            )
            self.assertNotEqual(bad_digest.returncode, 0)
            self.assertEqual(channels.read_text(), original)

    def test_promotion_rejects_downgrade_and_immutable_tag_change(self):
        with tempfile.TemporaryDirectory() as directory:
            channels = Path(directory) / "channels.json"
            channels.write_text((ROOT / "channels.json").read_text(), encoding="utf-8")

            first = self.run_promotion(
                channels,
                channel="prerelease",
                tag="nebula-v3.0.0-alpha.10",
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            promoted = channels.read_text()

            downgrade = self.run_promotion(
                channels,
                channel="prerelease",
                tag="nebula-v3.0.0-alpha.9",
            )
            self.assertNotEqual(downgrade.returncode, 0)
            self.assertIn("promotion must advance", downgrade.stderr)
            self.assertEqual(channels.read_text(), promoted)

            changed_digest = self.run_promotion(
                channels,
                channel="prerelease",
                tag="nebula-v3.0.0-alpha.10",
                digest="b" * 64,
            )
            self.assertNotEqual(changed_digest.returncode, 0)
            self.assertIn("conflicts with an immutable channel entry", changed_digest.stderr)
            self.assertEqual(channels.read_text(), promoted)

    def test_channel_policy_rejects_unsafe_asset_path(self):
        with tempfile.TemporaryDirectory() as directory:
            channels = Path(directory) / "channels.json"
            channels.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "channels": {
                            "stable": [
                                {
                                    "tag": "nebula-v3.1.0",
                                    "version": "3.1.0",
                                    "asset": "../../private.key",
                                    "sha256": "a" * 64,
                                }
                            ],
                            "prerelease": [],
                        },
                    }
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    ROOT / "scripts" / "channel_policy.py",
                    channels,
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unexpected asset", result.stderr)

    def test_checksum_verifier_selects_the_exact_deb_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            asset = root / "Nebula-3.0.0-alpha.5-linux-x86_64.deb"
            asset.write_bytes(b"managed deb")
            digest = hashlib.sha256(asset.read_bytes()).hexdigest()
            checksums = root / "SHA256SUMS-linux-x64.txt"
            checksums.write_text(
                "\n".join(
                    (
                        f"{digest}  {asset.name}",
                        f"{'b' * 64}  {asset.name}.cyclonedx.json",
                        f"{'c' * 64}  {asset.name}.spdx.json",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    ROOT / "scripts" / "verify_release_checksum.py",
                    "--checksums",
                    checksums,
                    "--asset",
                    asset,
                    "--sha256",
                    digest,
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            mismatch = subprocess.run(
                [
                    sys.executable,
                    ROOT / "scripts" / "verify_release_checksum.py",
                    "--checksums",
                    checksums,
                    "--asset",
                    asset,
                    "--sha256",
                    "d" * 64,
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(mismatch.returncode, 0)
            self.assertIn("digest disagreement", mismatch.stderr)


if __name__ == "__main__":
    unittest.main()
