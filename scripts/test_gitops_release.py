"""Regression checks for registry-backed releases, without accessing GHCR."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import gitops_release as release


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.original_directory = Path.cwd()
        self.directory = tempfile.TemporaryDirectory()
        os.chdir(self.directory.name)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(os.chdir, self.original_directory)
        self.repository = "ghcr.io/tikto-calender/backend"
        self.digest = "sha256:" + "1" * 64
        self.dev_image = f"{self.repository}@{self.digest}"
        self.tag = f"{self.repository}:v1.2.3"
        self.prod_image = f"{self.tag}@{self.digest}"
        self.sha = "a" * 40
        self.tags = {}
        self.history = ""
        self.commands = []
        self.env = patch.dict(os.environ, GITHUB_REPOSITORY="tikto-calender/backend",
                              GITHUB_SHA=self.sha, IMAGE=self.dev_image)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.run_patch = patch.object(release, "run", side_effect=self.run_command)
        self.run_patch.start()
        self.addCleanup(self.run_patch.stop)
        self.inspect_patch = patch.object(release, "inspect_manifest", side_effect=self.inspect)
        self.inspect_patch.start()
        self.addCleanup(self.inspect_patch.stop)
        publisher = patch.object(release, "publish_update", side_effect=lambda prepare, _: prepare())
        self.publisher = publisher.start()
        self.addCleanup(publisher.stop)
        self.write_image("dev", self.dev_image)
        self.write_image("prod", "placeholder")

    def write_image(self, environment, image):
        path = Path(f"overlays/{environment}/backend-image.yaml")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"        - name: tikto\n          image: {image}\n", encoding="utf-8")

    def inspect(self, image, allow_missing=False):
        if image in self.tags:
            return self.tags[image]
        if allow_missing:
            return None
        raise ValueError("Image missing")

    def run_command(self, *args):
        self.commands.append(args)
        if args[:4] == ("docker", "buildx", "imagetools", "create"):
            self.tags[self.tag] = self.digest
            return ""
        if args[:4] == ("docker", "buildx", "imagetools", "inspect"):
            return json.dumps({"config": {"Labels": {
                "org.opencontainers.image.revision": self.sha,
                "org.opencontainers.image.source": "https://github.com/tikto-calender/backend",
            }}})
        if args[:2] == ("git", "log"):
            return self.history
        if args[0] == "gh":
            return self.sha
        raise AssertionError(args)

    def test_promote_preserves_digest_and_source_commit(self):
        release.promote("backend", "v1.2.3", "tikto-calender")
        self.assertEqual(release.read_image(release.image_patch("prod", "backend")), self.prod_image)
        self.assertIn(("docker", "buildx", "imagetools", "create", "--prefer-index=false",
                       "--tag", self.tag, self.dev_image), self.commands)
        self.assertEqual(release.verify_prod("backend", "v1.2.3", "tikto-calender"),
                         {"source_sha": self.sha, "image": self.prod_image})
        self.assertFalse(Path("releases").exists())

    def test_cli_promote_accepts_version_without_prefix(self):
        with patch.object(sys, "argv", ["gitops_release.py", "promote", "backend", " 1.2.3 "]), \
             patch.dict(os.environ, GITHUB_REPOSITORY_OWNER="tikto-calender"):
            release.main()
        self.assertEqual(self.tags, {self.tag: self.digest})

    def test_cli_verify_outputs_version_for_source_tag(self):
        self.tags[self.tag] = self.digest
        self.write_image("prod", self.prod_image)
        with patch.object(sys, "argv", ["gitops_release.py", "verify-prod", "backend", "1.2.3"]), \
             patch.dict(os.environ, GITHUB_REPOSITORY_OWNER="tikto-calender", GITHUB_OUTPUT="output.txt"):
            release.main()
        self.assertIn("version=v1.2.3", Path("output.txt").read_text(encoding="utf-8").splitlines())

    def test_retry_after_tag_created_before_manifest_push(self):
        self.tags[self.tag] = self.digest
        release.promote("backend", "v1.2.3", "tikto-calender")
        self.assertFalse(any("create" in command for command in self.commands))
        self.assertEqual(release.read_image(release.image_patch("prod", "backend")), self.prod_image)

    def test_retry_active_prod_after_dev_advanced(self):
        self.tags[self.tag] = self.digest
        self.write_image("prod", self.prod_image)
        self.write_image("dev", self.dev_image[:-64] + "2" * 64)
        release.promote("backend", "v1.2.3", "tikto-calender")
        self.assertEqual(release.verify_prod("backend", "v1.2.3", "tikto-calender")["source_sha"], self.sha)

    def test_existing_tag_cannot_identify_different_image(self):
        self.tags[self.tag] = "sha256:" + "2" * 64
        with self.assertRaisesRegex(ValueError, "different image"):
            release.promote("backend", "v1.2.3", "tikto-calender")

    def test_old_version_cannot_replace_newer_prod(self):
        self.tags[self.tag] = self.digest
        self.history = "historical-commit"
        with self.assertRaisesRegex(ValueError, "Prod has since changed"):
            release.promote("backend", "v1.2.3", "tikto-calender")

    def test_finalize_rejects_mutated_version_tag(self):
        self.tags[self.tag] = "sha256:" + "2" * 64
        self.write_image("prod", self.prod_image)
        with self.assertRaisesRegex(ValueError, "changed"):
            release.verify_prod("backend", "v1.2.3", "tikto-calender")

    def test_missing_source_labels_block_retagging(self):
        with patch.object(release, "run", return_value='{"config": {}}'):
            with self.assertRaisesRegex(ValueError, "OCI source labels"):
                release.promote("backend", "v1.2.3", "tikto-calender")
        self.assertNotIn(self.tag, self.tags)

    def test_concurrent_dev_change_blocks_manifest_update(self):
        def concurrent_update(prepare, _):
            self.write_image("dev", self.dev_image[:-64] + "2" * 64)
            prepare()
        self.publisher.side_effect = concurrent_update
        with self.assertRaisesRegex(ValueError, "changed"):
            release.promote("backend", "v1.2.3", "tikto-calender")
        self.assertEqual(release.read_image(release.image_patch("prod", "backend")), "placeholder")

    def test_dev_updates_only_image_and_skips_superseded_commits(self):
        self.assertIsNone(release.update_dev("backend", "tikto-calender"))
        self.assertFalse(Path("releases").exists())
        with patch.object(release, "run", return_value="b" * 40):
            self.write_image("dev", "placeholder")
            release.update_dev("backend", "tikto-calender")
        self.assertEqual(release.read_image(release.image_patch("dev", "backend")), "placeholder")

    def test_missing_tag_is_distinct_from_permission_error(self):
        self.inspect_patch.stop()
        for error, expected in ((f"{self.tag}: not found", None), ("denied: unauthorized", ValueError)):
            result = subprocess.CompletedProcess([], 1, "", error)
            with patch.object(subprocess, "run", return_value=result):
                if expected:
                    with self.assertRaises(expected):
                        release.inspect_manifest(self.tag, allow_missing=True)
                else:
                    self.assertIsNone(release.inspect_manifest(self.tag, allow_missing=True))

    def test_supports_manifest_layout_before_migration(self):
        Path("apps/tikto").mkdir(parents=True)
        Path("overlays").rename("apps/tikto/overlays")
        release.update_dev("backend", "tikto-calender")
        self.tags[self.tag] = self.digest
        release.promote("backend", "v1.2.3", "tikto-calender")
        self.assertEqual(release.verify_prod("backend", "v1.2.3", "tikto-calender")["source_sha"], self.sha)

    def test_updates_helm_dev_values_and_verifies_active_prod_backend(self):
        root = Path("charts/tikto/environments")
        dev = root / "dev/backend-image.yaml"
        prod = root / "prod/backend-image.yaml"
        for path, values in (
            (dev, "backend:\n  image: placeholder\n"),
            (prod, f"backend:\n  image: {self.prod_image}\n"),
        ):
            path.parent.mkdir(parents=True)
            path.write_text(values, encoding="utf-8")
        prod.with_name("backend-slot").write_text("blue\n", encoding="utf-8")
        self.tags[self.tag] = self.digest

        release.update_dev("backend", "tikto-calender")

        self.assertEqual(dev.read_text(encoding="utf-8"), f"backend:\n  image: {self.dev_image}\n")
        self.assertEqual(release.verify_prod("backend", "v1.2.3", "tikto-calender")["image"], self.prod_image)
        with self.assertRaisesRegex(ValueError, "Promote Backend"):
            release.promote("backend", "v1.2.3", "tikto-calender")


if __name__ == "__main__":
    unittest.main()
