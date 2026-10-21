# Structured DATA / SMS / CALL verification

This package contains modified copies of the three supplied Bash scripts, a shared JSON reporting helper, and a Pydantic v2 SSH runner. It has not been deployed to or executed against your server.

## Why change the scripts?

- DATA suppresses per-operator failures with `|| true` and finishes with `exit 0`.
- SMS finishes with `cleanup_and_exit 0`, even when FINAL_STATUS is FAILED or UNKNOWN.
- CALL finishes with `cleanup_and_exit 0`, even when FINAL_STATUS is FAILED or MISSED.
- SMS/CALL primarily write diagnostics to files, so parsing SSH stdout alone cannot recover reliable results.

Each modified script prints exactly one line beginning `MODEM_RESULT_JSON=` before it exits. Existing human logs can still appear around this line. The JSON encoder is Python's standard library; the server does not need Pydantic.

The shared EXIT trap covers explicit early exits and normal completion. Abrupt termination such as SIGKILL, a syntax error before the helper loads, missing Python, or lost SSH can prevent JSON emission. The runner treats missing, invalid, duplicated, or wrong-kind JSON as failure. It does not infer success from a zero exit code.

## Result contract

`ScriptResult` contains `kind`, `success`, `checks`, `errors`, and `warnings`. `checks` contains one result per DATA operator, or one result for the SMS/CALL direction. Each check contains `subject`, `status`, `success`, `errors`, and `details`.

`ExecutionResult` adds the step name, exit code, raw stdout/stderr, and a flattened list of script/check/SSH errors. `WorkflowResult` records executions and skipped steps.

| Test | Success rule | Limits |
|---|---|---|
| DATA | Every requested operator passes the script's connectivity rule | The original rule accepts received ping replies or HTTP 200 fallback; it does not require every diagnostic metric to pass. |
| SMS | Final response contains `+CMGS:` with no error | Confirms modem submission, not receipt by the destination. Plain `OK` now produces UNKNOWN. End-to-end receipt requires a separate correlated incoming SMS check. |
| CALL | Final status is SUCCESS | Uses the original local/incoming END detection. RING-RELEASE is a failed verification in this contract, because ringing alone does not prove an answered call. The original call script stops its internal retries on RING-RELEASE. |

For any failed service check, the modified script exits nonzero even if its original code would have exited zero. Earlier failed attempts, discovery diagnostics, and DB logging errors are retained in `warnings`; they do not override an eventual service success. `success` answers the service-verification question, not whether every logging/email side effect succeeded.

Data results are reset before each attempt so an early port error cannot accidentally reuse the previous operator's result. Error diagnostics survive command substitutions through a private per-run file. CALL includes the final raw modem response in details, and SMS modem rejection diagnostics include its response in warnings.

## Install

1. Back up the three existing server scripts.
2. Copy `check_modem_data.sh`, `smsmodem2TOmodem1.sh`, `callmodem2TOmodem1.sh`, and `modem_result.sh` together into `/usr/local/bin/`.
3. Confirm `python3` exists on the server. Keep all existing modem, DB, incoming-handler, mail, and other dependencies.
4. Check syntax and executable permissions:

```bash
bash -n /usr/local/bin/modem_result.sh
bash -n /usr/local/bin/check_modem_data.sh
bash -n /usr/local/bin/smsmodem2TOmodem1.sh
bash -n /usr/local/bin/callmodem2TOmodem1.sh
chmod +x /usr/local/bin/check_modem_data.sh /usr/local/bin/smsmodem2TOmodem1.sh /usr/local/bin/callmodem2TOmodem1.sh
```

On the client install `pydantic>=2,<3` and `paramiko`. Provision the verified server key in your normal known_hosts file, or set MODEM_KNOWN_HOSTS to a custom known_hosts path.

Configure MODEM_HOST, MODEM_PORT, MODEM_USERNAME, and MODEM_PASSWORD through your process environment, then run:

```bash
python modem_runner.py
```

The runner defaults to the supplied host, port 22, and root user; the password must come from MODEM_PASSWORD. It prints a JSON WorkflowResult and exits nonzero if any step fails.

To retain your connection manager, pass its connected client directly:

```python
from modem_runner import modem_execution_worker

ssh_manager.connect()
try:
    results = modem_execution_worker(ssh_manager.get_client())
    print(results.model_dump_json(indent=2))
    for execution in results.executions:
        if not execution.success:
            for error in execution.errors:
                print(error.code, error.message)
finally:
    ssh_manager.close()
```

## Execution behavior

The five steps retain your original DATA, forward SMS, forward CALL, reverse SMS, reverse CALL order and three-second settle delay. Failure stops the workflow and records the skipped steps. No extra Python retries are performed: SMS and CALL already retry internally. Replaying after a timeout could duplicate an operation that completed remotely.

The timeout defaults to 600 seconds per command. A timeout closes the SSH channel but does not guarantee termination of the remote command. Its result is EXECUTION_UNCONFIRMED, and the sequence stops. Verify the remote process/lock state before retrying.

The runner drains stdout and stderr concurrently while waiting for EOF and exit status, preventing stderr buffer saturation from blocking the child. It validates the exact result schema rather than interpreting natural-language logs.

The original shared modem lock implementation is retained. Run sequentially; concurrent runs require a separate lock design review, especially since the original SMS/CALL cleanup can remove the shared lock before ownership is acquired.

Your command requests `Zero1,ICYMI`. The supplied DATA script has explicit auto-port mappings for VIVIFI and Zero1 and generic fallback for other operator strings; it does not provide a dedicated ICYMI identity mapping. Verify that the selected fallback port actually belongs to ICYMI before treating that operator's result as conclusive.

## Local validation

Run `python test_results.py`. Tests exercise valid success, handled failure with an original zero exit, early exit, mixed DATA results, subprocess diagnostics, redirected stderr, RING-RELEASE, missing/malformed JSON, duplicate JSON, wrong kind, and inconsistent success fields. Bash syntax checks pass for all four shell files. Physical modem behavior and real SSH have not been tested here.
