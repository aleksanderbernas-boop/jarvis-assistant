"""Command line entry point.

    python -m jarvis                 start JARVIS (live mode per .env)
    python -m jarvis serve --mock    start with simulated tools/LLM (safe for development)
    python -m jarvis check           run boot checks + diagnostics and print them
    python -m jarvis models          list the models your LLM API key can use
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import webbrowser
from typing import Any

from jarvis import __version__
from jarvis.config import RunMode, load_settings


def _settings(args: argparse.Namespace):  # type: ignore[no-untyped-def]
    overrides: dict[str, Any] = {}
    if getattr(args, "mock", False):
        overrides["mode"] = RunMode.MOCK
    settings = load_settings(**overrides)
    if getattr(args, "mock", False) and settings.llm.provider != "mock":
        settings.llm.provider = "mock"
    if getattr(args, "port", None):
        settings.port = args.port
    return settings


def serve(args: argparse.Namespace) -> None:
    import uvicorn

    from jarvis.app import create_app

    settings = _settings(args)
    app = create_app(settings)
    url = f"http://{settings.host}:{settings.port}/"
    print(f"\n  J.A.R.V.I.S. v{__version__} — {settings.mode.value} mode — {url}\n")
    if args.open:
        webbrowser.open(url)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning", ws_ping_interval=20)


async def _check(args: argparse.Namespace) -> int:
    from jarvis.core.services import Services
    from jarvis.logging_setup import setup_logging

    settings = _settings(args)
    setup_logging("WARNING", settings.data_dir / "logs", settings.secret_values())
    services = Services(settings)
    await services.boot()
    report = await services.diagnostics()
    for stage in services.boot_stages:
        mark = {"ok": "✓", "warning": "!", "failed": "✗"}.get(stage.status, "?")
        print(f"  {mark} {stage.label:<22} {stage.detail}")
    print(json.dumps(report["components"], indent=2))
    await services.shutdown()
    return 0 if all(s.status != "failed" for s in services.boot_stages) else 1


async def _models(args: argparse.Namespace) -> int:
    from jarvis.llm.factory import build_llm

    settings = _settings(args)
    llm = build_llm(settings)
    try:
        for model in await llm.list_models():
            print(model)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        await llm.close()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="jarvis", description="J.A.R.V.I.S. assistant")
    sub = parser.add_subparsers(dest="command")
    for name in ("serve", "check", "models"):
        p = sub.add_parser(name)
        p.add_argument("--mock", action="store_true", help="simulated tools, web and LLM")
        if name == "serve":
            p.add_argument("--port", type=int)
            p.add_argument("--open", action="store_true", help="open the HUD in your browser")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()
    command = args.command or "serve"
    if command == "serve":
        if not hasattr(args, "mock"):
            args = parser.parse_args(["serve", *sys.argv[1:]])
        serve(args)
    elif command == "check":
        sys.exit(asyncio.run(_check(args)))
    elif command == "models":
        sys.exit(asyncio.run(_models(args)))


if __name__ == "__main__":
    main()
