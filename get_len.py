import csv
from pathlib import Path

yes_input = Path("processed_classified_yes_updated_5.csv")
no_input = Path("processed_2_classified_no_updated_5.csv")

yes_output = Path("processed_classified_yes_updated_6.csv")
no_output = Path("processed_2_classified_no_updated_6.csv")


def read_csv(path):
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        return reader.fieldnames, list(reader)


def write_csv(path, columns, rows):
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


yes_columns, yes_rows = read_csv(yes_input)
no_columns, no_rows = read_csv(no_input)

if yes_columns != no_columns:
    raise ValueError("The CSV files have different columns")

rows_to_move = [
    row for row in yes_rows
    if "STUCK ORDER ALERT" in row["subject"].upper()
]

if len(rows_to_move) != 343:
    raise ValueError(
        f"Expected 343 STUCK ORDER ALERT rows, found {len(rows_to_move)}"
    )

move_ids = {row["row_index"].strip() for row in rows_to_move}
if len(move_ids) != 343:
    raise ValueError("Duplicate row_index among the selected rows")

no_ids = {row["row_index"].strip() for row in no_rows}
overlap = move_ids & no_ids
if overlap:
    raise ValueError(f"Rows already in no: {sorted(overlap, key=int)}")

remaining_yes = [
    row for row in yes_rows
    if row["row_index"].strip() not in move_ids
]

for row in rows_to_move:
    row["classification"] = "no"

updated_no = no_rows + rows_to_move

write_csv(yes_output, yes_columns, remaining_yes)
write_csv(no_output, no_columns, updated_no)

print(f"Moved {len(rows_to_move)} STUCK ORDER ALERT emails to no")
print(f"Yes: {len(remaining_yes)} -> {yes_output}")  # 2,104
print(f"No: {len(updated_no)} -> {no_output}")      # 2,719 if input no has 2,376