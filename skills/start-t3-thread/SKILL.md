# Control T3 Code Threads

Coordinate T3 Code work through an authorized MCP controller.

## Prerequisites
- A configured T3 MCP controller endpoint.
- A bearer token issued with `orchestration:read` and `orchestration:operate` scopes.

## Operations
Use the T3 controller CLI to manage threads:
- `discover`: List available projects and server info.
- `start`: Launch a new thread for a project with a specific task.
- `send`: Update or steer an existing thread.
- `wait`: Block until a run reaches a terminal state.
- `interrupt`: Stop a running turn.

## Authorization
All mutations require `orchestration:operate`. Use a least-privilege token and export it as `T3_TOKEN_<HOST>` for the target host.

## Limits
- Threads are host-scoped; identifiers are not portable across instances.
- No auto-approval of runtime modes is permitted without explicit user opt-in.
