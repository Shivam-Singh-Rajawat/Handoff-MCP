"""Compare matched two-stage reviews with single-pass and cross-model baselines."""
from collections import defaultdict
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

from .providers import ProviderError, provider_setting_names
from .store import Handoffs
from .tasks import TASKS, Task

STRATEGIES = ("single", "same_model", "handoff")
EVAL_IMAGE = "python:3.12-slim"


def score(task: Task, source: str, offline: bool) -> dict:
    fence = re.fullmatch(r"```(?:python)?\s*\n(.*?)\n```", source.strip(), re.DOTALL)
    if fence:
        source = fence[1]
    runner = """\
from candidate import transform

examples = {examples!r}
outcomes = []
for argument, expected in examples:
    try:
        outcomes.append(transform(argument) == expected)
    except Exception:
        outcomes.append(False)
print({marker!r} + ''.join('1' if outcome else '0' for outcome in outcomes))
"""
    marker = f"RESULT-{uuid4().hex}="
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        path.chmod(0o755)
        (path / "candidate.py").write_text(source, encoding="utf-8")
        (path / "check.py").write_text(runner.format(examples=task.examples, marker=marker), encoding="utf-8")
        if offline:
            command = [sys.executable, "-B", str(path / "check.py")]
        else:
            command = [
                "docker", "run", "--rm", "--pull=never", "--name", marker.removesuffix("="),
                "--network=none", "--read-only", "--memory=128m", "--cpus=1", "--pids-limit=32",
                "--log-driver=none",
                "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user=65534:65534",
                "--mount", f"type=bind,src={directory},dst=/work,readonly", "-w", "/work",
                EVAL_IMAGE, "python", "-B", "check.py",
            ]
        try:
            with tempfile.TemporaryFile() as output:
                result = subprocess.run(command, stdout=output, stderr=subprocess.DEVNULL, timeout=12)
                if not offline and result.returncode == 125:
                    raise RuntimeError("Docker could not start the isolated evaluator.")
                output.seek(0)
                captured = output.read(64_000).decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            if not offline:
                subprocess.run(["docker", "rm", "-f", marker.removesuffix("=")],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            return {"passed": 0, "total": len(task.examples), "status": "test_timeout"}
    match = re.search(rf"^{marker}([01]{{{len(task.examples)}}})$", captured, re.MULTILINE)
    return {"passed": match[1].count("1") if match and result.returncode == 0 else 0,
            "total": len(task.examples), "status": "scored" if match and result.returncode == 0 else "invalid_candidate"}


def run_task(handoffs: Handoffs, task: Task, lead: str, strategy: str) -> dict:
    providers = handoffs.providers
    other = "gemini" if lead == "gpt" else "gpt"
    initial = (
        "Fix the Python function below. Return the complete Python module only, including "
        "transform. Use only the standard library.\n"
        f"Requirement: {task.requirement}\n"
        f"Broken module:\n{task.buggy}\n"
        f"Failing example (input, expected output): {task.examples[0]!r}"
    )
    calls = []
    record = {"task": task.name, "lead": lead, "strategy": strategy, "handoff_id": None}
    start = time.monotonic()
    try:
        draft = providers.call(lead, initial, fixture=task.buggy)
        calls.append(asdict(draft))
        final = draft.text
        if strategy != "single":
            reviewer = lead if strategy == "same_model" else other
            review = handoffs.ask(
                reviewer, "Review the candidate for correctness. Identify edge cases and suggest fixes.",
                f"{initial}\nCandidate:\n{draft.text}",
                fixture=f"Scripted review fixture. Reference implementation:\n{task.fixed}",
            )
            record["handoff_id"] = review["id"]
            if review["status"] != "completed":
                raise ProviderError(review["error"])
            calls.append(review["response"])
            synthesis = (
                f"{initial}\nCandidate:\n{draft.text}\nReviewer advice:\n{review['response']['text']}\n"
                "Check the advice against the requirement. Return only the final Python module."
            )
            refined = providers.call(lead, synthesis, fixture=task.fixed)
            calls.append(asdict(refined))
            final = refined.text
        record.update(score(task, final, providers.offline))
    except ProviderError as exc:
        record.update(passed=0, total=len(task.examples), status=exc.code)

    costs = [call["estimated_cost_usd"] for call in calls]
    record.update(
        elapsed_ms=round((time.monotonic() - start) * 1000),
        estimated_cost_usd=sum(costs) if costs and all(cost is not None for cost in costs) else None,
        calls=calls,
    )
    return record


def check_live_evaluation(handoffs: Handoffs) -> None:
    if not handoffs.providers.offline:
        settings = (*provider_setting_names("gpt"), *provider_setting_names("gemini"))
        missing = [name for name in settings if not os.getenv(name)]
        if missing:
            raise ValueError("Missing live provider configuration: " + ", ".join(missing))
        if not shutil.which("docker"):
            raise RuntimeError("Docker is required to score live model-generated code.")
        subprocess.run(["docker", "image", "inspect", EVAL_IMAGE], check=True, stdout=subprocess.DEVNULL)

def benchmark(handoffs: Handoffs, output: Path) -> dict:
    check_live_evaluation(handoffs)
    rows = []
    for task in TASKS:
        for lead in ("gpt", "gemini"):
            for strategy in STRATEGIES:
                rows.append(run_task(handoffs, task, lead, strategy))
                # Keep partial results available after an interruption.
                save_report(output, summarize(rows, handoffs.providers.offline))
                print(f"{len(rows):2}/72  {task.name}  {lead}  {strategy}  {rows[-1]['status']}",
                      file=sys.stderr, flush=True)
    return summarize(rows, handoffs.providers.offline)


def summarize(rows: list[dict], offline: bool) -> dict:
    groups = defaultdict(list)
    for row in rows:
        groups[(row["lead"], row["strategy"])].append(row)
    metrics = []
    for (lead, strategy), runs in groups.items():
        costs = [run["estimated_cost_usd"] for run in runs]
        metrics.append({
            "lead": lead, "strategy": strategy, "tasks": len(runs),
            "tasks_passed": sum(run["passed"] == run["total"] for run in runs),
            "assertions_passed": sum(run["passed"] for run in runs),
            "assertions_total": sum(run["total"] for run in runs),
            "mean_elapsed_ms": round(sum(run["elapsed_ms"] for run in runs) / len(runs), 1),
            "estimated_cost_usd": sum(costs) if all(cost is not None for cost in costs) else None,
        })
    return {"mode": "offline_scripted_fixture" if offline else "live_provider",
            "runs_completed": len(rows), "runs_expected": 72, "metrics": metrics, "runs": rows}


def save_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
