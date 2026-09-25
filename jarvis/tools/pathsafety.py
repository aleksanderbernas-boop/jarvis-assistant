"""Path resolution and access control for filesystem tools.

Every path the model supplies is expanded, resolved (symlinks included) and
checked against the allowed roots and a denylist of sensitive locations.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from jarvis.config import PROJECT_ROOT, Settings
from jarvis.tools.base import ToolError

_SENSITIVE_DIRS = {".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".password-store", "credentials"}
_SENSITIVE_FILES = re.compile(
    r"(^\.env(\..*)?$|^id_(rsa|dsa|ecdsa|ed25519)(\.pub)?$|\.(pem|key|pfx|p12|kdbx|keychain)$|"
    r"^(login data|cookies|web data|key3\.db|key4\.db|logins\.json)$)",
    re.IGNORECASE,
)


def _known_folder(name: str) -> Path | None:
    home = Path.home()
    folders = {
        "home": home, "~": home, "desktop": home / "Desktop", "documents": home / "Documents",
        "docs": home / "Documents", "downloads": home / "Downloads", "pictures": home / "Pictures",
        "photos": home / "Pictures", "music": home / "Music", "videos": home / "Videos",
        "screenshots": home / "Pictures" / "Screenshots",
    }
    onedrive = os.environ.get("OneDrive")
    if onedrive:
        for key in ("desktop", "documents", "pictures"):
            candidate = Path(onedrive) / folders[key].name
            if candidate.is_dir() and not folders[key].is_dir():
                folders[key] = candidate
        folders["onedrive"] = Path(onedrive)
    return folders.get(name.lower().strip().rstrip("/\\"))


def resolve_user_path(raw: str) -> Path:
    raw = raw.strip().strip('"').strip("'")
    if not raw:
        raise ToolError("a path is required", kind="invalid_arguments")
    first, _, rest = raw.replace("\\", "/").partition("/")
    known = _known_folder(first)
    if known is not None:
        path = known / rest if rest else known
    else:
        path = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not path.is_absolute():
            path = Path.home() / path
    return path.resolve(strict=False)


class PathGuard:
    def __init__(self, settings: Settings) -> None:
        self._roots = [p.expanduser().resolve(strict=False) for p in settings.permissions.allowed_paths]
        self._protected = [settings.data_dir.resolve(strict=False), (PROJECT_ROOT / ".env").resolve(strict=False)]

    @property
    def roots(self) -> list[Path]:
        return list(self._roots)

    def check(self, raw: str | Path, *, write: bool = False) -> Path:
        path = resolve_user_path(str(raw)) if isinstance(raw, str) else raw.resolve(strict=False)
        if not any(path == root or path.is_relative_to(root) for root in self._roots):
            allowed = ", ".join(str(r) for r in self._roots)
            raise ToolError(
                f"'{path}' is outside the folders I'm allowed to access ({allowed}). "
                "Add it to JARVIS_ALLOWED_PATHS to permit it.",
                kind="permission_denied",
            )
        if any(part.lower() in _SENSITIVE_DIRS for part in path.parts) or _SENSITIVE_FILES.search(path.name):
            raise ToolError(f"'{path.name}' looks like a credential or secret store; I won't touch it.",
                            kind="permission_denied")
        for protected in self._protected:
            if write and (path == protected or path.is_relative_to(protected)):
                raise ToolError("that is JARVIS's own data; it can't be modified through file tools.",
                                kind="permission_denied")
            if path == protected and protected.name == ".env":
                raise ToolError("the .env file holds secrets; I won't read or change it.", kind="permission_denied")
        if write and any(path == root for root in self._roots):
            raise ToolError("I won't modify an entire allowed root folder in one operation.", kind="permission_denied")
        return path
