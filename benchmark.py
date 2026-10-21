"""Benchmark JSON IS_ALERT classification through a local llama.cpp server.

Install: pip install httpx 'pydantic>=2'
Example: python benchmark_is_alert_llamacpp.py --requests 300 --concurrency 4
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, Field, ValidationError


class IsAlertPrediction(BaseModel):
    label: Literal["YES", "NO"] = Field(
        description="YES if the email is an L1 alert; NO otherwise."
    )


SCHEMA = IsAlertPrediction.model_json_schema()

SYSTEM_PROMPT = """Classify the email for the IS_ALERT task.

Return YES if it is an L1 monitoring alert, including a notification that
the monitored condition has resolved. Return NO for other emails, including
tickets, correspondence, and summary reports that are not themselves alerts.

Respond with exactly one JSON object in this format:
{"label":"YES"}
or
{"label":"NO"}
Do not add any other text."""


def sample_emails(path: Path, count: int, seed: int) -> tuple[list[dict], int]:
    """Uniform reservoir sample; count=0 means read and test the entire file."""
    # Outlook email bodies can exceed Python's 128 KiB CSV field default.
    field_limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(field_limit)
            break
        except OverflowError:
            field_limit //= 10

    rng = random.Random(seed)
    selected: list[dict] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not {"subject", "body"}.issubset(reader.fieldnames or []):
            raise ValueError("CSV requires subject and body columns")
        total = 0
        for total, row in enumerate(reader, 1):
            email = {"subject": row["subject"] or "", "body": row["body"] or ""}
            if count == 0 or len(selected) < count:
                selected.append(email)
            else:
                index = rng.randrange(total)
                if index < count:
                    selected[index] = email
    return selected, total


def messages_for(email: dict) -> list[dict]:
    # Same system instruction and Subject/Body user message as training.
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f'Subject: {email["subject"]}\n\nBody:\n{email["body"]}',
        },
    ]


async def classify(client: httpx.AsyncClient, endpoint: str, model: str,
                   email: dict, timeout: float) -> dict:
    started = time.perf_counter()
    try:
        response = await client.post(
            endpoint,
            json={
                "model": model,
                "messages": messages_for(email),
                "response_format": {"type": "json_object", "schema": SCHEMA},
                "temperature": 0,
                "max_tokens": 32,
                "stream": False,
            },
            timeout=timeout,
        )
        response.raise_for_status()
        result = response.json()
        raw = result["choices"][0]["message"]["content"]
        try:
            label = IsAlertPrediction.model_validate_json(raw).label
            status = "valid"
        except ValidationError:
            label, status = None, "invalid_json_or_label"
        usage = result.get("usage") or {}
        return {
            "status": status, "label": label, "raw": raw,
            "latency": time.perf_counter() - started,
            "prompt_tokens": usage.get("prompt_tokens") or 0,
            "completion_tokens": usage.get("completion_tokens") or 0,
        }
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
        return {
            "status": "error", "label": None,
            "raw": f"{type(exc).__name__}: {str(exc)[:250]}",
            "latency": time.perf_counter() - started,
            "prompt_tokens": 0, "completion_tokens": 0,
        }


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", type=Path,
                        default=Path("/root/projects/alert_results/tribal_eye_all.csv"))
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8000"
    )
    parser.add_argument("--requests", type=int, default=300,
                        help="Uniform sample size; 0 tests the entire CSV")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--progress-interval", type=float, default=5.0,
                        help="Seconds between live throughput updates")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    if args.requests < 0 or args.concurrency < 1 or args.progress_interval <= 0:
        parser.error("requests >= 0, concurrency >= 1, progress-interval > 0 required")

    sample, total = sample_emails(args.csv, args.requests, args.seed)
    if not sample:
        raise ValueError("No emails found")
    print(f"CSV rows: {total:,}; benchmark sample: {len(sample):,}; "
          f"concurrency: {args.concurrency}")
    base = args.url.rstrip("/")
    async with httpx.AsyncClient(
        limits=httpx.Limits(max_connections=args.concurrency,
                           max_keepalive_connections=args.concurrency)
    ) as client:
        models_response = await client.get(f"{base}/v1/models", timeout=30)
        models_response.raise_for_status()
        model = models_response.json()["data"][0]["id"]
        endpoint = f"{base}/v1/chat/completions"
        print(f"Server model: {model}")

        # Warm the server outside the timed interval and validate its response.
        warmup = await classify(client, endpoint, model, sample[0], args.timeout)
        if warmup["status"] != "valid":
            raise RuntimeError(f"Warmup failed: {warmup['raw']}")

        queue = asyncio.Queue()
        for email in sample:
            queue.put_nowait(email)
        results: list[dict] = []

        async def worker() -> None:
            while True:
                try:
                    email = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                results.append(await classify(client, endpoint, model,
                                              email, args.timeout))
                queue.task_done()

        start = time.perf_counter()
        stop_progress = asyncio.Event()

        async def report_progress() -> None:
            last_time = start
            last_completed = 0
            last_valid = 0
            while True:
                try:
                    await asyncio.wait_for(
                        stop_progress.wait(), timeout=args.progress_interval
                    )
                except asyncio.TimeoutError:
                    pass
                now = time.perf_counter()
                completed = len(results)
                valid_now = sum(r["status"] == "valid" for r in results)
                errors_now = sum(r["status"] == "error" for r in results)
                window_seconds = now - last_time
                if completed > last_completed and window_seconds > 0:
                    print(
                        f"[{now - start:7.1f}s] "
                        f"{completed:,}/{len(sample):,} complete | "
                        f"interval {((completed - last_completed) / window_seconds):.2f} req/s | "
                        f"valid {((valid_now - last_valid) / window_seconds):.2f}/s | "
                        f"errors {errors_now:,}",
                        flush=True,
                    )
                last_time = now
                last_completed = completed
                last_valid = valid_now
                if stop_progress.is_set():
                    return

        progress_task = asyncio.create_task(report_progress())
        try:
            await asyncio.gather(*(worker() for _ in range(args.concurrency)))
        finally:
            stop_progress.set()
            await progress_task
        seconds = time.perf_counter() - start

    valid = sum(r["status"] == "valid" for r in results)
    invalid = sum(r["status"] == "invalid_json_or_label" for r in results)
    errors = len(results) - valid - invalid
    latencies = [r["latency"] for r in results]
    print(f"Elapsed:                  {seconds:.2f} s")
    print(f"Completed requests:       {len(results):,}")
    print(f"Valid YES/NO JSON:        {valid:,}")
    print(f"Invalid JSON/label:       {invalid:,}")
    print(f"HTTP/time-out errors:     {errors:,}")
    print(f"Completed requests/sec:   {len(results) / seconds:.3f}")
    print(f"Valid classifications/sec:{valid / seconds:>8.3f}")
    print(f"Mean latency:             {statistics.mean(latencies):.2f} s")
    print(f"P50 / P95 latency:        {percentile(latencies, .50):.2f} / "
          f"{percentile(latencies, .95):.2f} s")
    prompt_tokens = sum(r["prompt_tokens"] for r in results)
    output_tokens = sum(r["completion_tokens"] for r in results)
    if prompt_tokens or output_tokens:
        print(f"Prompt / output tokens:   {prompt_tokens:,} / {output_tokens:,}")
        print(f"Prompt tokens/sec:        {prompt_tokens / seconds:.1f}")
    for result in (r for r in results if r["status"] != "valid"):
        print(f"Example {result['status']}: {result['raw'][:200]}")
        break


if __name__ == "__main__":
    asyncio.run(main())
