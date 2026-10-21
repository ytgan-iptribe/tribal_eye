
from __future__ import annotations

import csv
import json
import os
import threading

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Literal

import pandas as pd

from dotenv import load_dotenv
from openai import OpenAI

from pydantic import BaseModel, ConfigDict


# ============================================================
# Config
# ============================================================

MODEL_NAME = "gpt-6-luna"

INPUT_CSV = Path("tribal_eye_all.csv")
PROMPT_FILE = Path("is_alert_prompt.md")

OUTPUT_DIR = Path("./")

ALL_RESULTS_FILE = OUTPUT_DIR / "all_results.csv"
YES_FILE = OUTPUT_DIR / "classified_yes.csv"
NO_FILE = OUTPUT_DIR / "classified_no.csv"

CHECKPOINT_FILE = OUTPUT_DIR / "checkpoint.json"

MAX_WORKERS = 50
VOTES_PER_EMAIL = 5

SAVE_EVERY = 100

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# Structured Output
# ============================================================

class AlertClassification(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
    )

    answer: Literal["yes", "no"]


# ============================================================
# Prompt Loading
# ============================================================

def load_prompt_template() -> str:
    return PROMPT_FILE.read_text(
        encoding="utf-8"
    )


PROMPT_TEMPLATE = load_prompt_template()


# ============================================================
# Client
# ============================================================

load_dotenv()

API_KEY = os.getenv("OPENAI_API_KEY")

if not API_KEY:
    raise RuntimeError("OPENAI_API_KEY not found")

client = OpenAI(api_key=API_KEY)


# ============================================================
# Checkpoint
# ============================================================

checkpoint_lock = threading.Lock()


def load_checkpoint() -> int:

    if not CHECKPOINT_FILE.exists():
        return 0

    try:
        with open(
            CHECKPOINT_FILE,
            "r",
            encoding="utf-8",
        ) as f:
            data = json.load(f)

        return int(data.get("last_row", 0))

    except Exception:
        return 0


def save_checkpoint(
    last_row: int,
) -> None:

    with checkpoint_lock:

        with open(
            CHECKPOINT_FILE,
            "w",
            encoding="utf-8",
        ) as f:

            json.dump(
                {
                    "last_row": last_row,
                },
                f,
                indent=2,
            )


# ============================================================
# Prompt Construction
# ============================================================

def build_prompt(
    subject: str,
    body: str,
) -> str:

    return (
        PROMPT_TEMPLATE
        .replace("{{subject}}", subject)
        .replace("{{body}}", body)
    )


# ============================================================
# Single Classification
# ============================================================

def classify_once(
    subject: str,
    body: str,
) -> str:

    prompt = build_prompt(
        subject=subject,
        body=body,
    )

    completion = client.chat.completions.parse(
        model=MODEL_NAME,
        messages=[
            {
                "role": "user",
                "content": prompt,
            }
        ],
        response_format=AlertClassification,
    )

    msg = completion.choices[0].message

    if getattr(msg, "refusal", None):
        raise RuntimeError(msg.refusal)

    if msg.parsed is None:
        raise RuntimeError(
            "No structured output returned."
        )

    result = AlertClassification.model_validate(
        msg.parsed
    )

    return result.answer


# ============================================================
# Majority Voting
# ============================================================

def classify_with_majority_vote(
    subject: str,
    body: str,
    votes: int = VOTES_PER_EMAIL,
) -> dict:

    individual_votes = []

    for _ in range(votes):

        prediction = classify_once(
            subject=subject,
            body=body,
        )

        individual_votes.append(
            prediction
        )

    counts = Counter(
        individual_votes
    )

    final_answer = counts.most_common(1)[0][0]

    return {
        "final_answer": final_answer,
        "yes_votes": counts.get("yes", 0),
        "no_votes": counts.get("no", 0),
        "raw_votes": "|".join(individual_votes),
    }


# ============================================================
# Classification Worker
# ============================================================

def process_row(
    row_index: int,
    row: pd.Series,
) -> dict:

    try:

        subject = (
            ""
            if pd.isna(row["subject"])
            else str(row["subject"])
        )

        body = (
            ""
            if pd.isna(row["body"])
            else str(row["body"])
        )

        vote_result = classify_with_majority_vote(
            subject=subject,
            body=body,
        )

        return {
            "row_index": row_index,
            "sender": row.get("sender", ""),
            "timestamp": row.get("timestamp", ""),
            "thread_id": row.get("thread_id", ""),
            "thread_topic": row.get("thread_topic", ""),
            "post_id": row.get("post_id", ""),
            "subject": subject,
            "body": body,
            "classification": vote_result["final_answer"],
            "yes_votes": vote_result["yes_votes"],
            "no_votes": vote_result["no_votes"],
            "raw_votes": vote_result["raw_votes"],
            "error": "",
        }

    except Exception as exc:

        return {
            "row_index": row_index,
            "sender": row.get("sender", ""),
            "timestamp": row.get("timestamp", ""),
            "thread_id": row.get("thread_id", ""),
            "thread_topic": row.get("thread_topic", ""),
            "post_id": row.get("post_id", ""),
            "subject": row.get("subject", ""),
            "body": row.get("body", ""),
            "classification": "",
            "yes_votes": 0,
            "no_votes": 0,
            "raw_votes": "",
            "error": repr(exc),
        }


# ============================================================
# CSV Writer
# ============================================================

write_lock = threading.Lock()


def append_row(
    filepath: Path,
    row: dict,
) -> None:

    with write_lock:

        exists = filepath.exists()

        with open(
            filepath,
            "a",
            encoding="utf-8-sig",
            newline="",
        ) as f:

            writer = csv.DictWriter(
                f,
                fieldnames=row.keys(),
            )

            if not exists:
                writer.writeheader()

            writer.writerow(row)


# ============================================================
# Main
# ============================================================

def main():

    if not INPUT_CSV.exists():
        raise FileNotFoundError(INPUT_CSV)

    df = pd.read_csv(
        INPUT_CSV,
        encoding="utf-8",
        low_memory=False,
    )

    required_columns = {
        "subject",
        "body",
    }

    missing = required_columns - set(df.columns)

    if missing:
        raise ValueError(
            f"Missing columns: {missing}"
        )

    start_row = load_checkpoint()

    print(
        f"Resuming from row {start_row}"
    )

    rows = [
        (idx, row)
        for idx, row in df.iloc[start_row:].iterrows()
    ]

    processed_count = 0

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        future_map = {
            executor.submit(
                process_row,
                idx,
                row,
            ): idx
            for idx, row in rows
        }

        for future in as_completed(
            future_map
        ):

            result = future.result()

            append_row(
                ALL_RESULTS_FILE,
                result,
            )

            classification = result[
                "classification"
            ]

            if classification == "yes":

                append_row(
                    YES_FILE,
                    result,
                )

            elif classification == "no":

                append_row(
                    NO_FILE,
                    result,
                )

            processed_count += 1

            current_row = result[
                "row_index"
            ]

            if processed_count % SAVE_EVERY == 0:

                save_checkpoint(
                    current_row
                )

                print(
                    f"Processed: {processed_count}"
                )

    save_checkpoint(len(df))

    print(
        f"Completed processing {len(df)} rows."
    )


if __name__ == "__main__":
    main()