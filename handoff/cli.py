"""Command line entry points for the demo, evaluation, and server."""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys

from .evaluation import benchmark, check_live_evaluation, run_task
from .tasks import TASKS
from .providers import ProviderError, Providers
from .server import create_server, serve_http
from .store import Handoffs


def main():
    parser = argparse.ArgumentParser(prog="handoff")
    parser.add_argument("--offline", action="store_true", help="Use scripted fixtures and make no API calls.")
    parser.add_argument("--db", type=Path, default=Path(os.getenv("HANDOFF_DB", ".handoff/history.sqlite")))
    commands = parser.add_subparsers(dest="command", required=True)

    migration = commands.add_parser("migrate-sqlite", help="Import --db SQLite history into DATABASE_URL.")
    migration.add_argument("--workspace", default="owner")

    backup = commands.add_parser("backup", help="Snapshot all workspace databases.")
    backup.add_argument("--destination", type=Path, required=True)

    serve = commands.add_parser("serve")
    serve.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=os.getenv("PORT", "8000"))

    demo = commands.add_parser("demo", help="Run one checked data-pipeline task in either direction.")
    demo.add_argument("--lead", choices=["gpt", "gemini"], required=True)
    demo.add_argument("--task", choices=[task.name for task in TASKS], default=TASKS[0].name)

    delegate = commands.add_parser("delegate", help="Try a handoff on a custom task and selected text file.")
    delegate.add_argument("--lead", choices=["gpt", "gemini"], required=True)
    delegate.add_argument("--task", required=True)
    delegate.add_argument("--context", type=Path, required=True)

    evaluation = commands.add_parser("benchmark")
    evaluation.add_argument("--output", type=Path, default=Path("results.json"))

    check = commands.add_parser("check-providers", help="Send one synthetic test prompt to each configured API.")
    check.add_argument("--confirm", action="store_true", help="Authorize two API calls.")

    history = commands.add_parser("get")
    history.add_argument("id")
    retry = commands.add_parser("retry")
    retry.add_argument("id")
    for command in (demo, delegate, evaluation, retry):
        command.add_argument("--confirm", action="store_true", help="Authorize sending supplied text to provider APIs.")

    args = parser.parse_args()
    if args.command == "migrate-sqlite":
        from .migrate import migrate_sqlite
        print(json.dumps({"imported": migrate_sqlite(args.db, args.workspace)}, indent=2))
        return 0
    if args.command == "backup":
        from .backup import backup_tree
        print(json.dumps({"saved": backup_tree(args.db, args.destination)}, indent=2))
        return 0
    if args.command in ("demo", "benchmark", "delegate", "retry") and not (args.offline or args.confirm):
        parser.error("Review the selected inputs and pass --confirm to send them to the providers.")
    if args.command == "check-providers":
        if args.offline or not args.confirm:
            parser.error("Use check-providers --confirm for a live API check.")
        providers = Providers()
        checks = {}
        for provider in ("gpt", "gemini"):
            try:
                reply = providers.call(provider, "Reply in one short sentence confirming you received this test.")
                checks[provider] = {"status": "ok", "model": reply.model, "latency_ms": reply.latency_ms}
            except ProviderError as exc:
                checks[provider] = {"status": "failed", "error": exc.code}
        print(json.dumps(checks, indent=2))
        return int(any(result["status"] != "ok" for result in checks.values()))
    handoffs = Handoffs(args.db, Providers(args.offline))
    try:
        if args.command == "serve":
            if args.transport == "http":
                serve_http(handoffs, args.host, args.port)
            else:
                create_server(handoffs).run()
            return 0
        if args.command == "demo":
            check_live_evaluation(handoffs)
            task = next(task for task in TASKS if task.name == args.task)
            result = run_task(handoffs, task, args.lead, "handoff")
        elif args.command == "delegate":
            context = args.context.read_text(encoding="utf-8")
            if not args.task.strip() or len(args.task) > 4000 or len(context) > 24000:
                raise ValueError("Provide a task up to 4,000 characters and a file up to 24,000 characters.")
            initial = json.dumps({"task": args.task, "context": context}, ensure_ascii=False)
            draft = handoffs.providers.call(args.lead, f"Draft a solution for this request:\n{initial}")
            helper = "gemini" if args.lead == "gpt" else "gpt"
            record = handoffs.ask(helper, "Review and improve the draft for the user's task.",
                                  f"{initial}\nDraft:\n{draft.text}")
            result = {"handoff": record, "lead": args.lead, "draft": asdict(draft)}
            if record["status"] == "completed":
                final = handoffs.providers.call(
                    args.lead, f"Use the relevant parts of the review to answer the original request.\n"
                               f"{initial}\nDraft:\n{draft.text}\nReview:\n{record['response']['text']}",
                )
                result["final"] = asdict(final)
            else:
                print(json.dumps(result, indent=2))
                return 1
        elif args.command == "benchmark":
            report = benchmark(handoffs, args.output)
            result = {key: value for key, value in report.items() if key != "runs"}
        elif args.command == "get":
            result = handoffs.get(args.id)
        else:
            result = handoffs.retry(args.id)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"handoff: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    if args.command == "demo":
        return int(result["status"] != "scored" or result["passed"] != result["total"])
    return int(result.get("status") == "failed")


if __name__ == "__main__":
    raise SystemExit(main())
