# dot-kit-public

Sanitized collection of connectivity plugins and workflow skills for dot.

## Components
- `plugins/ecosystem_connectivity/`: Local connectivity primitives for T3 and Beeper.
- `skills/`: Workflow instructions for orchestrating T3 and Beeper.
- `tests/`: Offline validation tests for the connectivity plugins.

## Test Instructions
Run tests from the root directory:
```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest plugins/ecosystem_connectivity/tests/
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest plugins/ecosystem_connectivity/t3mcp_adapter/tests/
```

## Dependencies
- Python 3.10+
- `pytest`
- `requests`
- `cryptography`

*Note: Tests may require separate invocations due to duplicate module names.*
