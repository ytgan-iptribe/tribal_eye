"""Parse script results; runner streams logs and workflow emits status/results.

Each direction has a stable script section assigned by workflow_modem.
"""
import re
from .schemas import TestStatus, SMSModemTestResult

def parse_sms_modem_output(raw_result):
    out, err = str(raw_result.get('stdout') or ''), str(raw_result.get('stderr') or '')
    try:
        code = int(raw_result.get('exit_code', -1))
    except (ValueError, TypeError):
        code = -1
    fields = dict(exit_code=code, stdout=out, stderr=err)
    if code != 0:
        return SMSModemTestResult(decision=TestStatus.FAIL, error_message=err or f'Exit {code}', **fields)
    match = re.search(r'Logged\s+to\s+DB:.*?\|\s*OUTGOING\s+SMS\s*\|\s*(?P<src>[^\s|]+)\s*->\s*(?P<dst>[^\s|]+)\s*\|\s*(?P<status>SUCCESS|FAILED)\s*\|', out, re.I)
    if not match:
        return SMSModemTestResult(decision=TestStatus.UNKNOWN, error_message='SMS confirmation missing.', **fields)
    status = match['status'].upper()
    return SMSModemTestResult(decision=TestStatus.PASS if status == 'SUCCESS' else TestStatus.FAIL,
        sender_number=match['src'], receiver_number=match['dst'], status_text=status,
        error_message=None if status == 'SUCCESS' else 'SMS delivery failed.', **fields)

def test_live_sms_zero1_to_vivifi(manager, runner):
    return parse_sms_modem_output(runner(manager, '/usr/local/bin/smsmodem2TOmodem1.sh --from Zero1 --to VIVIFI --message "Automated Ping Zero1 to VIVIFI"', 'SMS Zero1 to VIVIFI'))

def test_live_sms_vivifi_to_zero1(manager, runner):
    return parse_sms_modem_output(runner(manager, '/usr/local/bin/smsmodem2TOmodem1.sh --from VIVIFI --to Zero1 --message "Automated Ping VIVIFI to Zero1"', 'SMS VIVIFI to Zero1'))
