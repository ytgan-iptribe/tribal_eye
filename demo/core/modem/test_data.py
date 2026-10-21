"""Parse script results; runner streams logs and workflow emits status/results.

Each direction has a stable script section assigned by workflow_modem.
"""
import re
from .schemas import TestStatus, CheckModemDataResult

def parse_check_modem_data_output(raw_result):
    out, err = str(raw_result.get('stdout') or ''), str(raw_result.get('stderr') or '')
    try:
        code = int(raw_result.get('exit_code', -1))
    except (ValueError, TypeError):
        code = -1
    results = re.findall(r'\[SUMMARY\].*?Result=(SUCCESS|FAILED|OK|FAIL)\b', out, re.I)
    if not results:
        results = re.findall(r'Logged\s+to\s+DB:.*?\|\s*(SUCCESS|FAILED)\s*\|', out, re.I)
    results = [x.upper() for x in results]
    decision, error = TestStatus.UNKNOWN, 'No usable confirmation in script output.'
    if code != 0 or any(x in ('FAIL', 'FAILED') for x in results):
        decision, error = TestStatus.FAIL, f'Data check failed (exit {code}).'
    elif results:
        decision, error = TestStatus.PASS, None
    if 'Another run is active (lock present)' in out + err:
        decision, error = TestStatus.FAIL, 'Another modem run is active.'
    return CheckModemDataResult(decision=decision, exit_code=code, stdout=out, stderr=err, error_message=error)

def test_live_data_check(ssh_manager, run_remote_command_fn):
    return parse_check_modem_data_output(run_remote_command_fn(ssh_manager,
        '/usr/local/bin/check_modem_data.sh --operators "Zero1,ICYMI" --verbose', 'Data check'))
