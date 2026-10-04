'''Top level module of this package'''
# Classes accessible from import

# Exceptions
from . import exceptions

# Classes
from .device import (
    KNOWN_CLI_COMMANDS,
    TITLE_MARKER,
    Ray3Device,
    is_ray3_device,
)

# Versions should comply with PEP 440:
# https://www.python.org/dev/peps/pep-0440/
__version__ = "0.1.0"

__all__ = [
    'KNOWN_CLI_COMMANDS',
    'TITLE_MARKER',
    'Ray3Device',
    'exceptions',
    'is_ray3_device',
]
