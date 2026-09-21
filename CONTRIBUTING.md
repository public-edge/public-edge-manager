# Contributing

Contributions are welcome through GitHub pull requests. Keep product defaults
free of organization-specific domains, addresses, credentials, and topology.

Before opening a pull request, run:

```sh
python -m unittest discover -s tests -v
helm lint .
helm lint . -f examples/values-example.yaml
helm template public-edge-manager . -f examples/values-example.yaml >/dev/null
docker build .
```

Maintainer-authored pull requests from this repository are eligible for an
automated `re8ch-policy-reviewer` check after all commits have verified
signatures and CI checks succeed. Its informational approval does not count
toward GitHub's human-review requirement; the App-owned check is the merge gate.
The reviewer does not merge pull requests or pass forked and external-author
contributions; a maintainer must bring those changes into a trusted, signed PR.

By contributing, you agree that your contribution is licensed under Apache-2.0.
