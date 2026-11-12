# Shared CI/CD workflows

This repository provides the GitHub Actions jobs used by TikTo's application and Manifest repositories. Each caller defines when its workflow runs and supplies the inputs. The shared jobs run checks, publish images, update image digests in Manifest and finalize releases.

## Use a shared workflow

Add a job to a workflow in the calling repository. For example, a Node application can call the shared CI job on pull requests into `dev`:

```yaml
name: CI
on:
  pull_request:
    branches: [dev]
permissions:
  contents: read
jobs:
  quality:
    uses: tikto-calender/reusable-workflow/.github/workflows/ci-node.yml@main
    with:
      build-command: npm run build
```

This example expects a committed `package-lock.json` and npm scripts for formatting checks, linting, tests and build. For other commands and required secrets, use the `workflow_call` inputs declared in the relevant workflow file below. Check the run in the **Actions** tab of the calling repository.

## Test the release scripts locally

From this repository's root, use Python 3.11 or later and Git:

```powershell
python -B -m unittest discover -s scripts -p 'test_*.py' -v
```

The tests create temporary Git repositories and mock registry access. They cover digest pinning, retries, concurrent updates and release finalization.

## Available workflows

| Workflow | Purpose |
| --- | --- |
| `ci-node.yml` | Node.js format, lint, test, and build or syntax checks |
| `ci-python.yml` | Python format, lint, test, and compile checks |
| `ci-sonarqube.yml` | SonarQube analysis using CI coverage |
| `push-ghcr.yml` | Build and scan an image, then publish to GHCR and return its digest |
| `cd-dev.yml` | Deploy the Dev image by digest |
| `cd-prod.yml` | Promote a Dev image digest by pinning it in the Prod Helm values |
| `finalize-release.yml` | Tag the verified source commit, create a GitHub Release, and open a source PR |

## Shared EKS GitOps

The promotion buttons are in the Manifest repository's Actions tab. Frontend, Backend and Notification all call [`cd-prod.yml`](.github/workflows/cd-prod.yml). [Manifest workflow files](https://github.com/tikto-calender/manifest/tree/main/.github/workflows)

1. The application repositories define their own CI branch and path filters and call the shared Node or Python workflow. Feature PRs are merged manually. Remove legacy feature auto-merge workflows from the default branch as well as Dev.
2. When a push to `dev` triggers **Deploy Dev**, the workflow builds, scans and publishes an image to GHCR with a `sha-<commit>` tag and OCI source/revision labels. CD updates the per-service image values in `charts/tikto/environments/dev/` on manifest `main`.
   If an older bot merge suppressed the push workflow, run **Deploy Dev** manually from `dev`. The caller must expose `workflow_dispatch` on the default branch for GitHub to show the Run workflow button.
3. After verifying Dev, run **Promote Frontend**, **Promote Backend**, or **Promote Notification** from manifest `main` with a version. **Promote All Services** accepts a separate version for each service. Promotion retags the exact Dev digest as `vMAJOR.MINOR.PATCH` on GHCR and pins it in the Prod Helm values. Argo CD syncs the updated Deployment. [Manifest promotion guide](https://github.com/tikto-calender/manifest#promote-to-production)
4. Check that the Prod Argo CD app is synced and healthy, confirm it runs the selected digest, and verify the application. Then run **Finalize release** from source `main` with the same version. It verifies the GHCR version tag against the pinned Prod image and reads the source SHA from its OCI labels. It tags that SHA, creates its GitHub Release, and opens a source PR into `main` if needed. Merge the PR with a merge commit to preserve the released SHA. Merge source `main` back into `dev` before the next release so the next release candidate includes the previous merge.

Version tags must retain their original digest. An existing version with a different digest is rejected. A retry after retagging but before the manifest push reuses the same image. Once a version was deployed and Prod moved on, promotion refuses to redeploy that old version. Git history provides this check, so the promotion checkout includes full history.

In each GHCR package's settings, grant the `manifest` repository **Write** under **Manage Actions access**. Promotion uses manifest's `GITHUB_TOKEN` with `packages: write`. Source finalization uses `packages: read` on its own package. Keep release tags on GHCR available for later finalization.

The release scripts support the Helm values and earlier Kustomize layouts during migration.

The source repos need `GITOPS_TOKEN` with Contents write on manifest for Dev CD and `MANIFEST_READ_TOKEN` with Contents read for finalization. Source callers that configure a fallback can use `GITOPS_TOKEN` to read manifest. Enable PR creation by GitHub Actions in the source repos. Allow the manifest workflow's token to write directly to manifest `main` under its branch rules. Keep GHCR packages public for the existing anonymous pull configuration.

## Callers and publication

Publish the Manifest migration before retiring the previous Backend workflow. [Backend migration steps](https://github.com/tikto-calender/manifest#migrate-an-existing-backend-deployment)

The current Manifest callers reference `@main`. When pinning a caller to a fixed revision, use a published commit SHA or a protected version tag.

Node and Python CI skip empty commands. CI uses the PR merge result, and Dev CD checks out the pushed commit. Finalize release resolves the tested source commit from the Prod digest.
