

# IS_ALERT Classification
# YES labelled: processed_classified_yes_updated_6.csv
# NO labelled: processed_2_classified_no_updated_6.csv
# 
# Steps: 
# combine the datasets YES/NO and load it 
# format the prompt to output the prediction in a json such that the following pydantic model with work with it: 
# class IsAlertPrediction(BaseModel):
    # label: Literal["YES", "NO"] = Field(
    #     description=(
    #         "YES if the email is an L1 alert; "
    #         "NO if the email is not an L1 alert."
    #     )
    # )
# 
# we will be training 'Qwen/Qwen2.5-0.5B-Instruct' with 32k context length. Load this model.
# write the standard training code to iterate through the dataset, evaluate, measure metrics and save the trained model in a output dir
# 


# train_is_alert.py
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Literal

import torch
from datasets import Dataset
from pydantic import BaseModel, Field, ValidationError
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    confusion_matrix,
)
from sklearn.model_selection import train_test_split
from transformers import AutoModelForCausalLM, AutoTokenizer
from trl import SFTConfig, SFTTrainer


YES_CSV = Path("processed_classified_yes_updated_6.csv")
NO_CSV = Path("processed_2_classified_no_updated_6.csv")
OUTPUT_DIR = Path("output/is_alert_qwen2.5_0.5b")

MODEL_NAME = "Qwen/Qwen2.5-0.5B-Instruct"
MAX_LENGTH = 32_768
SEED = 42

SYSTEM_PROMPT = """Classify the email for the IS_ALERT task.

Return YES if it is an L1 monitoring alert, including a notification that
the monitored condition has resolved. Return NO for other emails, including
tickets, correspondence, and summary reports that are not themselves alerts.

Respond with exactly one JSON object in this format:
{"label":"YES"}
or
{"label":"NO"}
Do not add any other text."""


class IsAlertPrediction(BaseModel):
    label: Literal["YES", "NO"] = Field(
        description=(
            "YES if the email is an L1 alert; "
            "NO if the email is not an L1 alert."
        )
    )


def read_labeled_csv(path: Path, expected_label: str) -> list[dict]:
    if not path.is_file():
        raise FileNotFoundError(path)

    rows = []
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        required = {"subject", "body"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path}: missing columns {sorted(missing)}")

        for line_number, row in enumerate(reader, start=2):
            # If the file includes a classification column, check it.
            recorded = (row.get("classification") or "").strip().upper()
            if recorded and recorded != expected_label:
                raise ValueError(
                    f"{path}:{line_number}: classification={recorded!r}, "
                    f"expected {expected_label!r}"
                )

            rows.append(
                {
                    "row_index": row.get("row_index") or str(line_number),
                    "subject": row["subject"] or "",
                    "body": row["body"] or "",
                    "label": expected_label,
                }
            )

    if not rows:
        raise ValueError(f"{path} contains no data rows")
    return rows


def make_prompt(tokenizer, row: dict) -> str:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Subject: {row['subject']}\n\n"
                f"Body:\n{row['body']}"
            ),
        },
    ]
    return tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True,
    )



def prepare_examples(tokenizer, rows: list[dict]) -> list[dict]:
    examples = []
    shortened = 0
    marker = "\n\n[... middle of email omitted ...]\n\n"

    for row in rows:
        answer = IsAlertPrediction(label=row["label"]).model_dump_json()
        completion = answer + tokenizer.eos_token

        prompt = make_prompt(tokenizer, row)
        original_length = len(
            tokenizer.encode(
                prompt + completion,
                add_special_tokens=False,
            )
        )

        if original_length > MAX_LENGTH:
            body_tokens = tokenizer.encode(
                row["body"],
                add_special_tokens=False,
            )

            # Reserve some room for the chat template, subject, marker,
            # JSON completion, and possible re-tokenization differences.
            excess = original_length - MAX_LENGTH
            keep = max(0, len(body_tokens) - excess - 128)

            while True:
                head_count = (keep * 3) // 4
                tail_count = keep - head_count

                head = tokenizer.decode(
                    body_tokens[:head_count],
                    skip_special_tokens=False,
                )
                tail = tokenizer.decode(
                    body_tokens[-tail_count:],
                    skip_special_tokens=False,
                ) if tail_count else ""

                shortened_row = {
                    **row,
                    "body": head + marker + tail,
                }
                prompt = make_prompt(tokenizer, shortened_row)

                new_length = len(
                    tokenizer.encode(
                        prompt + completion,
                        add_special_tokens=False,
                    )
                )
                if new_length <= MAX_LENGTH:
                    break

                keep -= max(128, new_length - MAX_LENGTH)
                if keep < 0:
                    raise ValueError(
                        f"row_index={row['row_index']}: the subject "
                        "and prompt alone exceed MAX_LENGTH"
                    )

            shortened += 1
            print(
                f"Shortened {row['label']} row_index={row['row_index']}: "
                f"{original_length:,} -> {new_length:,} tokens"
            )

        examples.append({
            "prompt": prompt,
            "completion": completion,
        })

    print(f"Shortened {shortened} of {len(rows)} examples")
    return examples



def predict(model, tokenizer, prompt: str) -> tuple[str | None, str]:
    inputs = tokenizer(
        prompt,
        return_tensors="pt",
        add_special_tokens=False,
    ).to(model.device)

    if inputs["input_ids"].shape[-1] + 32 > MAX_LENGTH:
        return None, "PROMPT_TOO_LONG"

    with torch.inference_mode():
        output = model.generate(
            **inputs,
            max_new_tokens=32,
            do_sample=False,
            pad_token_id=tokenizer.eos_token_id,
        )

    new_tokens = output[0, inputs["input_ids"].shape[-1]:]
    raw = tokenizer.decode(new_tokens, skip_special_tokens=True).strip()

    try:
        parsed = IsAlertPrediction.model_validate(json.loads(raw))
        return parsed.label, raw
    except (json.JSONDecodeError, ValidationError):
        return None, raw


def main() -> None:
    if not torch.cuda.is_available():
        raise RuntimeError("A CUDA GPU is required for this configuration.")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    yes_rows = read_labeled_csv(YES_CSV, "YES")
    no_rows = read_labeled_csv(NO_CSV, "NO")
    rows = yes_rows + no_rows

    print(f"YES: {len(yes_rows)} | NO: {len(no_rows)} | Total: {len(rows)}")

    # 80% train, 10% validation, 10% untouched test.
    train_rows, held_out = train_test_split(
        rows,
        test_size=0.20,
        stratify=[row["label"] for row in rows],
        random_state=SEED,
    )
    validation_rows, test_rows = train_test_split(
        held_out,
        test_size=0.50,
        stratify=[row["label"] for row in held_out],
        random_state=SEED,
    )

    print(
        f"Train: {len(train_rows)} | "
        f"Validation: {len(validation_rows)} | "
        f"Test: {len(test_rows)}"
    )

    tokenizer = AutoTokenizer.from_pretrained(
        MODEL_NAME,
        use_fast=True,
    )
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    train_dataset = Dataset.from_list(
        prepare_examples(tokenizer, train_rows)
    )
    validation_dataset = Dataset.from_list(
        prepare_examples(tokenizer, validation_rows)
    )
    test_examples = prepare_examples(tokenizer, test_rows)

    # This is a prompt/completion dataset: loss applies only to the
    # completion containing the JSON label.
    training_args = SFTConfig(
        output_dir=str(OUTPUT_DIR / "checkpoints"),
        max_length=MAX_LENGTH,
        completion_only_loss=True,
        packing=False,
        per_device_train_batch_size=1,
        per_device_eval_batch_size=1,
        gradient_accumulation_steps=8,
        gradient_checkpointing=True,
        num_train_epochs=3,
        learning_rate=2e-5,
        lr_scheduler_type="cosine",
        # warmup_ratio=0.05,
        weight_decay=0.01,
        eval_strategy="epoch",
        save_strategy="epoch",
        save_total_limit=2,
        load_best_model_at_end=True,
        metric_for_best_model="eval_loss",
        greater_is_better=False,
        logging_steps=20,
        bf16=torch.cuda.is_bf16_supported(),
        fp16=not torch.cuda.is_bf16_supported(),
        report_to="none",
        seed=SEED,
    )

    model = AutoModelForCausalLM.from_pretrained(
        MODEL_NAME,
        torch_dtype=(
            torch.bfloat16
            if torch.cuda.is_bf16_supported()
            else torch.float16
        ),
    )
    model.config.use_cache = False

    trainer = SFTTrainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=validation_dataset,
        processing_class=tokenizer,
    )

    trainer.train()
    validation_metrics = trainer.evaluate()
    print("Validation:", validation_metrics)

    model_dir = OUTPUT_DIR / "model"
    trainer.save_model(str(model_dir))
    tokenizer.save_pretrained(str(model_dir))

    model = trainer.model
    model.config.use_cache = True
    model.eval()

    predictions = []
    for row, example in zip(test_rows, test_examples):
        predicted, raw_output = predict(
            model,
            tokenizer,
            example["prompt"],
        )
        predictions.append(
            {
                "row_index": row["row_index"],
                "subject": row["subject"],
                "true_label": row["label"],
                "predicted_label": predicted or "INVALID",
                "raw_output": raw_output,
            }
        )

    true_labels = [p["true_label"] for p in predictions]
    predicted_labels = [p["predicted_label"] for p in predictions]

    # INVALID outputs count as incorrect predictions.
    report = classification_report(
        true_labels,
        predicted_labels,
        labels=["YES", "NO"],
        zero_division=0,
        output_dict=True,
    )
    metrics = {
        "test_accuracy": accuracy_score(
            true_labels,
            predicted_labels,
        ),
        "test_invalid_count": predicted_labels.count("INVALID"),
        "test_classification_report": report,
        "test_confusion_matrix": confusion_matrix(
            true_labels,
            predicted_labels,
            labels=["YES", "NO", "INVALID"],
        ).tolist(),
        "confusion_matrix_labels": ["YES", "NO", "INVALID"],
        "validation": validation_metrics,
        "splits": {
            "train": len(train_rows),
            "validation": len(validation_rows),
            "test": len(test_rows),
        },
    }

    with (OUTPUT_DIR / "metrics.json").open("w") as file:
        json.dump(metrics, file, indent=2)

    with (OUTPUT_DIR / "test_predictions.csv").open(
        "w", newline="", encoding="utf-8"
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "row_index",
                "subject",
                "true_label",
                "predicted_label",
                "raw_output",
            ],
        )
        writer.writeheader()
        writer.writerows(predictions)

    print(
        classification_report(
            true_labels,
            predicted_labels,
            labels=["YES", "NO"],
            zero_division=0,
        )
    )
    print(f"Test accuracy: {metrics['test_accuracy']:.4f}")
    print(f"Invalid JSON/label outputs: {metrics['test_invalid_count']}")
    print(f"Saved model and results to: {OUTPUT_DIR.resolve()}")

# CUDA_VISIBLE_DEVICES=3 python train.py
if __name__ == "__main__":
    main()