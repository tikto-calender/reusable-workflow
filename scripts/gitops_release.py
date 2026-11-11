"""Update a GitOps checkout or verify its active production release."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


SOURCE_REPOSITORIES = {
    "frontend": "frontend",
    "backend": "backend",
    "notification": "notification-service",
}
IMAGE_FIELD = re.compile(r"(?m)^( +image: )([^\s]+)$")
VERSION = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
SHA = re.compile(r"[a-f0-9]{40}")


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def image_patch(environment, component):
    chart = Path("charts/tikto/environments")
    if chart.is_dir():
        return chart / environment / f"{component}-image.yaml"
    # Keep supporting manifest checkouts from before the Helm migration.
    root = Path("overlays")
    if not root.is_dir():
        root = Path("apps/tikto/overlays")
    return root / environment / f"{component}-image.yaml"


def read_image_patch(path):
    content = path.read_text(encoding="utf-8")
    matches = list(IMAGE_FIELD.finditer(content))
    if len(matches) != 1:
        raise ValueError(f"Expected one image field in {path}.")
    return content, matches[0]


def update_image(path, image):
    content, match = read_image_patch(path)
    path.write_text(content[:match.start(2)] + image + content[match.end(2):], encoding="utf-8")


def image_repository(component, owner):
    return f"ghcr.io/{owner}/{component}"


def read_image(path):
    return read_image_patch(path)[1].group(2)


def validate_digest_image(image, component, owner):
    prefix = re.escape(image_repository(component, owner))
    if not re.fullmatch(prefix + r"@sha256:[a-f0-9]{64}", image):
        raise ValueError(f"Expected a GHCR image pinned by digest for {component}.")


def inspect_manifest(image, allow_missing=False):
    result = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", image, "--format", "{{json .Manifest}}"],
        capture_output=True, text=True,
    )
    if result.returncode:
        if allow_missing and (
            "manifest unknown" in result.stderr.lower() or f"{image}: not found" in result.stderr
        ):
            return None
        raise ValueError(f"Cannot inspect {image}: {result.stderr.strip()}")
    manifest = json.loads(result.stdout)
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", manifest.get("digest", "")):
        raise ValueError(f"Registry returned an invalid digest for {image}.")
    return manifest["digest"]


def source_revision(image, component, owner):
    config = json.loads(run("docker", "buildx", "imagetools", "inspect", image,
                            "--format", "{{json .Image}}"))
    labels = config.get("config", {}).get("Labels") or {}
    sha = labels.get("org.opencontainers.image.revision", "")
    source = labels.get("org.opencontainers.image.source", "")
    expected_source = f"https://github.com/{owner}/{SOURCE_REPOSITORIES[component]}"
    if not SHA.fullmatch(sha) or source.lower().removesuffix(".git") != expected_source:
        raise ValueError(f"Missing or invalid OCI source labels on {image}. Build it again through Dev CD.")
    return sha


def require_image(path, image):
    _, match = read_image_patch(path)
    if match.group(2) != image:
        raise ValueError(f"Image in {path} changed. Review the deployment before trying again.")


def publish_update(prepare_update, message):
    """Reapply changes to fresh main if another writer wins the push."""
    for attempt in range(3):
        base = run("git", "rev-parse", "HEAD")
        paths = prepare_update()
        if not paths:
            return
        run("git", "add", "--", *map(str, paths))
        if not run("git", "diff", "--cached", "--name-only"):
            print("Manifest already up to date.")
            return
        run("git", "-c", "user.name=github-actions[bot]",
            "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "commit", "-m", message)
        if subprocess.run(["git", "push", "origin", "HEAD:refs/heads/main"]).returncode == 0:
            return
        run("git", "fetch", "origin", "refs/heads/main")
        if run("git", "rev-parse", "FETCH_HEAD") == base:
            raise ValueError("Push failed without a concurrent update. Check token and branch permissions.")
        if attempt < 2:
            run("git", "reset", "--hard", "FETCH_HEAD")
            time.sleep(attempt + 1)
    raise ValueError("Manifest main kept changing. Run the workflow again.")


def update_dev(component, owner):
    repository = os.environ["GITHUB_REPOSITORY"]
    sha = os.environ["GITHUB_SHA"]
    image = os.environ["IMAGE"]
    validate_digest_image(image, component, owner)
    if repository.lower() != f"{owner}/{SOURCE_REPOSITORIES[component]}" or not SHA.fullmatch(sha):
        raise ValueError("Invalid source repository or commit.")
    patch = image_patch("dev", component)

    def prepare():
        latest_sha = run("gh", "api", f"repos/{repository}/branches/dev", "--jq", ".commit.sha")
        if latest_sha != sha:
            print("A newer Dev commit superseded this build.")
            return []
        update_image(patch, image)
        return [patch]

    publish_update(prepare, f"dev: deploy {component} {sha}")


def verify_prod(component, version, owner):
    tag = f"{image_repository(component, owner)}:{version}"
    image = f"{tag}@{inspect_manifest(tag)}"
    require_image(image_patch("prod", component), image)
    sha = source_revision(image, component, owner)
    return {"source_sha": sha, "image": image}


def promote(component, version, owner):
    patch = image_patch("prod", component)
    if component == "backend" and patch.with_name("backend-slot").is_file():
        raise ValueError("Use Promote Backend (blue/green) in the manifest repository.")
    dev_patch = image_patch("dev", component)
    selected = read_image(dev_patch)
    validate_digest_image(selected, component, owner)
    digest = selected.split("@", 1)[1]
    tag = f"{image_repository(component, owner)}:{version}"
    image = f"{tag}@{digest}"
    existing_digest = inspect_manifest(tag, allow_missing=True)
    original_prod = read_image(patch)

    if existing_digest:
        existing_image = f"{tag}@{existing_digest}"
        if original_prod == existing_image:
            source_revision(existing_image, component, owner)
            print(f"{component} {version} is already promoted.")
            return
        if existing_digest != digest:
            raise ValueError(f"{version} already identifies a different image.")
        if run("git", "log", "-1", "--format=%H", "-S", existing_image, "--", str(patch)):
            raise ValueError(f"{version} was already deployed and Prod has since changed. Use a new version.")

    source_revision(selected, component, owner)
    if not existing_digest:
        run("docker", "buildx", "imagetools", "create", "--prefer-index=false",
            "--tag", tag, selected)
    if inspect_manifest(tag) != digest:
        raise ValueError("The version tag does not match the selected Dev digest.")

    def prepare():
        if read_image(patch) == image:
            print(f"{component} {version} is already promoted.")
            return []
        require_image(patch, original_prod)
        require_image(dev_patch, selected)
        update_image(patch, image)
        return [patch]

    publish_update(prepare, f"prod: promote {component} {version}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("update-dev", "promote", "verify-prod"))
    parser.add_argument("component", choices=SOURCE_REPOSITORIES)
    parser.add_argument("version", nargs="?")
    args = parser.parse_args()
    owner = os.environ["GITHUB_REPOSITORY_OWNER"].lower()

    if args.operation == "update-dev":
        update_dev(args.component, owner)
        return
    version = "v" + (args.version or "").strip().removeprefix("v")
    if not VERSION.fullmatch(version):
        parser.error("enter a version such as 1.2.3 or v1.2.3")
    if args.operation == "promote":
        promote(args.component, version, owner)
        return

    record = verify_prod(args.component, version, owner)
    if f"{owner}/{SOURCE_REPOSITORIES[args.component]}" != os.environ["GITHUB_REPOSITORY"].lower():
        raise ValueError("Release belongs to a different source repository.")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        print(f"version={version}", file=output)
        print(f"source_sha={record['source_sha']}", file=output)
        print(f"image={record['image']}", file=output)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit(f"Release check failed: {error}")
