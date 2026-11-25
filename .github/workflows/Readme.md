# List of workflows and actions
This folder contains workflows that are helpful for maintaining a smooth and secure development process. The workflows should be enabled for open-source projects.

Workflows:
1. `ci.yml` - Runs Python 3.10 and current-Python tests, CDI schema validation, setuptools and Meson package builds, static checks, and DCO sign-off checks. Pull requests use a pinned CDI release; the weekly scheduled run checks compatibility with the latest release.
2. `qcom-preflight-checks.yml` - Runs copyright, email, repolinter, dependency review, Semgrep, and other policy checks. See [qualcomm/qcom-reusable-workflows](https://github.com/qualcomm/qcom-reusable-workflows).
3. `stale-issues.yaml` - Runs weekly, marks unexempt issues and pull requests stale after 90 days of inactivity, and closes them after they have remained stale for at least 7 days.
4. `lava-schema-check.yml` - Reusable validation for rendered job templates under `ci/lava/`. `ci.yml` calls it for pull requests, and `lava-hardware.yml` calls it before job submission.
5. `lava-test.yml` - Reusable three-board matrix that validates its inputs, renders each LAVA job without modifying the checkout, and waits for the hardware result. Failed test cases and incomplete or canceled LAVA jobs fail the GitHub Actions job.
6. `resolve-qcom-image.yml` - Reusable resolver for the newest qualifying publication from a completed `qcom-deb-images` Build workflow run from the last three months, independently of upstream board-test results. It accepts scheduled and legacy `workflow_run` triggers on trusted `main`, verifies a live immutable `build_url` artifact and a successful default-profile/default-variant image build for the selected suite in the selected attempt, and logs rejected candidates before trying older fresh runs. Explicit run IDs fail validation rather than falling back.
7. `lava-hardware.yml` - Runs the schema gate and real-hardware matrix on pushes to `main`, daily at 07:15 UTC, or manually. It resolves a `qcom-deb-images` daily build no older than three months; manual dispatch may select a specific workflow run ID and Debian suite.
