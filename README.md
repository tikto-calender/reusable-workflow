# Flavoriy reusable GitHub Actions workflows

Shared CI/CD building blocks for the Flavoriy frontend, backend, and notification
service repositories. Caller repositories own their triggers and branch filters;
this repository owns the repeatable implementation.

## Workflows

| Workflow | Intended callers | Purpose |
| --- | --- | --- |
| `node-ci.yml` | frontend, backend | Install, format, lint, test, build/check, and optionally scan with SonarQube |
| `python-ci.yml` | notification-service | Install, format, lint, test, compile, and optionally scan with SonarQube |
| `ecr-publish.yml` | backend, notification-service | Build a container, fail on high/critical vulnerabilities, assume AWS credentials with OIDC, and push an immutable ECR image |
| `s3-cloudfront-deploy.yml` | frontend | Build and scan static assets, upload them to S3 with cache headers, and invalidate CloudFront |
| `dependabot-auto-merge.yml` | all repositories | Verify Dependabot metadata, approve eligible patch/minor updates, and enable auto-merge |

Reusable workflows must stay directly under `.github/workflows`; GitHub does not
load reusable workflows from subdirectories.

## Versioning

Create immutable releases such as `v1.0.0`, then maintain a moving major tag such
as `v1` for convenient upgrades. Callers below use `@v1`. For the strongest supply
chain protection, replace `v1` with the full commit SHA of the release.

Do not point production deployment workflows at `@main`.

## Required repository configuration

The three caller repositories need these values where applicable:

| Name | Kind | Used by |
| --- | --- | --- |
| `SONAR_TOKEN` | Repository or environment secret | Optional SonarQube scan |
| `SONAR_HOST_URL` | Repository variable | Optional SonarQube scan |
| `AWS_BUILD_ROLE_ARN` | Repository/environment variable | Backend and notification ECR publish |
| `AWS_DEPLOY_ROLE_ARN` | Repository/environment variable | Frontend S3 deploy |
| `AWS_REGION` | Repository/environment variable | AWS workflows; examples fall back to `ap-southeast-2` |
| `FRONTEND_BUCKET_NAME` | Environment variable | Frontend S3 deploy |
| `CLOUDFRONT_DISTRIBUTION_ID` | Environment variable | Frontend cache invalidation |

The AWS roles must trust the caller repositories' GitHub OIDC subjects. No static
AWS access keys are required. The caller must grant `id-token: write` to jobs that
invoke an AWS workflow.

If this repository is private, configure **Settings → Actions → General → Access**
so the caller repositories can use its workflows. Cross-organization callers need
this repository to be public or an access arrangement supported by GitHub.

## Caller: backend CI

Save as `.github/workflows/ci.yml` in the backend repository:

```yaml
name: CI

on:
  pull_request:
    branches: [dev, main]
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: ci-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  quality:
    uses: tikto-calender/reusable-workflow/.github/workflows/node-ci.yml@v1
    with:
      install-command: npm ci --ignore-scripts
      build-command: npm run syntax
```

## Caller: backend CD

Save as `.github/workflows/cd.yml` in the backend repository:

```yaml
name: Build, scan and push image

on:
  push:
    branches: [dev, main]
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: cd-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: false

jobs:
  quality:
    uses: tikto-calender/reusable-workflow/.github/workflows/node-ci.yml@v1
    with:
      install-command: npm ci --ignore-scripts
      build-command: npm run syntax

  publish:
    needs: quality
    permissions:
      contents: read
      id-token: write
    uses: tikto-calender/reusable-workflow/.github/workflows/ecr-publish.yml@v1
    with:
      aws-region: ${{ vars.AWS_REGION || 'ap-southeast-2' }}
      aws-role-arn: ${{ vars.AWS_BUILD_ROLE_ARN }}
      ecr-repository: tikto/backend
      service-name: backend
```

## Caller: notification-service CI

Save as `.github/workflows/ci.yml` in the notification-service repository:

```yaml
name: CI

on:
  pull_request:
    branches: [dev, main]
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: ci-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  quality:
    uses: tikto-calender/reusable-workflow/.github/workflows/python-ci.yml@v1
    with:
      run-sonarqube: true
      sonar-host-url: ${{ vars.SONAR_HOST_URL }}
    secrets:
      SONAR_TOKEN: ${{ secrets.SONAR_TOKEN }}
```

## Caller: notification-service CD

Save as `.github/workflows/cd.yml` in the notification-service repository:

```yaml
name: Build, scan and push image

on:
  push:
    branches: [dev, main]
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: cd-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: false

jobs:
  quality:
    uses: tikto-calender/reusable-workflow/.github/workflows/python-ci.yml@v1
    with:
      run-sonarqube: true
      sonar-host-url: ${{ vars.SONAR_HOST_URL }}
    secrets:
      SONAR_TOKEN: ${{ secrets.SONAR_TOKEN }}

  publish:
    needs: quality
    permissions:
      contents: read
      id-token: write
    uses: tikto-calender/reusable-workflow/.github/workflows/ecr-publish.yml@v1
    with:
      aws-region: ${{ vars.AWS_REGION || 'ap-southeast-2' }}
      aws-role-arn: ${{ vars.AWS_BUILD_ROLE_ARN }}
      ecr-repository: tikto/notification
      service-name: notification
```

## Caller: frontend CI

Save as `.github/workflows/ci.yml` in the frontend repository:

```yaml
name: CI

on:
  pull_request:
    branches: [dev, main]
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: ci-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  quality:
    uses: tikto-calender/reusable-workflow/.github/workflows/node-ci.yml@v1
    with:
      build-command: npm run build
      run-sonarqube: true
      sonar-host-url: ${{ vars.SONAR_HOST_URL }}
    secrets:
      SONAR_TOKEN: ${{ secrets.SONAR_TOKEN }}
```

## Caller: frontend CD

Save as `.github/workflows/cd.yml` in the frontend repository:

```yaml
name: Build and deploy frontend

on:
  push:
    branches: [dev, main]
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: cd-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: false

jobs:
  quality:
    uses: tikto-calender/reusable-workflow/.github/workflows/node-ci.yml@v1
    with:
      # The deployment job performs the production build after this quality gate.
      build-command: ""
      run-sonarqube: true
      sonar-host-url: ${{ vars.SONAR_HOST_URL }}
    secrets:
      SONAR_TOKEN: ${{ secrets.SONAR_TOKEN }}

  deploy:
    needs: quality
    permissions:
      contents: read
      id-token: write
    uses: tikto-calender/reusable-workflow/.github/workflows/s3-cloudfront-deploy.yml@v1
    with:
      aws-region: ${{ vars.AWS_REGION || 'ap-southeast-2' }}
      environment-name: ${{ github.ref_name == 'main' && 'prod' || 'dev' }}
```

## Dependabot automatic PRs and auto-merge

Dependabot configuration is repository-specific, so it cannot be inherited from a
reusable workflow. Copy one of these templates into each caller repository as
`.github/dependabot.yml`:

- [`examples/dependabot-node.yml`](examples/dependabot-node.yml) for frontend and backend.
- [`examples/dependabot-python.yml`](examples/dependabot-python.yml) for notification-service.

Then add this caller as `.github/workflows/dependabot-auto-merge.yml` in each
repository:

```yaml
name: Dependabot auto-merge

on:
  pull_request_target:
    types: [opened, synchronize, reopened, ready_for_review]

permissions:
  contents: write
  pull-requests: write

jobs:
  dependabot:
    if: github.event.pull_request.user.login == 'dependabot[bot]'
    uses: tikto-calender/reusable-workflow/.github/workflows/dependabot-auto-merge.yml@v1
    with:
      target-branch: dev
      allow-patch: true
      allow-minor: true
      allow-major: false
      auto-approve: true
      merge-method: squash
```

This workflow never checks out or executes code from the pull request. It verifies
Dependabot metadata and refuses PRs containing maintainer changes. The supplied
Dependabot templates target `dev`, and the reusable workflow also checks that the
PR base branch is exactly `dev`. Patch and minor updates can therefore auto-merge
only into `dev`; `main`/production and major updates always require manual review.

Before enabling it, configure the caller repository:

1. Enable **Settings → General → Pull Requests → Allow auto-merge**.
2. Enable **Settings → Actions → General → Workflow permissions → Allow GitHub
   Actions to create and approve pull requests** if automatic approval is desired.
3. Protect `dev` and `main` with required CI status checks. Auto-merge into `dev`
   will wait for those checks instead of bypassing them. Promotion to `main`
   remains manual.

## Notes

- A called workflow checks out the caller repository, not this repository.
- Workflow-level `env` values from a caller are not propagated to a called workflow;
  pass configuration through `with`, `secrets`, or repository/environment variables.
- Secrets only flow to directly called workflows. These examples pass only the
  SonarQube secret instead of using `secrets: inherit`.
- Branch and path triggers belong in caller workflows because reusable workflows
  are invoked through `workflow_call`.
