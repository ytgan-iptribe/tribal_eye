#!/usr/bin/env bash

set -e

MODEL="/root/projects/alert_results/output/is_alert_qwen2.5_0.5b/qwen2.5-alert-checkpoint1449-f16.gguf"

RESULTS="tuning_results.csv"

echo "threads,parallel,batch,ubatch,threads_batch,rps,p95,latency" \
    > "${RESULTS}"

THREADS_LIST=(
    16
    32
    48
    64
)

PARALLEL_LIST=(
    1
    2
    4
    8
    16
)

BATCH_LIST=(
    512
    1024
    2048
)

UBATCH_LIST=(
    128
    256
    512
)

THREADS_BATCH_LIST=(
    8
    16
    32
)

for THREADS in "${THREADS_LIST[@]}"
do
for PARALLEL in "${PARALLEL_LIST[@]}"
do
for BATCH in "${BATCH_LIST[@]}"
do
for UBATCH in "${UBATCH_LIST[@]}"
do
for THREADS_BATCH in "${THREADS_BATCH_LIST[@]}"
do

    echo
    echo "====================================================="
    echo "TEST"
    echo "threads=${THREADS}"
    echo "parallel=${PARALLEL}"
    echo "batch=${BATCH}"
    echo "ubatch=${UBATCH}"
    echo "threads_batch=${THREADS_BATCH}"
    echo "====================================================="

    pkill -f llama-server || true

    sleep 2

    ./build/bin/llama-server \
        -m "${MODEL}" \
        -c 32768 \
        -t "${THREADS}" \
        --parallel "${PARALLEL}" \
        -b "${BATCH}" \
        -ub "${UBATCH}" \
        --threads-batch "${THREADS_BATCH}" \
        --host 127.0.0.1 \
        --port 8000 \
        > server.log 2>&1 &

    PID=$!

    sleep 10

    OUTPUT=$(python /root/projects/alert_results/benchmark.py)

    RPS=$(echo "${OUTPUT}" \
        | grep "Completed requests/sec" \
        | awk '{print $3}')

    LATENCY=$(echo "${OUTPUT}" \
        | grep "Mean latency" \
        | awk '{print $3}')

    P95=$(echo "${OUTPUT}" \
        | grep "P50 / P95 latency" \
        | awk '{print $6}')

    echo "${THREADS},${PARALLEL},${BATCH},${UBATCH},${THREADS_BATCH},${RPS},${P95},${LATENCY}" \
        >> "${RESULTS}"

    kill "${PID}" || true

    sleep 3

done
done
done
done
done

echo
echo "Finished."
echo "Results:"
echo "${RESULTS}"