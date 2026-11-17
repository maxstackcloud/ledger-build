# Spring Boot feature build pipeline

The implementation is `.github/workflows/ledgerbuild.yml`. It builds and tests the application, gates Java dependencies with Trivy, submits Java analysis and coverage to SonarCloud, waits for its quality gate, then publishes an image and gates ECR scan findings. Any failed command stops its job. There are no ignored security failures.

## Review of the original repository

| Finding | Change or remaining action |
| --- | --- |
| Workflow ran on main pushes and pull requests | Replaced with `feature/**` pushes and manual dispatch restricted to feature branches |
| Dispatch inputs were absent for automatic events | Automatic events use repository variables; manual inputs override them |
| Tests skipped during packaging, then build repeated | One `mvn clean verify` builds, tests, packages, and creates coverage |
| Obsolete OWASP Dependency Check ran during Maven verify | Removed; requested SCA uses Trivy instead |
| Trivy and Sonar errors ignored | Both now block publishing |
| Sonar only ran on main | Runs on the selected feature branch |
| Old Docker API version forced for Testcontainers | Removed; validate against the actual hosted Docker engine |
| Dockerfile selected a version-specific JAR and old Java runtime | Stable `target/app.jar` and a Java 17 runtime base pulled on each build |
| Startup required `/opt/wezva/application.properties.orig`, absent from image | Java starts directly; Spring configuration can be provided by environment or an external config mount |
| Image tag used `rel` | Uses `fb` plus `github.run_number`, for example `fb42` |
| ECR polling could time out and still proceed | Explicit ten-minute timeout fails the job |
| ECR API errors became zero findings | Authentication, API errors, invalid responses and failed scan statuses fail the job |
| Scan used a mutable image tag | Resolves the pushed digest and scans that exact digest |
| CD dispatch targeted a separate hardcoded repository | Removed from this build-only pipeline |

Code inspection also found production concerns outside this pipeline change: Spring Boot is still 3.1.2; review and test an upgrade to a supported release. The packaged configuration contains a sample database password, exposes all Actuator endpoints, and uses `spring.jpa.hibernate.ddl-auto=create-drop`. Do not use those settings in production. Override database configuration through deployment secrets, restrict management endpoints, and use controlled database migrations. The integration test has six test methods and starts a MySQL container; it needs working Docker, not an independently installed MySQL server. Its container variable is misleadingly named `postgres`, but actually uses MySQL.

## One-time GitHub configuration

Under repository **Settings → Secrets and variables → Actions**, add these values:

| Kind | Name | Meaning / example |
| --- | --- | --- |
| Variable | `ECRURL` | `123456789012.dkr.ecr.ap-south-1.amazonaws.com`; hostname only |
| Variable | `APPREPO` | Existing repository, e.g. `ledger` |
| Variable | `REGION` | `ap-south-1` |
| Variable | `AWS_ACCOUNT_ID` | Registry account ID, e.g. `123456789012` |
| Variable | `AWS_ROLE_ARN` | IAM role assumed by GitHub OIDC |
| Variable | `SONAR_PROJECT_KEY` | Project key from SonarCloud |
| Variable | `SONAR_ORGANIZATION` | Organization key from SonarCloud |
| Secret | `SONAR_TOKEN` | Token authorized to analyze this project |

The former `sonarcloud` secret was renamed to `SONAR_TOKEN` in the workflow; create the new secret or adjust that reference. No permanent AWS access keys are needed. This implementation supports commercial AWS regions and a registry in the role's account; other partitions and cross-account publishing need deliberate adjustments.

Create the ECR repository beforehand. Set its registry scanning type to **BASIC**. The pipeline explicitly requests a scan, so scan-on-push is optional. It reads scanning configuration but never changes it. Enhanced scanning uses Amazon Inspector and different completion semantics; this workflow explicitly rejects it rather than reporting incomplete enhanced scans as safe.

Import/create the SonarCloud project, configure the desired quality gate, and enable CI-based analysis instead of automatic analysis. Ensure the subscription/project supports feature-branch analysis. The workflow does not weaken a gate to make an old codebase pass. A quality gate controls the conditions configured in SonarCloud; coverage or security thresholds must be configured there.

## AWS OIDC and least-privilege permissions

Create the GitHub OIDC provider `https://token.actions.githubusercontent.com` with audience `sts.amazonaws.com`. Use a role trust policy scoped to your repository's feature branches (replace placeholders):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Federated": "arn:aws:iam::ACCOUNT_ID:oidc-provider/token.actions.githubusercontent.com"},
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com"},
      "StringLike": {"token.actions.githubusercontent.com:sub": "repo:OWNER/REPOSITORY:ref:refs/heads/feature/*"}
    }
  }]
}
```

Attach a permissions policy scoped to the destination repository:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["ecr:GetAuthorizationToken", "ecr:GetRegistryScanningConfiguration"],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "ecr:DescribeRepositories", "ecr:DescribeImages",
        "ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage",
        "ecr:StartImageScan", "ecr:DescribeImageScanFindings"
      ],
      "Resource": "arn:aws:ecr:REGION:ACCOUNT_ID:repository/REPOSITORY"
    }
  ]
}
```

Only trusted contributors should have feature-branch push access: branch code, tests and workflow changes execute in CI. The publish job has OIDC permission; the build job does not. Restrict who can change workflows and the IAM trust policy as appropriate to your team.

## Triggers and parameters

A push to `feature/add-ledger-api` starts a run automatically using `ECRURL`, `APPREPO`, and `REGION` repository variables. The pattern includes nested feature branch names. Branches named `feature-xyz` do not match; adjust the filter and IAM trust policy if that is your convention.

GitHub cannot prompt for inputs on a push event. To supply values interactively, open **Actions → Ledger feature build and security gates → Run workflow**, select a `feature/...` branch and fill the three fields. The workflow must be present on the default branch for manual dispatch to be available. Selecting a non-feature branch skips the build and publish jobs.

Input values are passed through environment variables and quoted shell expansions, rather than interpolated into shell commands. Validation rejects missing values, malformed hostnames, account/region mismatches, and malformed repository names before building.

## Stages and commands

1. **Checkout:** `fetch-depth: 0` retrieves complete Git history for Sonar blame and new-code analysis.
2. **Runner and Java:** `ubuntu-24.04` selects a GitHub-hosted Ubuntu runner. `setup-java` selects Temurin Java 17 and caches downloaded Maven dependencies. Maven, Docker, AWS CLI and jq are provided by the hosted image and checked explicitly. Trivy runs in its versioned official container, avoiding a host installation script. `docker pull` downloads it; `docker run --rm` executes it and removes the temporary container.
3. **Build, tests and JaCoCo:** `mvn --batch-mode --no-transfer-progress clean verify`. `--batch-mode` disables interactive Maven output; `--no-transfer-progress` reduces download noise. `clean` removes prior build output; `verify` executes the lifecycle through compilation, tests, packaging and verification. JaCoCo's `prepare-agent` instruments the test JVM and its `report` goal runs in `verify`, creating HTML and XML reports. The workflow checks that both coverage XML and the executable JAR exist. Nothing skips tests.
4. **Trivy SCA:** scans the built Spring Boot JAR, including its embedded dependencies. `fs` selects filesystem scanning; `-v` mounts the application read-only and the output folder writable. `--scanners vuln` selects vulnerability scanning; `--severity HIGH,CRITICAL` selects blocking severities. `--format json` and `--output` retain machine-readable evidence; `--exit-code 1` makes matching findings fail the step. Unfixed vulnerabilities are included. Scanner/database download errors also fail the step. This is dependency analysis, not Java source SAST.
5. **SonarCloud SAST:** `mvn sonar:sonar` invokes the versioned Maven scanner declared in the POM. `-D` passes Maven/system properties: project, organization, server URL, branch and JaCoCo report location. The secret is read from `SONAR_TOKEN`. `sonar.qualitygate.wait=true` waits for server-side processing and fails if the gate fails; `sonar.qualitygate.timeout=600` bounds the wait. A failed scan or gate prevents publishing.
6. **Reports:** `if: always()` uploads available JaCoCo, Surefire and Trivy evidence even after an earlier failure. A failed earlier step still prevents container build/publish.
7. **Docker build:** `docker build --pull` checks for an updated Java runtime base; `--label` records the source commit; `-t` assigns `REGISTRY/REPOSITORY:fbN`; `.` uses the current build context. `.dockerignore` restricts that context to the application JAR and Dockerfile. The image runs under an unprivileged user. `docker save -o` writes the built image to a short-lived GitHub artifact for the publish job.
8. **ECR authentication and push:** the separate publish job downloads the image, obtains temporary AWS credentials through OIDC, verifies BASIC scanning, logs Docker into ECR, and loads/pushes the image. `--repository-names` selects the existing repository. `--query` is an AWS CLI JMESPath projection; `--output text` extracts a scalar. `describe-images --image-ids imageTag=fbN` resolves the image digest after the push.
9. **ECR gate:** `python3 scripts/ecr_scan_gate.py` calls `start-image-scan` and polls `describe-image-scan-findings` by `imageDigest`, using `--output json` and `--no-cli-pager` for automation. A daily scan-limit error is tolerated only because an earlier scan of that same digest may exist; the gate still requires a successful COMPLETE response. Scan-not-found is retried. Other API errors fail immediately; incomplete scans fail after ten minutes. Only a valid completed response can use zero for an omitted severity. HIGH or CRITICAL greater than zero fails the job. The JSON response is uploaded even when the vulnerability gate fails.

## Expected behavior and limits

The image tag is `fb${{ github.run_number }}`, e.g. `fb42`; it contains no angle brackets. A rerun retains that run number. With immutable ECR tags, a rerun that pushes an existing tag can fail; start a new run for a new tag. The digest, not the tag, identifies the scanned image.

ECR BASIC scans operating-system packages; Trivy covers the packaged Java dependencies. They have different scopes and vulnerability databases. Neither implies that all application security issues are absent. SonarCloud findings are available in SonarCloud; Trivy and ECR reports are retained as GitHub artifacts.

The ECR gate runs **after push**, as requested: a rejected image remains in ECR and must not be deployed. This build workflow contains no deployment job. A deployment workflow should require this entire workflow to succeed and deploy its approved digest. Do not deploy simply because a tag was pushed.

The runtime Java image is a moving Java 17 tag pulled on each run, so its bytes may change between builds. For controlled production builds, approve and pin dependency/action/container digests and automate their updates. The sample application dependencies remain unchanged; security gates may correctly fail until vulnerabilities are remediated.

## Validation status

Local verification passed workflow structure, POM XML, every shell block syntax check and eight automated ECR gate tests covering vulnerabilities, invalid responses, API denial, scan failures, scan-on-push races, and timeout. The tests are in `tests/test_ecr_scan_gate.py`. A real Maven/Testcontainers build cannot be executed in this workspace: Java and Maven are absent and access to the Docker socket is denied. AWS and SonarCloud execution require the configuration above. A successful hosted run is required before claiming end-to-end validation.

## Reference documentation

- [GitHub workflow triggers and manual inputs](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)
- [AWS credentials action and OIDC](https://github.com/aws-actions/configure-aws-credentials)
- [ECR BASIC scanning](https://docs.aws.amazon.com/AmazonECR/latest/userguide/image-scanning-basic.html)
- [ECR scan findings](https://docs.aws.amazon.com/cli/latest/reference/ecr/describe-image-scan-findings.html)
- [Trivy Java dependency scanning](https://trivy.dev/docs/dev/coverage/language/java/)
- [SonarCloud analysis parameters](https://docs.sonarsource.com/sonarqube-cloud/analyzing-source-code/analysis-parameters/parameters-not-settable-in-ui)
- [SonarCloud subscription capabilities](https://docs.sonarsource.com/sonarqube-cloud/administering-sonarcloud/managing-subscription/subscription-plans)
