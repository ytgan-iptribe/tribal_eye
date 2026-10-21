import csv
from pathlib import Path
from statistics import mean, median

from transformers import AutoTokenizer

YES_CSV = Path("processed_classified_yes_updated_6.csv")
NO_CSV = Path("processed_2_classified_no_updated_6.csv")  # Set your actual NO filename
MODEL_NAME = "Qwen/Qwen2.5-0.5B-Instruct"

tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME, use_fast=True)

if YES_CSV.resolve() == NO_CSV.resolve():
    raise ValueError("YES_CSV and NO_CSV point to the same file")

lengths = {
    "YES": {"prompt": [], "training": []},
    "NO": {"prompt": [], "training": []},
}


def token_count(messages, *, add_generation_prompt):
    rendered = tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=add_generation_prompt,
    )
    if not isinstance(rendered, str):
        raise TypeError(f"Expected rendered text, got {type(rendered).__name__}")

    return len(tokenizer.encode(rendered, add_special_tokens=False))


def measure(path, label):
    if not path.is_file():
        raise FileNotFoundError(path)

    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        required = {"subject", "body"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path}: missing columns {sorted(missing)}")

        for row in reader:
            content = (
                f"Subject: {row['subject'] or ''}\n\n"
                f"Body:\n{row['body'] or ''}"
            )
            user_message = {"role": "user", "content": content}

            prompt_length = token_count(
                [user_message],
                add_generation_prompt=True,
            )
            training_length = token_count(
                [user_message, {"role": "assistant", "content": label.lower()}],
                add_generation_prompt=False,
            )

            if prompt_length < 10 or training_length < 10:
                raise ValueError(
                    f"Unexpectedly short tokenization in {path}, "
                    f"row_index={row.get('row_index')!r}: "
                    f"prompt={prompt_length}, training={training_length}"
                )

            lengths[label]["prompt"].append(prompt_length)
            lengths[label]["training"].append(training_length)


def report(label, values):
    print(
        f"{label:<10} count={len(values):>5}  "
        f"median={median(values):>8.1f}  "
        f"mean={mean(values):>8.1f}  "
        f"max={max(values):>7}"
    )


print(f"YES file: {YES_CSV.resolve()}")
print(f"NO file:  {NO_CSV.resolve()}")

measure(YES_CSV, "YES")
measure(NO_CSV, "NO")

for kind in ("prompt", "training"):
    print(f"\n{kind.upper()} TOKEN LENGTHS")
    report("YES", lengths["YES"][kind])
    report("NO", lengths["NO"][kind])
    report("COMBINED", lengths["YES"][kind] + lengths["NO"][kind])