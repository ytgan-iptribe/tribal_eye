cd /root/projects/llama.cpp
CUDA_VISIBLE_DEVICES=3 \
./build/bin/llama-server \
  -m /root/projects/alert_results/output/is_alert_qwen2.5_0.5b/qwen2.5-alert-checkpoint1449-f16.gguf \
  -ngl 999 \
  -t 64 \
  --parallel 16 \
  --host 127.0.0.1 \
  --port 8000

cd /root/projects/llama.cpp
./build/bin/llama-server \
  -m /root/projects/alert_results/output/is_alert_qwen2.5_0.5b/qwen2.5-alert-checkpoint1449-f16.gguf \
  -ngl 0 \
  -t 64 \
  -tb 64 \
  --parallel 1 \
  --ctx-size 32768 \
  --batch-size 32768 \
  --ubatch-size 512 \
  --host 127.0.0.1 \
  --port 8000



parallel 4
Elapsed:                  102.85 s
Completed requests:       300
Valid YES/NO JSON:        300
Invalid JSON/label:       0
HTTP/time-out errors:     0
Completed requests/sec:   2.917

parallel 8
Elapsed:                  105.49 s
Completed requests:       300
Valid YES/NO JSON:        300
Invalid JSON/label:       0
HTTP/time-out errors:     0
Completed requests/sec:   2.844
Valid classifications/sec:   2.844
Mean latency:             1.41 s
P50 / P95 latency:        1.05 / 4.47 s
Prompt / output tokens:   208,391 / 1,800
Prompt tokens/sec:        1975.5


# python convert_hf_to_gguf.py /root/projects/alert_results/output/is_alert_qwen2.5_0.5b/model --outfile /root/projects/alert_results/output/is_alert_qwen2.5_0.5b/qwen2.5-alert-f16.gguf --outtype f16

# python convert_hf_to_gguf.py \
# /root/projects/alert_results/output/is_alert_qwen2.5_0.5b/checkpoints/checkpoint-1449 \
# --outfile qwen2.5-alert-checkpoint1449-f16.gguf \
# --outtype f16

