# Shared CI/CD workflows

Application repositories own triggers and inputs; this repository owns the jobs.

| Workflow                | Purpose                                                                                          |
| ----------------------- | ------------------------------------------------------------------------------------------------ |
| `node-ci.yml`           | Parallel format, lint, test, build/syntax checks; one `CI result` gate                           |
| `python-ci.yml`         | Parallel format, lint, test, compile checks; one `CI result` gate                                |
| `container-publish.yml` | Build, Trivy scan, push immutable `sha-<full SHA>` to ECR; return digest URI                     |
| `ecs-deploy.yml`        | Register task revision, update service, wait and verify requested revision, optional HTTPS smoke |
| `container-release.yml` | Validate release tag and successful dev CD, promote digest, open production GitOps PR            |
| `frontend-delivery.yml` | Build/deploy S3 on dev; promote the archived dev artifact on release tags                        |

`sonarqube.yml` and `dependabot-auto-merge.yml` remain available for other callers;
the basic application pipelines do not use automatic merging.

## Shared EKS GitOps

Frontend, Backend and Notification use `ghcr-dev.yml` to build, scan and publish an image to GHCR, then update their own Dev image patch on the manifest repository's `dev` branch. The source repository grants the workflow `packages: write`. A `GITOPS_TOKEN` scoped to the manifest repository grants Contents write and Pull requests write. Make the GHCR packages public for anonymous pulls.

After verifying the running Dev digest, use `gitops-promote.yml` to open a PR for that digest into the manifest repository's protected `main` branch. Argo CD syncs Dev and Prod from their respective branches. Frontend pods use the same ALB hostname as their backend API.

## Legacy ECS and ECR flow

```text
PR -> dev: parallel quality checks -> CI result -> manual merge
Push dev: build -> scan -> ECR -> ECS dev -> verify rollout
Tag vX.Y.Z: validate tested commit on main -> approve prod -> promote digest -> GitOps PR
Merge GitOps PR: Argo CD syncs production EKS -> inspect rollout health
Frontend: build -> S3 dev; release downloads same build -> S3 prod -> smoke
```

CI only runs on application PRs into `dev`. CD runs for every dev push, including
documentation changes, so every releasable commit has an unambiguous image/build.
Independent CI tasks run on separate runners; each installs dependencies with a
package-manager cache. A failed task does not cancel the other diagnostics.

## Callers and publication

Callers contain only events, permissions, concurrency and `uses`/inputs. Publish
this repository first. The workspace callers currently reference `@main` because
these changes have no published shared commit yet. Before production rollout,
replace `@main` with the published shared commit SHA (or maintain a protected
version tag). Do not invent a SHA for unpublished local changes.

Node CI retains its command/version/working-directory/coverage inputs. Python CI
retains equivalent inputs. Empty commands are skipped. CI uses the PR merge
result; CD checks out the dev push; release checks out the tagged commit.

## Required settings

| Scope                            | Setting                                                                                        |
| -------------------------------- | ---------------------------------------------------------------------------------------------- |
| All repos                        | `AWS_REGION=ap-southeast-2`                                                                    |
| BE/notification repo             | `AWS_BUILD_ROLE_ARN` from shared Terraform                                                     |
| BE/notification dev Environment  | `AWS_DEPLOY_ROLE_ARN`, `ECS_CLUSTER`, `ECS_SERVICE`                                            |
| BE/notification dev Environment  | Optional `SMOKE_URL`, returning HTTPS JSON with `status: ok`                                   |
| BE/notification prod Environment | `GITOPS_TOKEN`: fine-grained token with Contents/Pull requests write on the GitOps repo        |
| BE/notification prod Environment | Optional `GITOPS_REPOSITORY`; defaults to `Flavoriy/gitops-manifest`                           |
| FE dev and prod Environments     | `AWS_DEPLOY_ROLE_ARN`, `FRONTEND_BUCKET_NAME`, `CLOUDFRONT_DISTRIBUTION_ID`, `APPLICATION_URL` |

Backend/notification production is EKS, not ECS. The release caller identifies the
existing container in the GitOps patch (`tikto` for backend, `notification` for
notification). Review the production workload configuration when merging the PR.
GitOps base/overlay/patch can be overridden through reusable inputs.

The build role needs ECR push, describe and pull/manifest access. Trust dev branch
OIDC subjects for build and prod Environment subjects for promotion. ECS deploy
uses the dev Environment role and needs DescribeServices, DescribeTaskDefinition,
RegisterTaskDefinition, UpdateService and PassRole for its ECS roles. Terraform
contains these policies; apply the updated shared/dev stacks before using CD.

Protect `dev` with `checks / CI result` and up-to-date branches. Protect `main`,
version tags and `prod` approval. Restrict `dev` deployments to dev branch and
`prod` deployments to release tags using GitHub Environment policies. For source
promotion, preserve the tested SHA (fast-forward main); a squash/merge-created
commit without its own dev deployment cannot be released.

## Release and retry

After dev testing, fast-forward main to the tested commit, push main, then push
`vMAJOR.MINOR.PATCH` pointing to that exact commit. Pushing main does not deploy.
Release requires a successful `cd.yml` push run for that SHA on dev. It never
rebuilds an image or the frontend. Existing image version tags must match the
source digest. Retrying promotion updates the same GitOps branch/PR.

Frontend artifacts are retained for 90 days (subject to repository limits).
Expired artifacts must be regenerated by rerunning dev CD before release.
Old hashed S3 assets are not deleted during deployment.

An ECS stability waiter can succeed after rollback: the workflow also requires
the requested task revision, positive desired count and completed rollout.
Service Connect sidecar/config, task roles, CPU, memory, ports and environment
are preserved from the running task definition. Changing those settings through
Terraform requires deliberately rolling out the resulting task revision; ignore
changes prevents Terraform from reverting an application deployment.

A successful container release means the production promotion PR was opened.
Argo health/analysis is the authority on actual production rollout. Revert the
GitOps image change for code rollback; data/schema rollback is a separate task.
FE rollback can restore a retained build with the same assets-before-HTML order.
