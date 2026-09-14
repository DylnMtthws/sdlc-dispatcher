# Contributing

Start with the [architecture walkthrough](docs/architecture.md) and
[operating boundaries](docs/operations.md). The project is a single-host pilot;
small, independently verifiable changes are easiest to review.

## Local setup

Python 3.11+ and Git are enough for the unit suite. Docker is required for the
container tests and demo. No provider credentials are needed for either suite.

```bash
git clone https://github.com/DylnMtthws/sdlc-dispatcher.git
cd sdlc-dispatcher
python3 -m venv .venv
make install
make check
make docker-test
```

`make help` lists the remaining commands. `make format` applies import ordering
and Black. `make test` measures branch coverage; inspect missing paths with
`.venv/bin/python -m coverage html` and open `htmlcov/index.html` locally.
CI tests Python 3.11–3.13, real Docker boundaries, and a built wheel installed
outside the source checkout. Coverage is downloadable from each CI run.

## Change boundaries

- Keep project-specific integrations outside the generic queue/worker path.
- Treat generated files, reports, provider responses, and review output as
  untrusted. Policy and release authority must stay controller-owned.
- Mock provider writes in tests. Use temporary stores and explicit provider
  fixtures; tests must not discover a developer's production configuration.
- Add a regression test for correctness fixes, especially approval withdrawal,
  stale evidence, lost responses, retries, or filesystem/network boundaries.
- Update operating instructions when configuration or recovery changes. Include
  migration and rollback considerations for durable-state changes.

Open a focused pull request explaining the problem, behavior, and validation.
Do not commit `.dispatcher/`, `.private/`, credentials, production data, or raw
user reports. Use synthetic reproductions. Report security issues through
[the private reporting process](SECURITY.md).
