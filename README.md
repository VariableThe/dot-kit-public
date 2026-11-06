# dot-kit-public

Generic workflow skills and prototype connectivity code for T3 Code and Beeper.

## Included

- `skills/start-t3-thread/`: discover and coordinate explicitly authorized T3 tasks.
- `skills/use-beeper-local/`: verify an existing local Beeper route and the intended recipient.
- `plugins/ecosystem_connectivity/`: Python T3 adapter and read-only Beeper facade prototypes.
- Offline tests with synthetic fixtures.

The skills require your own existing tools and authenticated services. This repository does not include credentials, private host configuration, a standalone T3 controller CLI, a deployed authentication service, or a working hosted integration. The Beeper facade cannot send messages.

## Status

Prototype code, validated with offline tests only. Passing tests do not establish live compatibility, production readiness, or safe deployment in a particular environment. Review the implementation and configure access boundaries before use. Installation does not grant permission to read data or take actions.

No open-source license has been selected. Publication alone is not a general license grant.

## Dependencies

Python 3.10 or newer, `pytest`, `requests`, and `cryptography`.

## Tests

From the repository root, run the suites separately because some modules share names:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/ -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest plugins/ecosystem_connectivity/tests/ -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest plugins/ecosystem_connectivity/t3mcp_adapter/tests/ -p no:cacheprovider
```

The reviewed export passed 301 tests plus 106 subtests. No live T3 or Beeper operation is part of those tests.
