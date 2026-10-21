import os
import glob
import pandas as pd
import extract_msg

def process_email_dataset():
    msg_dir = '/root/projects/5012'
    csv_path = '/root/projects/alert_results/tribal_eye_all.csv'
    output_path = '/root/projects/alert_results/demo/sampled_100_emails.csv'
    
    # Target columns to extract
    selected_columns = ['timestamp', 'sender', 'subject', 'body']

    # 1. Parse .msg files from /root/projects/5012
    msg_records = []
    msg_pattern = os.path.join(msg_dir, '*.msg')
    
    for file_path in glob.glob(msg_pattern):
        try:
            msg = extract_msg.Message(file_path)
            
            # Format timestamp to ISO 8601 string if date exists
            timestamp_str = msg.date.strftime("%Y-%m-%dT%H:%M:%SZ") if msg.date else ""
            
            msg_records.append({
                'timestamp': timestamp_str,
                'sender': msg.sender or "",
                'subject': msg.subject or "",
                'body': msg.body or ""
            })
            msg.close()
        except Exception as e:
            print(f"Error parsing {file_path}: {e}")

    df_5012 = pd.DataFrame(msg_records)
    num_5012_files = len(df_5012)
    print(f"Loaded {num_5012_files} .msg files from {msg_dir}")

    # 2. Load the CSV file
    df_csv = pd.read_csv(csv_path)
    
    # Filter only requested columns that exist in the CSV
    df_csv = df_csv[selected_columns]

    # 3. Calculate sample size required to reach 100 total rows
    sample_size = 100 - num_5012_files
    
    if len(df_csv) > sample_size:
        df_sampled_csv = df_csv.sample(n=sample_size, random_state=42)
    else:
        df_sampled_csv = df_csv

    # 4. Combine both datasets
    df_final = pd.concat([df_5012, df_sampled_csv], ignore_index=True)

    # Clean up newline character artifacts if needed and save
    df_final.to_csv(output_path, index=False)
    print(f"Successfully created dataset with {len(df_final)} rows at: {output_path}")

if __name__ == '__main__':
    process_email_dataset()