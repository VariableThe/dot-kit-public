# Native T3 connector provenance

The source dependency is the upstream T3 Code repository at tag `v0.1.8-nightly.20261104.4179`, commit `9e85538535c8553901351112ab02dd1dd62db5ee`. It is included as the `vendor/t3code` Git submodule with its upstream MIT license and third-party notices.

This tag matches the server version returned by the working T3 connector on 2026-11-06. Matching a reported version does not establish byte-for-byte deployed-binary provenance or prove the complete live routing chain. No upstream build has been reproduced here, and no service, credential, tunnel, or runtime configuration was changed.

For a first installation, follow the [first-install quickstart](../plugins/t3-native/QUICKSTART.md). Use the [setup guide](../plugins/t3-native/README.md), the [setup and preflight guide](../plugins/t3-native/SETUP_AND_PREFLIGHT.md), and the [connection choices comparison](../plugins/t3-native/CONNECTION_MODES.md) for the detailed wizard, consent, and preflight behavior. See the [source lock](../plugins/t3-native/source-lock.json). Initialize the source from the repository root with `git submodule update --init --recursive`.

The upstream server exposes `/mcp` with OAuth. Each user must configure and authorize their own connection. See the [upstream outside-agents guide](https://github.com/pingdotgg/t3code/blob/9e85538535c8553901351112ab02dd1dd62db5ee/docs/user/outside-agents.md).

The Python adapter under `plugins/ecosystem_connectivity` is included separately. It is experimental, offline-tested, and not represented as deployed. The repository's Python tests do not build or run upstream T3.
