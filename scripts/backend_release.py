"""Promote a tested Dev backend digest through the inactive Prod slot."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import json
import math
import os
import signal
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

from gitops_release import (
    VERSION,
    image_patch,
    image_repository,
    inspect_manifest,
    publish_update,
    read_image,
    require_image,
    run,
    source_revision,
    update_image,
    validate_digest_image,
)


def aws(service, operation, *arguments):
    return json.loads(run("aws", service, operation, *arguments, "--output", "json"))


def rule_target(rule):
    actions = aws("elbv2", "describe-rules", "--rule-arns", rule)["Rules"][0]["Actions"]
    if len(actions) != 1 or actions[0]["Type"] != "forward":
        raise ValueError("Expected one forwarding action on the production backend rule.")
    action = actions[0]
    groups = action.get("ForwardConfig", {}).get("TargetGroups", [])
    if len(groups) == 1 and groups[0].get("Weight", 1) > 0:
        return groups[0]["TargetGroupArn"]
    if not groups and action.get("TargetGroupArn"):
        return action["TargetGroupArn"]
    raise ValueError("Expected the production backend rule to forward to one slot.")


def switch(rule, target):
    aws("elbv2", "modify-rule", "--rule-arn", rule, "--actions",
        json.dumps([{"Type": "forward", "TargetGroupArn": target}]))
    if rule_target(rule) != target:
        raise ValueError("ALB rule does not point to the requested slot.")


def targets_ready(target):
    targets = aws("elbv2", "describe-target-health", "--target-group-arn", target)
    states = [item["TargetHealth"]["State"] for item in targets["TargetHealthDescriptions"]]
    return len(states) == 2 and all(state == "healthy" for state in states)


def api(url, path, slot=None):
    headers = {"X-TikTo-Preview": slot} if slot else {}
    with urlopen(Request(url + path, headers=headers), timeout=5) as response:
        return json.load(response)["data"]


def smoke(url, image, slot=None):
    if api(url, "/api/runtime", slot).get("image") != image:
        raise ValueError("Request reached a backend with a different image digest.")
    status = api(url, "/api/status", slot)
    if status.get("database", {}).get("status") != "ok":
        raise ValueError("Backend cannot read the planner database.")


def wait_for_preview(url, image, slot, target):
    deadline = time.monotonic() + 480
    consecutive = 0
    while time.monotonic() < deadline:
        try:
            if not targets_ready(target):
                raise ValueError("Waiting for two healthy targets and old targets to drain.")
            smoke(url, image, slot)
            consecutive += 1
            if consecutive >= 5:
                return
        except (URLError, TimeoutError, ValueError, KeyError) as error:
            consecutive = 0
            print(f"Preview pending: {error}", flush=True)
        time.sleep(5)
    raise ValueError("Preview never passed smoke tests for the selected image.")


def metric(load_balancer, target, name, start, end):
    result = aws("cloudwatch", "get-metric-statistics",
                 "--namespace", "AWS/ApplicationELB", "--metric-name", name,
                 "--dimensions", f"Name=TargetGroup,Value={target.split(':')[-1]}",
                 f"Name=LoadBalancer,Value={load_balancer}",
                 "--period", "60", "--statistics", "Sum",
                 "--start-time", start.isoformat(), "--end-time", end.isoformat())
    return sum(point["Sum"] for point in result["Datapoints"])


def observe(url, image, rule, target):
    # Use complete one-minute CloudWatch buckets after ALB propagation.
    deadline = time.monotonic() + 30
    while True:
        try:
            smoke(url, image)
            break
        except (URLError, TimeoutError, ValueError, KeyError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)
    start = datetime.now(UTC).replace(second=0, microsecond=0) + timedelta(minutes=1)
    time.sleep(max(0, (start - datetime.now(UTC)).total_seconds()))
    end = start + timedelta(minutes=2)
    latencies = []
    failures = 0

    def sample():
        before = time.monotonic()
        try:
            smoke(url, image)
        except (URLError, TimeoutError, ValueError, KeyError):
            return None
        return time.monotonic() - before

    with ThreadPoolExecutor(max_workers=4) as executor:
        while datetime.now(UTC) + timedelta(seconds=10) < end:
            for duration in executor.map(lambda _: sample(), range(4)):
                if duration is None:
                    failures += 1
                else:
                    latencies.append(duration)
            if failures:
                raise ValueError(f"{failures} synthetic checks failed after cutover.")
            time.sleep(4)

    if len(latencies) < 40:
        raise ValueError("Too few successful synthetic samples.")
    p95 = sorted(latencies)[math.ceil(len(latencies) * .95) - 1]
    print(f"Synthetic samples={len(latencies)} p95={p95:.3f}s", flush=True)
    if p95 > 1.0:
        raise ValueError("Synthetic p95 exceeded the 1 second release threshold.")

    time.sleep(max(0, (end - datetime.now(UTC)).total_seconds()))
    load_balancer = "/".join(rule.split(":")[-1].split("/")[1:4])
    for _ in range(9):
        requests = metric(load_balancer, target, "RequestCount", start, end)
        if requests >= len(latencies) * 2:
            errors = metric(load_balancer, target, "HTTPCode_Target_5XX_Count", start, end)
            print(f"Green/Blue candidate target: requests={requests:g} 5xx={errors:g}", flush=True)
            if errors / requests > .01:
                raise ValueError("Candidate target group exceeded 1% HTTP 5xx.")
            return
        time.sleep(20)
    raise ValueError("CloudWatch did not report enough candidate traffic.")


def promote(version, owner, dev_url, url, rule, targets):
    if not VERSION.fullmatch(version):
        raise ValueError("Version must use vMAJOR.MINOR.PATCH.")
    prod = image_patch("prod", "backend")
    dev = image_patch("dev", "backend")
    slot_path = prod.with_name("backend-slot")
    stable = slot_path.read_text().strip()
    if stable not in targets or not all(targets.values()) or targets["blue"] == targets["green"]:
        raise ValueError("Expected two distinct target groups and a valid stable slot.")
    candidate = "green" if stable == "blue" else "blue"
    stable_path = prod.with_name(f"{stable}-image.yaml")
    candidate_path = prod.with_name(f"{candidate}-image.yaml")
    old = read_image(prod)
    require_image(stable_path, old)

    live = rule_target(rule)
    if live not in targets.values():
        raise ValueError("ALB is pointing to an unknown target group.")
    if live != targets[stable]:
        if not targets_ready(targets[stable]):
            raise ValueError("Cannot recover an interrupted cutover: stable slot is unhealthy.")
        switch(rule, targets[stable])
        print("Restored the last verified slot after an interrupted cutover.", flush=True)
    if not targets_ready(targets[stable]):
        raise ValueError("Stable slot needs two healthy targets before release.")

    selected = read_image(dev)
    validate_digest_image(selected, "backend", owner)
    tag = f"{image_repository('backend', owner)}:{version}"
    digest = selected.split("@", 1)[1]
    image = f"{tag}@{digest}"
    registered = inspect_manifest(tag, allow_missing=True)
    if registered and old == f"{tag}@{registered}":
        print(f"{version} is already active.")
        return
    if registered and registered != digest:
        raise ValueError("Version tag already identifies another digest.")
    if run("git", "log", "-1", "--format=%H", "-S", tag + "@", "--", str(prod),
           "overlays/prod/backend-image.yaml", "apps/tikto/overlays/prod/backend-image.yaml"):
        raise ValueError("Version was previously promoted. Select a new version.")

    smoke(dev_url, selected)
    source_revision(selected, "backend", owner)
    if not registered:
        run("docker", "buildx", "imagetools", "create", "--prefer-index=false",
            "--tag", tag, selected)
    if inspect_manifest(tag) != digest:
        raise ValueError("Registry tag does not match the selected Dev digest.")

    def stage():
        require_image(prod, old)
        require_image(dev, selected)
        update_image(candidate_path, image)
        return [candidate_path]

    publish_update(stage, f"prod: stage backend {version} in {candidate}")
    wait_for_preview(url, image, candidate, targets[candidate])

    def finalize():
        require_image(prod, old)
        require_image(candidate_path, image)
        update_image(prod, image)
        slot_path.write_text(f"{candidate}\n", encoding="utf-8")
        return [prod, slot_path]

    finalizing = False
    try:
        switch(rule, targets[candidate])
        observe(url, image, rule, targets[candidate])
        finalizing = True
        publish_update(finalize, f"prod: activate backend {version} in {candidate}")
    except (Exception, KeyboardInterrupt):
        # A push may have succeeded even if its response was lost. Check remote state
        # before restoring traffic so the listener and the confirmed Git slot agree.
        recorded = stable
        if finalizing:
            run("git", "fetch", "origin", "refs/heads/main")
            recorded = run("git", "show", f"FETCH_HEAD:{slot_path.as_posix()}")
        if recorded == stable:
            switch(rule, targets[stable])
            print(f"Restored production traffic to {stable}.", flush=True)
        elif recorded != candidate:
            raise ValueError("Unknown stable slot in manifest main. Inspect production.")
        raise


def stop_release(_signal, _frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop_release)
    try:
        promote(os.environ["VERSION"], os.environ["GITHUB_REPOSITORY_OWNER"].lower(),
                os.environ["DEV_URL"].rstrip("/"), os.environ["APP_URL"].rstrip("/"), os.environ["RULE_ARN"],
                {"blue": os.environ["BLUE_ARN"], "green": os.environ["GREEN_ARN"]})
    except (OSError, ValueError, KeyError, KeyboardInterrupt, subprocess.CalledProcessError) as error:
        sys.exit(f"Backend release failed: {error}")
