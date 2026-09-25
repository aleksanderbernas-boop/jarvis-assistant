"""Filesystem intelligence: search, read, write, edit, move, copy, delete — safely."""

from __future__ import annotations

import asyncio
import fnmatch
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from jarvis.api.schemas import RiskLevel
from jarvis.tools.base import RiskAssessment, Tool, ToolContext, ToolError, ToolResult
from jarvis.tools.pathsafety import PathGuard

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "AppData", "$RECYCLE.BIN", ".cache",
              "Library", ".Trash", "System Volume Information"}
_TEXT_SUFFIXES = {".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".csv", ".html", ".css", ".xml",
                  ".yml", ".yaml", ".toml", ".ini", ".cfg", ".log", ".java", ".c", ".cpp", ".h", ".cs", ".go",
                  ".rs", ".rb", ".php", ".sql", ".sh", ".ps1", ".bat", ".tex", ".rtf", ".srt"}
BULK_CONFIRM_THRESHOLD = 20


def _guard(ctx: ToolContext) -> PathGuard:
    return PathGuard(ctx.settings)


def _human_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _stat_entry(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "name": path.name,
        "path": str(path),
        "type": "dir" if path.is_dir() else "file",
        "size": _human_size(stat.st_size) if path.is_file() else None,
        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="minutes"),
    }


def _tree_stats(path: Path, limit: int = 50_000) -> tuple[int, int]:
    """(file count, total bytes) — bounded so huge trees don't stall."""
    if path.is_file():
        return 1, path.stat().st_size
    files = total = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            files += 1
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
            if files >= limit:
                return files, total
    return files, total


def _backup(ctx: ToolContext, path: Path) -> Path | None:
    """Keep a copy before overwriting/editing so changes are reversible."""
    if not path.is_file():
        return None
    backup_dir = ctx.settings.data_dir / "backups" / datetime.now().strftime("%Y%m%d")
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"{int(time.time())}_{path.name}"
    shutil.copy2(path, target)
    return target


# ── Read-only ────────────────────────────────────────────────────────────────


class ListInput(BaseModel):
    path: str = Field(description="Folder path. Shortcuts: ~, desktop, documents, downloads, pictures, screenshots")
    include_hidden: bool = False
    limit: int = Field(100, ge=1, le=500)


class FsList(Tool):
    name = "fs_list"
    description = "List a folder's contents (names, types, sizes, modified times)."
    display_name = "Inspecting folder"
    category = "files"
    Input = ListInput
    parallel_safe = True

    async def execute(self, args: ListInput, ctx: ToolContext) -> ToolResult:
        path = _guard(ctx).check(args.path)
        if not path.is_dir():
            raise ToolError(f"'{path}' is not a folder", kind="not_found")

        def scan() -> list[dict[str, Any]]:
            entries = []
            for child in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                if not args.include_hidden and child.name.startswith("."):
                    continue
                try:
                    entries.append(_stat_entry(child))
                except OSError:
                    continue
            return entries

        entries = await asyncio.to_thread(scan)
        shown = entries[: args.limit]
        return ToolResult.success(f"{len(entries)} items in {path}", {"path": str(path), "entries": shown,
                                                                      "truncated": len(entries) > len(shown)})


class SearchInput(BaseModel):
    root: str = Field("~", description="Folder to search in")
    pattern: str | None = Field(None, description="Glob on file name, e.g. '*.pdf' or 'report*'")
    name_contains: str | None = None
    content_contains: str | None = Field(None, description="Only text files containing this text")
    modified_after: str | None = Field(None, description="ISO date, e.g. 2026-09-01")
    modified_before: str | None = None
    max_results: int = Field(100, ge=1, le=1000)


class FsSearch(Tool):
    name = "fs_search"
    description = "Find files by name pattern, text content and/or modification date under a folder."
    display_name = "Searching files"
    category = "files"
    Input = SearchInput
    timeout_s = 60.0
    parallel_safe = True

    async def execute(self, args: SearchInput, ctx: ToolContext) -> ToolResult:
        root = _guard(ctx).check(args.root)
        if not root.is_dir():
            raise ToolError(f"'{root}' is not a folder", kind="not_found")
        after = datetime.fromisoformat(args.modified_after).timestamp() if args.modified_after else None
        before = datetime.fromisoformat(args.modified_before).timestamp() if args.modified_before else None
        needle = (args.content_contains or "").lower()
        name_part = (args.name_contains or "").lower()

        def walk() -> tuple[list[dict[str, Any]], int]:
            found: list[dict[str, Any]] = []
            scanned = 0
            for current, dirs, files in os.walk(root):
                dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
                if ctx.cancel.cancelled:
                    break
                for name in files:
                    scanned += 1
                    if args.pattern and not fnmatch.fnmatch(name.lower(), args.pattern.lower()):
                        continue
                    if name_part and name_part not in name.lower():
                        continue
                    path = Path(current) / name
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    if (after and stat.st_mtime < after) or (before and stat.st_mtime > before):
                        continue
                    if needle:
                        if path.suffix.lower() not in _TEXT_SUFFIXES or stat.st_size > 2_000_000:
                            continue
                        try:
                            if needle not in path.read_text(encoding="utf-8", errors="ignore").lower():
                                continue
                        except OSError:
                            continue
                    found.append({"path": str(path), "size": _human_size(stat.st_size),
                                  "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="minutes")})
                    if len(found) >= args.max_results:
                        return found, scanned
            return found, scanned

        found, scanned = await asyncio.to_thread(walk)
        return ToolResult.success(f"Found {len(found)} matching files (scanned {scanned})",
                                  {"root": str(root), "results": found, "limit_reached": len(found) >= args.max_results})


class ReadInput(BaseModel):
    path: str
    max_chars: int = Field(12_000, ge=200, le=60_000)
    offset: int = Field(0, ge=0, description="Character offset for reading long files in parts")


class FsRead(Tool):
    name = "fs_read"
    description = "Read a text, PDF or Word document (for answering questions about it or summarising it)."
    display_name = "Reading file"
    category = "files"
    Input = ReadInput
    parallel_safe = True

    async def execute(self, args: ReadInput, ctx: ToolContext) -> ToolResult:
        path = _guard(ctx).check(args.path)
        if not path.is_file():
            raise ToolError(f"'{path}' doesn't exist or isn't a file", kind="not_found")
        text = await asyncio.to_thread(self._extract, path)
        chunk = text[args.offset: args.offset + args.max_chars]
        remaining = max(0, len(text) - args.offset - len(chunk))
        return ToolResult.success(
            f"Read {len(chunk)} characters from {path.name}" + (f" ({remaining} more available)" if remaining else ""),
            {"path": str(path), "content": chunk, "total_chars": len(text), "remaining_chars": remaining},
        )

    @staticmethod
    def _extract(path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError as exc:
                raise ToolError("reading PDFs needs pypdf: pip install pypdf", kind="not_supported") from exc
            return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
        if suffix == ".docx":
            try:
                import docx
            except ImportError as exc:
                raise ToolError("reading .docx needs python-docx: pip install python-docx", kind="not_supported") from exc
            return "\n".join(p.text for p in docx.Document(str(path)).paragraphs)
        raw = path.read_bytes()[:5_000_000]
        if b"\x00" in raw[:4096]:
            raise ToolError(f"{path.name} is a binary file; I can only read text, PDF and Word documents",
                            kind="not_supported")
        return raw.decode("utf-8", errors="replace")


class InfoInput(BaseModel):
    path: str


class FsInfo(Tool):
    name = "fs_info"
    description = "Details about a file or folder: size, item count, dates, whether it exists."
    display_name = "Inspecting file"
    category = "files"
    Input = InfoInput
    parallel_safe = True

    async def execute(self, args: InfoInput, ctx: ToolContext) -> ToolResult:
        path = _guard(ctx).check(args.path)
        if not path.exists():
            return ToolResult.success(f"{path} does not exist", {"path": str(path), "exists": False})
        files, total = await asyncio.to_thread(_tree_stats, path)
        stat = path.stat()
        data = {"path": str(path), "exists": True, "type": "dir" if path.is_dir() else "file",
                "files": files, "total_size": _human_size(total),
                "created": datetime.fromtimestamp(stat.st_ctime).isoformat(timespec="minutes"),
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="minutes")}
        return ToolResult.success(f"{path.name}: {data['type']}, {data['total_size']}", data)


class OpenFileInput(BaseModel):
    path: str
    reveal: bool = Field(False, description="Show it in the file manager instead of opening it")


class OpenFile(Tool):
    name = "open_file"
    description = "Open a file or folder with its default application (or reveal it in the file manager)."
    display_name = "Opening file"
    category = "files"
    Input = OpenFileInput

    async def execute(self, args: OpenFileInput, ctx: ToolContext) -> ToolResult:
        path = _guard(ctx).check(args.path)
        if not path.exists():
            raise ToolError(f"'{path}' doesn't exist", kind="not_found")
        await ctx.platform.call(ctx.platform.reveal_path if args.reveal else ctx.platform.open_path, path)
        return ToolResult.success(f"{'Revealed' if args.reveal else 'Opened'} {path.name}", {"path": str(path)},
                                  simulated=ctx.platform.simulated)


# ── Modifying ────────────────────────────────────────────────────────────────


class WriteInput(BaseModel):
    path: str
    content: str
    mode: Literal["create", "overwrite", "append"] = "create"


class FsWrite(Tool):
    name = "fs_write"
    description = "Create a text file, or overwrite/append to one. Overwrites keep a backup copy."
    display_name = "Writing file"
    category = "files"
    Input = WriteInput
    risk = RiskLevel.MEDIUM

    async def assess(self, args: WriteInput, ctx: ToolContext) -> RiskAssessment:
        path = _guard(ctx).check(args.path, write=True)
        if path.exists() and args.mode == "overwrite":
            return RiskAssessment(RiskLevel.MEDIUM, f"Overwrite {path.name}? A backup will be kept.",
                                  [str(path)], needs_confirmation=True)
        return RiskAssessment(RiskLevel.MEDIUM)

    async def execute(self, args: WriteInput, ctx: ToolContext) -> ToolResult:
        path = _guard(ctx).check(args.path, write=True)
        if path.exists() and args.mode == "create":
            raise ToolError(f"{path.name} already exists; use mode 'overwrite' or 'append'", kind="exists")
        if ctx.simulated:
            return ToolResult.success(f"Simulated writing {len(args.content)} characters to {path}", simulated=True)

        def write() -> Path | None:
            backup = _backup(ctx, path) if args.mode == "overwrite" else None
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a" if args.mode == "append" else "w", encoding="utf-8") as handle:
                handle.write(args.content)
            return backup

        backup = await asyncio.to_thread(write)
        return ToolResult.success(f"Wrote {len(args.content)} characters to {path}",
                                  {"path": str(path), "backup": str(backup) if backup else None})

    async def verify(self, args: WriteInput, result: ToolResult, ctx: ToolContext) -> bool | None:
        if result.simulated:
            return None
        path = _guard(ctx).check(args.path)
        return path.is_file() and args.content[-200:] in path.read_text(encoding="utf-8", errors="ignore")


class EditInput(BaseModel):
    path: str
    find: str = Field(min_length=1, description="Exact text to replace")
    replace: str
    replace_all: bool = False


class FsEdit(Tool):
    name = "fs_edit"
    description = "Edit a text file by replacing exact text. A backup is kept."
    display_name = "Editing file"
    category = "files"
    Input = EditInput
    risk = RiskLevel.MEDIUM

    async def execute(self, args: EditInput, ctx: ToolContext) -> ToolResult:
        path = _guard(ctx).check(args.path, write=True)
        if not path.is_file():
            raise ToolError(f"'{path}' doesn't exist", kind="not_found")
        text = path.read_text(encoding="utf-8")
        count = text.count(args.find)
        if count == 0:
            raise ToolError("the text to replace was not found in the file", kind="not_found")
        if count > 1 and not args.replace_all:
            raise ToolError(f"the text appears {count} times; set replace_all or give more context", kind="ambiguous")
        if ctx.simulated:
            return ToolResult.success(f"Simulated {count} replacement(s) in {path.name}", simulated=True)
        backup = await asyncio.to_thread(_backup, ctx, path)
        new_text = text.replace(args.find, args.replace) if args.replace_all else text.replace(args.find, args.replace, 1)
        await asyncio.to_thread(path.write_text, new_text, encoding="utf-8")
        return ToolResult.success(f"Made {count if args.replace_all else 1} replacement(s) in {path.name}",
                                  {"path": str(path), "backup": str(backup) if backup else None})

    async def verify(self, args: EditInput, result: ToolResult, ctx: ToolContext) -> bool | None:
        if result.simulated:
            return None
        text = _guard(ctx).check(args.path).read_text(encoding="utf-8")
        return args.replace in text if args.replace else True


class MkdirInput(BaseModel):
    path: str


class FsMkdir(Tool):
    name = "fs_mkdir"
    description = "Create a folder (and any missing parents)."
    display_name = "Creating folder"
    category = "files"
    Input = MkdirInput
    risk = RiskLevel.MEDIUM

    async def execute(self, args: MkdirInput, ctx: ToolContext) -> ToolResult:
        path = _guard(ctx).check(args.path, write=True)
        if ctx.simulated:
            return ToolResult.success(f"Simulated creating {path}", simulated=True)
        await asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)
        return ToolResult.success(f"Folder ready: {path}", {"path": str(path)})

    async def verify(self, args: MkdirInput, result: ToolResult, ctx: ToolContext) -> bool | None:
        return None if result.simulated else _guard(ctx).check(args.path).is_dir()


class TransferInput(BaseModel):
    sources: list[str] = Field(min_length=1, max_length=2000, description="Files or folders to move/copy")
    destination: str = Field(description="Destination folder (created if missing), or a new name for a single item")
    overwrite: bool = False


def _plan_transfer(args: TransferInput, ctx: ToolContext) -> list[tuple[Path, Path]]:
    guard = _guard(ctx)
    sources = [guard.check(s, write=True) for s in args.sources]
    missing = [str(s) for s in sources if not s.exists()]
    if missing:
        raise ToolError(f"not found: {', '.join(missing[:5])}", kind="not_found")
    dest = guard.check(args.destination, write=True)
    single_rename = len(sources) == 1 and not dest.is_dir() and (dest.suffix or not args.destination.endswith(("/", "\\")))
    if single_rename and dest.parent.is_dir() and dest.suffix == sources[0].suffix:
        return [(sources[0], dest)]
    return [(s, dest / s.name) for s in sources]


class _TransferTool(Tool):
    Input = TransferInput
    risk = RiskLevel.MEDIUM
    category = "files"
    timeout_s = 300.0
    verb = "move"

    async def assess(self, args: TransferInput, ctx: ToolContext) -> RiskAssessment:
        plan = _plan_transfer(args, ctx)
        overwrites = [d for _, d in plan if d.exists()]
        if overwrites and not args.overwrite:
            raise ToolError(f"{len(overwrites)} destination item(s) already exist (e.g. {overwrites[0].name}); "
                            "set overwrite=true to replace them", kind="exists")
        files, total = 0, 0
        for source, _ in plan:
            f, t = _tree_stats(source)
            files, total = files + f, total + t
        needs = files > BULK_CONFIRM_THRESHOLD or bool(overwrites) or any(s.is_dir() for s, _ in plan)
        dest_desc = plan[0][1].parent if plan else args.destination
        summary = (f"That will {self.verb} {files} file{'s' if files != 1 else ''} ({_human_size(total)}) to "
                   f"{dest_desc}" + (f", replacing {len(overwrites)} existing item(s)" if overwrites else "") + ". Proceed?")
        details = [f"{s} → {d}" for s, d in plan[:15]] + ([f"…and {len(plan) - 15} more"] if len(plan) > 15 else [])
        level = RiskLevel.HIGH if overwrites and files > BULK_CONFIRM_THRESHOLD else RiskLevel.MEDIUM
        return RiskAssessment(level, summary, details, needs_confirmation=needs)

    async def execute(self, args: TransferInput, ctx: ToolContext) -> ToolResult:
        plan = _plan_transfer(args, ctx)
        if ctx.simulated:
            return ToolResult.success(f"Simulated: would {self.verb} {len(plan)} item(s)", simulated=True,
                                      data={"planned": [f"{s} → {d}" for s, d in plan[:50]]})
        done: list[str] = []
        failed: list[str] = []
        for index, (source, dest) in enumerate(plan):
            ctx.cancel.raise_if_cancelled()
            try:
                await asyncio.to_thread(self._transfer, source, dest, args.overwrite)
                done.append(str(dest))
            except OSError as exc:
                failed.append(f"{source.name}: {exc.strerror or exc}")
            if len(plan) > 50 and index % 50 == 0:
                await ctx.progress(f"{self.verb.title()}d {index}/{len(plan)} items")
        summary = f"{self.verb.title()}d {len(done)} of {len(plan)} item(s)"
        if failed:
            summary += f"; {len(failed)} failed"
        result = ToolResult.success(summary, {"completed": done[:100], "failed": failed[:50]})
        if not done:
            return ToolResult.failure(summary + ": " + "; ".join(failed[:3]), data=result.data)
        return result

    def _transfer(self, source: Path, dest: Path, overwrite: bool) -> None:
        raise NotImplementedError

    async def verify(self, args: TransferInput, result: ToolResult, ctx: ToolContext) -> bool | None:
        if result.simulated or not result.data:
            return None
        completed = [Path(p) for p in result.data.get("completed", [])]
        return bool(completed) and all(p.exists() for p in completed) and not result.data.get("failed")


class FsMove(_TransferTool):
    name = "fs_move"
    description = ("Move (or rename) one or many files/folders into a destination folder. Use this for "
                   "organising files; pass all files in one call.")
    display_name = "Moving files"
    verb = "move"

    def _transfer(self, source: Path, dest: Path, overwrite: bool) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and overwrite:
            shutil.rmtree(dest) if dest.is_dir() else dest.unlink()
        shutil.move(str(source), str(dest))

    async def verify(self, args: TransferInput, result: ToolResult, ctx: ToolContext) -> bool | None:
        base = await super().verify(args, result, ctx)
        if base is None:
            return None
        plan_sources = [PathGuard(ctx.settings).check(s) for s in args.sources]
        return base and not any(s.exists() for s in plan_sources)


class FsCopy(_TransferTool):
    name = "fs_copy"
    description = "Copy one or many files/folders into a destination folder."
    display_name = "Copying files"
    verb = "copy"

    def _transfer(self, source: Path, dest: Path, overwrite: bool) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, dest, dirs_exist_ok=overwrite)
        else:
            shutil.copy2(source, dest)


class RenameInput(BaseModel):
    path: str
    new_name: str = Field(description="New file/folder name only, not a path")


class FsRename(Tool):
    name = "fs_rename"
    description = "Rename a file or folder in place."
    display_name = "Renaming"
    category = "files"
    Input = RenameInput
    risk = RiskLevel.MEDIUM

    def validate(self, args: RenameInput, ctx: ToolContext) -> None:
        if any(sep in args.new_name for sep in ("/", "\\")) or args.new_name in {".", ".."}:
            raise ToolError("new_name must be a plain name, not a path", kind="invalid_arguments")

    async def execute(self, args: RenameInput, ctx: ToolContext) -> ToolResult:
        guard = _guard(ctx)
        path = guard.check(args.path, write=True)
        target = guard.check(path.parent / args.new_name, write=True)
        if not path.exists():
            raise ToolError(f"'{path}' doesn't exist", kind="not_found")
        if target.exists():
            raise ToolError(f"'{target.name}' already exists", kind="exists")
        if ctx.simulated:
            return ToolResult.success(f"Simulated renaming {path.name} to {target.name}", simulated=True)
        await asyncio.to_thread(path.rename, target)
        return ToolResult.success(f"Renamed {path.name} to {target.name}", {"path": str(target)})

    async def verify(self, args: RenameInput, result: ToolResult, ctx: ToolContext) -> bool | None:
        if result.simulated:
            return None
        return Path(result.data["path"]).exists() and not _guard(ctx).check(args.path).exists()


class DeleteInput(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=500)


class FsDelete(Tool):
    name = "fs_delete"
    description = ("Delete files or folders. Items go to the Recycle Bin when possible. Always requires the "
                   "user's explicit confirmation — identify the exact items first.")
    display_name = "Deleting files"
    category = "files"
    Input = DeleteInput
    risk = RiskLevel.HIGH
    timeout_s = 300.0

    def _targets(self, args: DeleteInput, ctx: ToolContext) -> list[Path]:
        guard = _guard(ctx)
        targets = [guard.check(p, write=True) for p in args.paths]
        missing = [str(t) for t in targets if not t.exists()]
        if missing:
            raise ToolError(f"not found: {', '.join(missing[:5])}", kind="not_found")
        return targets

    async def assess(self, args: DeleteInput, ctx: ToolContext) -> RiskAssessment:
        targets = self._targets(args, ctx)
        files = total = 0
        for target in targets:
            f, t = await asyncio.to_thread(_tree_stats, target)
            files, total = files + f, total + t
        can_trash = ctx.platform.simulated or self._trash_available(ctx)
        subject = f"'{targets[0].name}'" if len(targets) == 1 else f"{len(targets)} items"
        size = f"{files} file{'s' if files != 1 else ''}, {_human_size(total)}"
        summary = (f"That will move {subject} ({size}) to the Recycle Bin. Proceed?" if can_trash
                   else f"That will PERMANENTLY delete {subject} ({size}). Proceed?")
        return RiskAssessment(RiskLevel.HIGH, summary, [str(t) for t in targets[:20]], needs_confirmation=True)

    @staticmethod
    def _trash_available(ctx: ToolContext) -> bool:
        try:
            import send2trash  # noqa: F401

            return True
        except ImportError:
            return os.name == "nt" or shutil.which("gio") is not None

    async def execute(self, args: DeleteInput, ctx: ToolContext) -> ToolResult:
        targets = self._targets(args, ctx)
        if ctx.simulated:
            await ctx.platform.call(lambda: [ctx.platform.trash(t) for t in targets])
            return ToolResult.success(f"Simulated deleting {len(targets)} item(s)", simulated=True)
        deleted, failed, permanent = [], [], False
        for target in targets:
            ctx.cancel.raise_if_cancelled()
            try:
                trashed = await ctx.platform.call(ctx.platform.trash, target)
                if not trashed:
                    permanent = True
                    if target.is_dir():
                        await asyncio.to_thread(shutil.rmtree, target)
                    else:
                        await asyncio.to_thread(target.unlink)
                deleted.append(str(target))
            except OSError as exc:
                failed.append(f"{target.name}: {exc.strerror or exc}")
        where = "permanently deleted" if permanent else "moved to the Recycle Bin"
        summary = f"{len(deleted)} of {len(targets)} item(s) {where}" + (f"; {len(failed)} failed" if failed else "")
        if not deleted:
            return ToolResult.failure(summary + ": " + "; ".join(failed[:3]))
        return ToolResult.success(summary, {"deleted": deleted, "failed": failed, "permanent": permanent})

    async def verify(self, args: DeleteInput, result: ToolResult, ctx: ToolContext) -> bool | None:
        if result.simulated:
            return None
        return all(not Path(p).exists() for p in result.data.get("deleted", []))


TOOLS = [FsList, FsSearch, FsRead, FsInfo, OpenFile, FsWrite, FsEdit, FsMkdir, FsMove, FsCopy, FsRename, FsDelete]
