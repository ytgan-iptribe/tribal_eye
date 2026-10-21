"""Parse script results; runner streams logs and workflow emits status/results.

Each direction has a stable script section assigned by workflow_modem.
"""
import re
from .schemas import TestStatus, VoiceCallTestResult

def parse_voice_call_output(raw_result):
    out, err = str(raw_result.get('stdout') or ''), str(raw_result.get('stderr') or '')
    try:
        code = int(raw_result.get('exit_code', -1))
    except (ValueError, TypeError):
        code = -1
    fields = dict(exit_code=code, stdout=out, stderr=err)
    if code != 0:
        return VoiceCallTestResult(decision=TestStatus.FAIL, error_message=err or f'Exit {code}', **fields)
    match = re.search(r'Logged\s+to\s+DB:.*?\|\s*OUTGOING\s+CALL\s*\|\s*(?P<src>[^\s|]+)\s*->\s*(?P<dst>[^\s|]+)\s*\|\s*(?P<status>SUCCESS|RING-RELEASE|FAILED|MISSED)\s*\|\s*(?P<duration>[^|\r\n]+)', out, re.I)
    if not match:
        return VoiceCallTestResult(decision=TestStatus.UNKNOWN, error_message='Call confirmation missing.', **fields)
    status = match['status'].upper()
    passed = status in ('SUCCESS', 'RING-RELEASE')
    return VoiceCallTestResult(decision=TestStatus.PASS if passed else TestStatus.FAIL,
        sender_number=match['src'], receiver_number=match['dst'], status_text=status,
        duration_str=match['duration'].strip(), error_message=None if passed else 'Call failed.', **fields)

def test_live_call_zero1_to_vivifi(manager, runner):
    return parse_voice_call_output(runner(manager, '/usr/local/bin/callmodem2TOmodem1.sh --from Zero1 --to VIVIFI', 'Call Zero1 to VIVIFI'))

def test_live_call_vivifi_to_zero1(manager, runner):
    return parse_voice_call_output(runner(manager, '/usr/local/bin/callmodem2TOmodem1.sh --from VIVIFI --to Zero1', 'Call VIVIFI to Zero1'))
