# Agent instructions

This is a disposable-scenario repository with durable history. See README.md for commands, workflows and setup status.

- Publish distributions only to TestPyPI. The GitHub environment named `pypi` is intentionally unattended and must still use the TestPyPI endpoint.
- Agents may create and delete test branches/tags and create, review, approve and merge test PRs, subject to GitHub permissions. Do not delete `main`, force-push it or destroy repository history. GitHub does not let an author approve their own PR.
- Wait for human approval on changes requiring owner feedback, including this file, CODEOWNERS and changes to these authority/publication boundaries. Routine test PRs may be merged autonomously after their relevant checks pass.
- Finish each test batch with a new committed version and matching tag; do not rewrite completed batches. A failed publication may require deleting/recreating its tag after a workflow fix: first inspect TestPyPI and GitHub state, retain evidence, and never replace or reuse an uploaded distribution version. Nopt's current run-attempt/recovery restrictions still apply.
- Assert expected failures fail for the intended reason. Preserve logs and artifact links for both successful and expected-failure scenarios. Do not report an arbitrary workflow failure as a successful negative test.
- PRs merge branches. Tags identify release commits and trigger publication; they are not PR branches.
