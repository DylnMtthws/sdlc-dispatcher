# Publication audit — 2026-09-14

This is a focused engineering audit of the publication working tree and the four
existing commits reachable from `main`. It is not an independent penetration test
or a claim that every execution path is verified.

## Validation

| Check | Result |
| --- | --- |
| Offline unit/provider-contract suite | 191 discovered, 183 passed, 8 Docker tests skipped |
| Full suite with Docker enabled | 191 passed, no skips |
| Ruff and Black | Passed across core, tests, and Deck Lab Python adapters |
| Combined statement/branch coverage | 60% offline; 65% with Docker enabled |
| Source distribution and wheel | Built successfully |
| Fresh wheel installation | CLI help passed outside the source checkout, with no runtime dependencies |
| Installed Python dependency audit | `pip-audit`: no known vulnerabilities found |
| Credential scan | `detect-secrets`: working files and all four prior `main` snapshots reviewed; no actual credentials identified |
| Static security analysis | Bandit: 0 high, 15 medium, 90 low findings; medium findings reviewed below |

Local verification used Python 3.11 on macOS with Docker 29.5.2. Coverage measures
the core Python package; it does not measure Python executed inside subprocesses
or containers. It is a baseline, not a coverage gate. Host orchestration, provider
failures, and integration scripts still need broader fault-injection coverage.

CI independently runs the offline suite on Python 3.11–3.13, the Docker boundary
suite, and package build/install checks. Its current result is linked from the
README badge. Local results above do not imply a live coding-model, Linear,
GitHub-release, or production-deployment test.

## Findings addressed

- **Proxy startup race found in hosted CI:** Docker's detached-start response
  preceded the proxy listener becoming ready on a faster Linux runner. Workers
  now wait for a bounded in-container socket probe before launching. A regression
  test delays proxy startup by two seconds; it failed with connection refused
  before the fix. The hosted container job now also runs the reviewer tests.

- **Host-dependent storage test:** the storage monitor test discovered this
  developer host's active DigitalOcean provider. Its fixture now explicitly
  disables that adapter, so the test cannot query production and exercises the
  intended WAL recovery behavior on any host.
- **Hardcoded deployment bridge location:** the core adapter used an absolute
  developer home directory. It now defaults to the sibling infrastructure checkout
  and supports `DISPATCHER_INFRA_ROOT`. Missing activation disables the provider;
  malformed or unreadable activation fails with a safe operator error. New tests
  cover provider selection, corrupt activation, and refusal to deploy an
  unapproved prepared commit.
- **Failing style checks:** normalized imports and formatting in the pending
  release and storage work. Replaced dynamic JSON importing with a normal import.
- **Outdated project introduction:** distinguished generic approval-based dispatch
  from the optional human-authorized release controller, and marked the initial
  verification log as historical. Documented current optional screenshot behavior.
- **Developer and publication gaps:** added an architecture/code tour, contributor
  and security guides, repeatable Make targets, package metadata, Python CI matrix,
  coverage/build artifacts, wheel smoke test, issue/PR templates, dependency update
  configuration, and broader ignores for local state and credentials.

## Static-analysis triage

The 15 medium Bandit warnings fall into four reviewed categories:

| Warning | Count | Review |
| --- | --- | --- |
| Temporary paths (`B108`) | 9 | Paths are inside disposable container tmpfs or the explicitly guarded synthetic preview. They are not host shared temporary credential files. |
| All-interface listeners (`B104`) | 3 | Container-internal model proxies and the synthetic preview server need container network listeners. Host exposure is controlled by trusted Docker orchestration. |
| URL opening (`B310`) | 2 | The Cursor installer constructs a fixed HTTPS download URL and verifies a pinned SHA-256; the containment probe calls a fixed internal proxy URL. Neither takes a freeform report URL. |
| Dynamic SQL (`B608`) | 1 | `Store.finish` restricts column names to three literal allowed fields before building assignments; values remain bound parameters. |

Low-severity findings include subprocess execution and assertions in containment
probes. Those remain visible to future audits; no global scanner suppressions were
added. The checks do not establish safety if trusted host configuration, images,
or operator scripts are compromised.

Credential-scan candidates were the pinned Cursor archive hash, explicit fake
provider keys, synthetic preview credentials, and a private-key header fixture
used to test rejection. Local caches were excluded from publication. Only `main`
is published; local checkpoint and dispatcher runtime refs are not pushed.

## Remaining limits

The core is a single-host pilot. The Deck Lab registration is a deployment-specific
reference, and its storage/deployment adapters require separately installed private
operator infrastructure. New users should start with `examples/project.toml`.
Docker is not a multi-tenant security boundary. Owner-session deployment credentials,
external CLI/image maintenance, and incomplete coverage remain explicit engineering
tradeoffs; see [architecture](architecture.md) and [security](../SECURITY.md).

The repository is public without a license, at the author's request. No open-source
license grant is included.

## Reproduce

```bash
python3 -m venv .venv
make install
make check
DISPATCHER_DOCKER_TESTS=1 .venv/bin/python -m coverage run -m unittest discover -s tests -v
.venv/bin/python -m coverage report
make build
```

Build `sdlc-dispatcher-test:local` first with `make docker-test` if it is absent.
The Docker suite uses disposable workspaces under `.dispatcher/docker-tests/`.
