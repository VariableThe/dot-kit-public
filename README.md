# dot-kit-public

Sanitized workflow skills and T3/Beeper integration material.

## Start here

Installing the T3 connector for the first time? Follow the [first-install quickstart](plugins/t3-native/QUICKSTART.md). It gives the shortest hosted-assistant path, the older-server fallback, and a table for the usual failures. If your connection already works, keep it. To separate transport, URL discovery, and consent-page decisions, read [connection choices compared](plugins/t3-native/CONNECTION_MODES.md).

For the read-only checks behind that page, see the [setup and preflight guide](plugins/t3-native/SETUP_AND_PREFLIGHT.md).

## Native T3 connector source

The native T3 connector is an upstream T3 Code feature. This repository pins its complete upstream source as the `vendor/t3code` submodule at `9e85538535c8553901351112ab02dd1dd62db5ee`, tag `v0.1.8-nightly.20261104.4179`.

That tag matches the server version observed through the working connector on 2026-11-06. It does not establish byte-for-byte deployed-binary provenance or verify the complete live routing chain. The upstream build has not been reproduced here.

See the [first-install quickstart](plugins/t3-native/QUICKSTART.md), the [native connector setup guide](plugins/t3-native/README.md), the [source lock](plugins/t3-native/source-lock.json), and [validation limits](docs/native-t3-connector.md).

Initialize the pinned source from the repository root:

```sh
git submodule update --init --recursive
```

This downloads source only. Follow the upstream build documentation and obtain your own authorized T3 access before use.

A pure, offline helper can check a copied MCP URL and the setup path you observed without contacting any server:

```sh
node plugins/t3-native/setup-readiness.mjs '<mcp-url>' --agent hosted \
  --discovery wizard --approval hosted
```

Its result never echoes the URL and does not prove reachability, consent, or write permission. A normal first install is confirmed by a simple read in the client; the preflight is optional advanced verification through an existing connector callback.

## Included

- [Start T3 work](skills/start-t3-thread/SKILL.md): discover and coordinate explicitly authorized tasks.
- [Use Beeper locally](skills/use-beeper-local/SKILL.md): verify an existing route and intended recipient.
- `plugins/ecosystem_connectivity/`: separate experimental Python adapter and read-only Beeper facade with synthetic offline tests.

The Python adapter is not represented as the deployed native connector. The Beeper facade cannot send messages. This repository includes no credentials, private host configuration, standalone controller CLI, or hosted service. Installing or cloning it grants no data access or action permission.

## Offline tests

The Python tests require Python 3.10 or newer, pytest, requests, and cryptography. From the repository root, run the suites separately because some modules share names:

```sh
export PYTHONPATH=".:plugins/ecosystem_connectivity/t3mcp_adapter:plugins/ecosystem_connectivity/t3mcp_adapter/tests"
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest plugins/ecosystem_connectivity/tests -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest plugins/ecosystem_connectivity/t3mcp_adapter/tests -p no:cacheprovider
```

These tests do not contact live T3 or Beeper services, build upstream T3, or establish production readiness.

## Licensing

The pinned upstream T3 source has its own MIT license and third-party notices, preserved in the submodule. No license has been selected for this repository's original content; publication alone is not a general license grant.

## T3 completion monitor

The native connector integration includes a [caller-side completion monitor](plugins/t3-native/COMPLETION_MONITOR.md). It keeps event-wait and result-read handling together while leaving task ownership, durable storage, permissions and user delivery with the caller.
