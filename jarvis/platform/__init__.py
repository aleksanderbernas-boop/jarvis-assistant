"""Select the platform adapter for this OS (or the simulated one in mock mode)."""

from __future__ import annotations

import sys

from jarvis.platform.base import PlatformAdapter


def get_platform(mock: bool) -> PlatformAdapter:
    if mock:
        from jarvis.platform.mock import MockPlatform

        return MockPlatform()
    if sys.platform == "win32":
        from jarvis.platform.windows import WindowsPlatform

        return WindowsPlatform()
    if sys.platform == "darwin":
        from jarvis.platform.macos import MacPlatform

        return MacPlatform()
    from jarvis.platform.linux import LinuxPlatform

    return LinuxPlatform()
