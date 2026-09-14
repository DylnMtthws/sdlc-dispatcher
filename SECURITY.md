# Security policy

This project executes untrusted generated code. The current `main` branch is the
maintained pilot; there is no long-term-support release or security certification.

## Report a vulnerability

Use [GitHub private vulnerability reporting](https://github.com/DylnMtthws/sdlc-dispatcher/security/advisories/new).
Include the affected commit, a synthetic reproduction, expected boundary, and
observed impact. Do not open a public issue with exploit details or credentials.
There is no guaranteed response-time SLA.

## Trust model

The host, Docker daemon, registered project configuration, development images,
and operator scripts are trusted. Coding output and reviewer output are untrusted.
Workers receive disposable source copies and only their selected model credential.
Verification runs without network access. Publisher and release credentials remain
outside agent containers. Review receipts are bound to candidate evidence; model
approval does not grant release authority.

Docker shares a kernel with its host: this is not a hostile multi-tenant sandbox.
Use a dedicated host and reviewed images. A compromised operator account or Docker
daemon is outside the containment boundary. Report sanitization reduces exposure
but is not a general data-loss-prevention system.

The optional Deck Lab release controller has merge/deployment authority only after
separate operator configuration and a verified human approval event. It must not
share those credentials with coding or review containers. See
[operations](docs/operations.md) and [release authorization](docs/linear-release-handoff.md).
