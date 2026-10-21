import codecs
import logging
import os
import sys
import time
import paramiko

from .schemas import AlertSeverity, StepExecutionGroup, TestStatus
from .telemetry import Emitter
from .test_data import test_live_data_check
from .test_sms import (
    test_live_sms_zero1_to_vivifi,
    test_live_sms_vivifi_to_zero1,
)
from .test_call import (
    test_live_call_zero1_to_vivifi,
    test_live_call_vivifi_to_zero1,
)

from core.email_status import (
    init_modem_status_db,
    update_modem_status,
)

MAX_RETRIES = 3
RETRY_DELAY_SEC = 5
MODEM_SETTLE_DELAY = 3
RETEST_DELAY_SEC = 3
COMMAND_TIMEOUT_SECONDS = 600

class ModemStatusEmitter:
    """Write modem snapshots and forward existing detailed telemetry."""

    STEP_STATUSES = {
        "data": "EXECUTING_MODEM_DATA_CHECK",
        "sms_z2v": "EXECUTING_MODEM_SMS_ZERO1_TO_VIVIFI",
        "sms_v2z": "EXECUTING_MODEM_SMS_VIVIFI_TO_ZERO1",
        "call_z2v": "EXECUTING_MODEM_CALL_ZERO1_TO_VIVIFI",
        "call_v2z": "EXECUTING_MODEM_CALL_VIVIFI_TO_ZERO1",
    }

    def __init__(self, email_id, simulation_run_id=None):
        self.email_id = email_id
        self.telemetry = Emitter(
            email_id,
            simulation_run_id,
        )

    def emit(self, event, message="", **fields):
        status = None
        error = None

        if event == "MODEM_STARTED":
            status = "EXECUTING_MODEM"

        elif event == "MODEM_COMPLETED":
            status = "COMPLETED_MODEM"

        elif event == "MODEM_FAILED":
            status = "FAILED_MODEM"
            error = message

        elif event == "STATUS":
            if message == "Setting up SSH Session":
                status = "EXECUTING_MODEM_SSH_SETUP"

            elif message == "SSH session closing":
                status = "EXECUTING_MODEM_SSH_CLOSING"

            elif message.startswith("Updating WhatsApp"):
                status = "EXECUTING_MODEM_WHATSAPP_UPDATE"

            elif message.startswith("Updating L2"):
                status = "EXECUTING_MODEM_L2_UPDATE"

            else:
                status = self.STEP_STATUSES.get(
                    fields.get("step"),
                    "EXECUTING_MODEM",
                )

        if status is not None:
            update_modem_status(
                self.email_id,
                status,
                message,
                step=fields.get("step"),
                check_number=fields.get("check_number"),
                phase=fields.get("phase"),
                error=error,
                all_checks_passed=fields.get(
                    "all_checks_passed"
                ),
            )

            logging.info(
                "[%s] %s: %s",
                self.email_id,
                status,
                message,
            )

        # STDOUT/STDERR and detailed events retain their
        # existing telemetry route. They do not change the snapshot.
        self.telemetry.emit(
            event,
            message,
            **fields,
        )

class SSHClientManager:
    def __init__(self, emitter):
        self.client = None
        self.emitter = emitter
    def connect(self):
        self.emitter.emit('STATUS', 'Setting up SSH Session', state='EXECUTING')
        if self.client:
            self.client.close()
        self.client = paramiko.SSHClient()
        self.client.load_system_host_keys()
        self.client.set_missing_host_key_policy(paramiko.RejectPolicy())
        self.client.connect(hostname=os.getenv('MODEM_SSH_HOST', '192.168.1.214'),
            port=int(os.getenv('MODEM_SSH_PORT', '22')), username=os.getenv('MODEM_SSH_USER', 'root'),
            password=os.getenv('MODEM_SSH_PASSWORD', 'Iptribe@2025') or None, timeout=15,
            banner_timeout=15, auth_timeout=15)
        self.emitter.emit('SSH_CONNECTED', 'SSH session established')
    def get_client(self):
        transport = self.client.get_transport() if self.client else None
        if not transport or not transport.is_active():
            self.connect()
        return self.client
    def close(self):
        if self.client:
            self.client.close()

def run_remote_command_single(manager, command, step_name, *, emit, attempt=1):
    out, err = [], []
    channel = None
    emit('COMMAND_STARTED', step_name, attempt=attempt, command=command)
    try:
        stdin, stdout, stderr = manager.get_client().exec_command(command)
        stdin.close()
        channel = stdout.channel
        decoders = {name: codecs.getincrementaldecoder('utf-8')('replace') for name in ('STDOUT', 'STDERR')}
        deadline = time.monotonic() + COMMAND_TIMEOUT_SECONDS
        while True:
            read_any = False
            for name, ready, recv, collected, console in (
                ('STDOUT', channel.recv_ready, channel.recv, out, sys.stdout),
                ('STDERR', channel.recv_stderr_ready, channel.recv_stderr, err, sys.stderr)):
                if ready():
                    text = decoders[name].decode(recv(4096))
                    if text:
                        collected.append(text)
                        console.write(text)
                        console.flush()
                        emit(name, text, attempt=attempt)
                    read_any = True
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f'Command exceeded {COMMAND_TIMEOUT_SECONDS}s')
            if not read_any:
                time.sleep(0.05)
        for name, collected in (('STDOUT', out), ('STDERR', err)):
            tail = decoders[name].decode(b'', final=True)
            if tail:
                collected.append(tail)
                emit(name, tail, attempt=attempt)
        code = channel.recv_exit_status()
    except Exception as exc:
        code = -1
        err.append(str(exc))
        emit('COMMAND_ERROR', str(exc), attempt=attempt)
    finally:
        if channel:
            channel.close()
    emit('COMMAND_COMPLETED', f'Exit code {code}', attempt=attempt, exit_code=code)
    return dict(step=step_name, command=command, exit_code=code, stdout=''.join(out), stderr=''.join(err))

def run_remote_command_with_retry(manager, command, step_name, *, emit,
                                  max_retries=MAX_RETRIES, delay=RETRY_DELAY_SEC):
    for attempt in range(1, max_retries + 1):
        result = run_remote_command_single(manager, command, step_name, emit=emit, attempt=attempt)
        if result['exit_code'] == 0:
            return result
        if attempt < max_retries:
            emit('RETRY_SCHEDULED', f'Retrying in {delay}s', attempt=attempt)
            time.sleep(delay)
    return result

# These remain placeholders, as in the supplied code. No external delivery occurs.
def update_result_in_whatsapp_gc(test_name, status, details):
    logging.info('[WHATSAPP PLACEHOLDER] %s: %s; %s', test_name, status, details)

def escalate_to_l2(test_name, reason, logs):
    logging.error('[L2 PLACEHOLDER] %s: %s', test_name, reason)

def modem_execution(
    severity=AlertSeverity.WARNING,
    *,
    email_id,
    simulation_run_id=None,
):
    init_modem_status_db()

    telemetry = ModemStatusEmitter(
        email_id,
        simulation_run_id,
    )

    manager = SSHClientManager(telemetry)
    group = StepExecutionGroup()

    steps = [
        (
            "data",
            "Data check",
            "step_1_data",
            test_live_data_check,
        ),
        (
            "sms_z2v",
            "SMS Zero1 to VIVIFI check",
            "step_2_sms_z2v",
            test_live_sms_zero1_to_vivifi,
        ),
        (
            "sms_v2z",
            "SMS VIVIFI to Zero1 check",
            "step_3_sms_v2z",
            test_live_sms_vivifi_to_zero1,
        ),
        (
            "call_z2v",
            "Call Zero1 to VIVIFI check",
            "step_4_call_z2v",
            test_live_call_zero1_to_vivifi,
        ),
        (
            "call_v2z",
            "Call VIVIFI to Zero1 check",
            "step_5_call_v2z",
            test_live_call_vivifi_to_zero1,
        ),
    ]

    completed = False
    failure = None

    telemetry.emit(
        "MODEM_STARTED",
        "Modem workflow started",
        state="EXECUTING",
    )

    try:
        manager.connect()

        for index, (step, title, field, check) in enumerate(steps):
            results = getattr(group, field)

            for check_number in (1, 2):
                phase = (
                    "initial"
                    if check_number == 1
                    else "retest"
                )

                def emit(event, message="", **fields):
                    telemetry.emit(
                        event,
                        message,
                        step=step,
                        phase=phase,
                        check_number=check_number,
                        **fields,
                    )

                ordinal = (
                    "1st"
                    if check_number == 1
                    else "2nd"
                )

                emit(
                    "STATUS",
                    f"Running {ordinal} {title}",
                    state="EXECUTING",
                )

                def runner(manager, command, name):
                    return run_remote_command_with_retry(
                        manager,
                        command,
                        name,
                        emit=emit,
                    )

                result = check(manager, runner)
                results.append(result)

                emit(
                    "STEP_RESULT",
                    result.error_message or result.decision.value,
                    decision=result.decision.value,
                    result=result.model_dump(mode="json"),
                )

                if check_number == 1:
                    emit(
                        "STATUS",
                        f"Waiting {RETEST_DELAY_SEC}s "
                        "before second check",
                        state="EXECUTING",
                    )

                    time.sleep(RETEST_DELAY_SEC)

            if severity == AlertSeverity.WARNING:
                telemetry.emit(
                    "STATUS",
                    "Updating WhatsApp (placeholder)",
                    step=step,
                    state="EXECUTING",
                )

                update_result_in_whatsapp_gc(
                    title,
                    results[0].decision.value,
                    (
                        f"Initial={results[0].decision.value}; "
                        f"retest={results[1].decision.value}"
                    ),
                )

                telemetry.emit(
                    "NOTIFICATION_PLACEHOLDER",
                    "WhatsApp handler logged locally; "
                    "no message sent",
                    step=step,
                )

                if all(
                    result.decision != TestStatus.PASS
                    for result in results
                ):
                    telemetry.emit(
                        "STATUS",
                        "Updating L2 (placeholder)",
                        step=step,
                        state="EXECUTING",
                    )

                    escalate_to_l2(
                        title,
                        "Both checks did not pass",
                        {
                            "runs": [
                                result.model_dump(mode="json")
                                for result in results
                            ]
                        },
                    )

                    telemetry.emit(
                        "NOTIFICATION_PLACEHOLDER",
                        "L2 handler logged locally; "
                        "no escalation sent",
                        step=step,
                    )

                else:
                    telemetry.emit(
                        "L2_SKIPPED",
                        "L2 escalation condition not met",
                        step=step,
                    )

            else:
                telemetry.emit(
                    "NOTIFICATION_SKIPPED",
                    "Supplied notification policy applies "
                    "only to WARNING severity",
                    step=step,
                )

            if index < len(steps) - 1:
                telemetry.emit(
                    "STATUS",
                    f"Waiting {MODEM_SETTLE_DELAY}s "
                    "before next script",
                    step=step,
                    state="EXECUTING",
                )

                time.sleep(MODEM_SETTLE_DELAY)

        completed = True

    except KeyboardInterrupt:
        failure = "Modem workflow interrupted"
        raise

    except Exception as exc:
        failure = str(exc)
        raise

    finally:
        # Always attempt SSH cleanup, even if status delivery fails.
        try:
            telemetry.emit(
                "STATUS",
                "SSH session closing",
                state="EXECUTING",
            )
        except Exception:
            logging.exception(
                "Unable to record SSH closing status"
            )

        try:
            manager.close()

        except Exception as exc:
            completed = False
            close_error = f"SSH close failed: {exc}"

            failure = (
                f"{failure}; {close_error}"
                if failure
                else close_error
            )

        else:
            try:
                telemetry.emit(
                    "SSH_CLOSED",
                    "SSH session closed",
                )
            except Exception:
                logging.exception(
                    "Unable to emit SSH_CLOSED"
                )

        if failure:
            telemetry.emit(
                "MODEM_FAILED",
                failure,
                state="FAILED",
            )

        elif completed:
            all_checks_passed = all(
                result.decision == TestStatus.PASS
                for _, _, field, _ in steps
                for result in getattr(group, field)
            )

            telemetry.emit(
                "MODEM_COMPLETED",
                "Complete",
                state="COMPLETED",
                all_checks_passed=all_checks_passed,
            )

    if failure:
        raise RuntimeError(failure)

    return group
