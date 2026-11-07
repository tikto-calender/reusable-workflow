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


def read_record(path):
    if not path.is_file():
        raise ValueError(f"Missing {path}. Deploy the component to Dev before promoting it.")
    return json.loads(path.read_text(encoding="utf-8"))


def write_record(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def image_patch(environment, component):
    return Path(f"apps/tikto/overlays/{environment}/{component}-image.yaml")


def read_image_patch(path):
    content = path.read_text(encoding="utf-8")
    matches = list(IMAGE_FIELD.finditer(content))
    if len(matches) != 1:
        raise ValueError(f"Expected one image field in {path}.")
    return content, matches[0]


def update_image(path, image):
    content, match = read_image_patch(path)
    path.write_text(content[:match.start(2)] + image + content[match.end(2):], encoding="utf-8")


def validate_record(record, component, owner):
    if not isinstance(record, dict) or set(record) != {"source_repository", "source_sha", "image"}:
        raise ValueError("Invalid release record fields.")
    if not all(isinstance(value, str) for value in record.values()):
        raise ValueError("Release record values must be strings.")
    expected_repo = f"{owner}/{SOURCE_REPOSITORIES[component]}"
    image_prefix = re.escape(f"ghcr.io/{owner}/{component}@sha256:")
    if record["source_repository"].lower() != expected_repo or not SHA.fullmatch(record["source_sha"]):
        raise ValueError(f"Invalid source repository or commit for {component}.")
    if not re.fullmatch(image_prefix + r"[a-f0-9]{64}", record["image"]):
        raise ValueError(f"Invalid GHCR digest for {component}.")


def require_image(path, image):
    _, match = read_image_patch(path)
    if match.group(2) != image:
        raise ValueError(f"Image in {path} does not match the release record.")


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
    record = {"source_repository": repository, "source_sha": sha, "image": image}
    validate_record(record, component, owner)
    patch = image_patch("dev", component)
    record_path = Path(f"releases/dev/{component}.json")

    def prepare():
        latest_sha = run("gh", "api", f"repos/{repository}/branches/dev", "--jq", ".commit.sha")
        if latest_sha != sha:
            print("A newer Dev commit superseded this build.")
            return []
        update_image(patch, image)
        write_record(record_path, record)
        return [patch, record_path]

    publish_update(prepare, f"dev: deploy {component} {sha}")


def production_paths(component, version):
    return (
        image_patch("prod", component),
        Path(f"releases/{component}/{version}.json"),
        Path(f"releases/prod/{component}.txt"),
    )


def verify_prod(component, version, owner):
    patch, record_path, active_version = production_paths(component, version)
    record = read_record(record_path)
    validate_record(record, component, owner)
    if not active_version.is_file() or active_version.read_text(encoding="utf-8").strip() != version:
        raise ValueError(f"{version} is not the active Prod version for {component}.")
    require_image(patch, record["image"])
    return record


def promote(component, version, owner):
    patch, record_path, active_version = production_paths(component, version)
    dev_record = Path(f"releases/dev/{component}.json")

    if record_path.exists():
        verify_prod(component, version, owner)
        print(f"{component} {version} is already promoted.")
        return

    selected = read_record(dev_record)
    validate_record(selected, component, owner)
    require_image(image_patch("dev", component), selected["image"])
    original_patch = patch.read_bytes()
    original_version = active_version.read_bytes() if active_version.is_file() else None
    subprocess.run(["docker", "manifest", "inspect", selected["image"]],
                   stdout=subprocess.DEVNULL, check=True)

    def prepare():
        if record_path.exists():
            if verify_prod(component, version, owner) != selected:
                raise ValueError(f"{version} already identifies a different build.")
            print(f"{component} {version} is already promoted.")
            return []
        if read_record(dev_record) != selected:
            raise ValueError("Dev changed while promoting. Review the new Dev image before trying again.")
        current_version = active_version.read_bytes() if active_version.is_file() else None
        if patch.read_bytes() != original_patch or current_version != original_version:
            raise ValueError("Prod changed while promoting. Review Production before trying again.")
        require_image(image_patch("dev", component), selected["image"])
        update_image(patch, selected["image"])
        write_record(record_path, selected)
        active_version.parent.mkdir(parents=True, exist_ok=True)
        active_version.write_text(version + "\n", encoding="utf-8")
        return [patch, record_path, active_version]

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
    if not args.version or not VERSION.fullmatch(args.version):
        parser.error("version must use the vMAJOR.MINOR.PATCH format")
    if args.operation == "promote":
        promote(args.component, args.version, owner)
        return

    record = verify_prod(args.component, args.version, owner)
    if record["source_repository"].lower() != os.environ["GITHUB_REPOSITORY"].lower():
        raise ValueError("Release belongs to a different source repository.")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        print(f"source_sha={record['source_sha']}", file=output)
        print(f"image={record['image']}", file=output)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit(f"Release check failed: {error}")
