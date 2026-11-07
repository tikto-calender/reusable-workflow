# Shared CI/CD workflows

Application repositories own triggers and inputs; this repository owns the jobs.

| Workflow | Purpose |
| --- | --- |
| `ci-node.yml` | Node.js format, lint, test, and build or syntax checks |
| `ci-python.yml` | Python format, lint, test, and compile checks |
| `ci-sonarqube.yml` | SonarQube analysis using CI coverage |
| `push-ghcr.yml` | Build and scan an image, then publish to GHCR and return its digest |
| `cd-dev.yml` | Deploy the Dev image by digest |
| `cd-prod.yml` | Retag one Dev image and deploy its pinned version to Prod |
| `finalize-release.yml` | Tag the verified source commit, create a GitHub Release, and open a source PR |
| `legacy-ecr-publish.yml` | Build, scan, and publish images to ECR |
| `release.yml` | Promote a legacy ECR digest and open a production GitOps PR |

## Shared EKS GitOps

1. CI checks pull requests into `dev` and pushes to `feat/**` when relevant files change.
   Feature PRs are merged manually. Remove legacy feature auto-merge workflows from the default branch as well as Dev.
2. Each push to `dev` builds, scans and publishes an image to GHCR with a `sha-<commit>` tag and OCI source/revision labels. CD updates only the image digest in `overlays/dev/` on manifest `main`.
   If an older bot merge suppressed the push workflow, run **Deploy Dev** manually with branch `dev`. The caller must also exist on the default branch for GitHub to expose its Run workflow button. Manual deployment of another branch is rejected.
3. After verifying Dev, run **Promote production** from manifest `main` with a service and a version. It retags the exact Dev digest as `vMAJOR.MINOR.PATCH` on GHCR, without rebuilding. It writes `ghcr.io/<owner>/<component>:<version>@sha256:<digest>` to the Prod image patch. Argo CD reads the Dev and Prod overlays on manifest `main`.
4. Check that the Prod Argo CD app is synced and healthy, confirm it runs the selected digest, and verify the application. Then run **Finalize release** from source `main` with the same version. It verifies the GHCR version tag against the pinned Prod image and reads the source SHA from its OCI labels. It tags that SHA, creates its GitHub Release, and opens a source PR into `main` if needed. Merge the PR with a merge commit to preserve the released SHA. Merge source `main` back into `dev` before the next release so the next release candidate includes the previous merge.

Version tags must retain their original digest. An existing version with a different digest is rejected. A retry after retagging but before the manifest push reuses the same image. Once a version was deployed and Prod moved on, promotion refuses to redeploy that old version. Git history provides this check, so the promotion checkout includes full history.

In each GHCR package's settings, grant the `manifest` repository **Write** under **Manage Actions access**. Promotion uses manifest's `GITHUB_TOKEN` with `packages: write`. Source finalization uses `packages: read` on its own package. Keep release tags on GHCR available for later finalization.

Publish the shared workflow revision before updating callers and migrating manifest paths. Update callers pinned to an older shared SHA, then move `apps/base` to `base` and `apps/tikto/overlays` to `overlays`. Reapply `bootstrap/dev.yaml` and `bootstrap/prod.yaml` to the cluster so the existing Argo CD Applications read the new paths. Remove obsolete `releases/` records after all callers use the new scripts. The new scripts support both layouts during migration.

The source repos need `GITOPS_TOKEN` with Contents write on manifest for Dev CD. Finalization uses `MANIFEST_READ_TOKEN` when configured and otherwise uses the existing `GITOPS_TOKEN` to read manifest. Enable PR creation by GitHub Actions in the source repos. Allow the manifest workflow's token to write directly to manifest `main` under its branch rules. Keep GHCR packages public for the existing anonymous pull configuration.

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

## Required settings for legacy ECS and ECR callers

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

## Legacy release and retry

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
