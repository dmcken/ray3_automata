# RACOM RAy Automations
[![Tests](https://github.com/dmcken/ray3_automata/actions/workflows/tests.yml/badge.svg)](https://github.com/dmcken/ray3_automata/actions/workflows/tests.yml)
[![Ruff](https://github.com/dmcken/ray3_automata/actions/workflows/ruff.yml/badge.svg)](https://github.com/dmcken/ray3_automata/actions/workflows/ruff.yml)

Automate RACOM RAy (RAy2/RAy3) microwave backhaul radios.

Unlike this author's other automation packages (`ubnt_automata`,
`tach_automata`), a RAy has no REST/JSON API - its only management
surface is its own live web GUI, a server-rendered "Wt" single-page
app. This package drives that same protocol directly (simulated DOM
events, polled for server-side results) rather than wrapping a
documented API - see `src/ray3_automata/device.py`'s module docstring
for exactly what's confirmed live versus inferred by analogy.

## Modules / Hardware Platforms

- RAy2, RAy3 - one `Ray3Device` class for both; nothing observed so far
  distinguishes them at the protocol level. Confirmed live against a
  real unit acting as a backhaul radio.

## Install

```sh
pip install "ray3_automata @ git+https://github.com/dmcken/ray3_automata.git@v0.1.0"
```

## Running tests

Pure unit tests (no live devices touched) - HTTP calls are mocked with
`requests-mock`.

```sh
uv sync --group dev  # or: pip install -e .[test]
uv run pytest        # or: pytest
```

## Examples

Login and run a CLI command:
```python
import ray3_automata

dev = ray3_automata.Ray3Device('10.0.0.1')
dev.login(['current-password', 'old-password'], username='admin')
print(dev.run_cli('cli_info_link'))
```

Pre-auth identification, e.g. before deciding which driver to try
against an unknown device:
```python
import ray3_automata

if ray3_automata.is_ray3_device('10.0.0.1'):
    ...
```

See `ray3_automata.KNOWN_CLI_COMMANDS` for the device's own full CLI
catalogue (from a live `cli_help` call) - `run_cli()` can be pointed at
any of them, though only `cli_help`/`cli_info_link` have a confirmed
live response shape so far.
