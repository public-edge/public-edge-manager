# Security policy

Supported releases are the latest published minor release. Security fixes may
also be backported when maintainers identify a broadly deployed older release;
such a backport is announced in its GitHub Release notes.

Please report vulnerabilities privately through
[GitHub Security Advisories](https://github.com/public-edge/public-edge-manager/security/advisories/new).
Do not open a public issue containing exploit details, secrets, or production
topology. Maintainers will acknowledge a report within seven days and will use
the private advisory to coordinate validation, a CVE when applicable, a fixed
release and disclosure.

Only tagged container images and Helm charts published by the repository release
workflow are official artifacts. Consumers should verify GitHub artifact
attestations and deploy immutable image digests.

Automated governance includes Dependabot for Actions, Python and container
dependencies, dependency review on pull requests, CodeQL analysis, and a
published OpenSSF Scorecard. Findings are triaged in GitHub Security rather
than copied into runtime telemetry or public issues before disclosure.
