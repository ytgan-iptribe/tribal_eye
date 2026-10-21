"""Pydantic v2 structured modem verification. No automatic replay of remote commands."""
from __future__ import annotations

import os
import shlex
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Kind = Literal['DATA', 'SMS', 'CALL']
PREFIX = 'MODEM_RESULT_JSON='


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class TestError(StrictModel):
    code: str
    message: str


class CheckResult(StrictModel):
    subject: str  # DATA operator or SMS/CALL direction
    status: str
    success: bool
    errors: list[TestError] = Field(default_factory=list)
    details: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode='after')
    def consistent(self):
        if self.success != (self.status == 'SUCCESS'):
            raise ValueError('success must match status == SUCCESS')
        if self.success and self.errors or not self.success and not self.errors:
            raise ValueError('failed checks require errors; successful checks cannot have errors')
        return self


class ScriptResult(StrictModel):
    schema_version: Literal[1]
    kind: Kind
    success: bool
    checks: list[CheckResult]
    errors: list[TestError] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def consistent(self):
        expected = bool(self.checks) and all(c.success for c in self.checks) and not self.errors
        if self.success != expected:
            raise ValueError('overall success inconsistent with checks/errors')
        if not self.checks and not self.errors:
            raise ValueError('missing checks require a script error')
        return self


class ExecutionResult(StrictModel):
    step: str
    kind: Kind
    success: bool
    exit_code: int | None  # None means completion could not be established
    result: ScriptResult | None = None
    errors: list[TestError] = Field(default_factory=list)
    stdout: str = ''
    stderr: str = ''


class WorkflowResult(StrictModel):
    success: bool
    executions: list[ExecutionResult]
    skipped_steps: list[str]


def parse_execution(step: str, kind: Kind, exit_code: int, stdout: str, stderr: str) -> ExecutionResult:
    errors: list[TestError] = []
    result = None
    lines = [line[len(PREFIX):] for line in stdout.splitlines() if line.startswith(PREFIX)]
    try:
        if len(lines) != 1:
            raise ValueError(f'Expected exactly one JSON result, received {len(lines)}')
        result = ScriptResult.model_validate_json(lines[0])
        if result.kind != kind:
            raise ValueError(f'Expected kind {kind}, received {result.kind}')
    except ValueError as exc:
        errors.append(TestError(code='INVALID_RESULT', message=str(exc)))
    if exit_code != 0:
        errors.append(TestError(code='REMOTE_EXIT', message=f'Remote process exited with code {exit_code}'))
    if result:
        errors.extend(result.errors)
        for check in result.checks:
            errors.extend(TestError(code=e.code, message=f'{check.subject}: {e.message}') for e in check.errors)
    return ExecutionResult(step=step, kind=kind, success=not errors and result is not None and result.success,
                           exit_code=exit_code, result=result, errors=errors, stdout=stdout, stderr=stderr)


def run_remote_command_single(ssh_client, command: str, step_name: str, kind: Kind,
                              timeout_sec: float = 600) -> ExecutionResult:
    """Drain both streams while running; require EOF and a remote exit status.

    A timeout/channel failure does not prove that the remote operation stopped.
    Return an indeterminate result and never automatically replay an SMS/call.
    """
    out, err = bytearray(), bytearray()
    channel = None
    try:
        transport = ssh_client.get_transport()
        if transport is None or not transport.is_active():
            raise RuntimeError('SSH transport is not active')
        channel = transport.open_session(timeout=15)
        channel.settimeout(15)
        channel.exec_command(command)
        channel.shutdown_write()
        deadline = time.monotonic() + timeout_sec
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError(f'No confirmed completion within {timeout_sec}s; remote command may still be running')
            # One chunk per stream per pass prevents a busy stream starving the other.
            if channel.recv_ready():
                out.extend(channel.recv(65536))
            if channel.recv_stderr_ready():
                err.extend(channel.recv_stderr(65536))
            drained = not channel.recv_ready() and not channel.recv_stderr_ready()
            if (channel.eof_received or channel.closed) and drained:
                if not channel.exit_status_ready():
                    raise RuntimeError('SSH channel closed without a remote exit status')
                rc = channel.recv_exit_status()
                if rc == -1:
                    raise RuntimeError('Server did not supply a remote exit status')
                return parse_execution(step_name, kind, rc, out.decode('utf-8', 'replace'), err.decode('utf-8', 'replace'))
            if not transport.is_active():
                raise RuntimeError('SSH disconnected; remote completion is unknown')
            time.sleep(0.05)
    except Exception as exc:
        return ExecutionResult(step=step_name, kind=kind, success=False, exit_code=None,
                               errors=[TestError(code='EXECUTION_UNCONFIRMED', message=str(exc))],
                               stdout=out.decode('utf-8', 'replace'), stderr=err.decode('utf-8', 'replace'))
    finally:
        if channel:
            channel.close()


def modem_execution_worker(ssh_client, settle_delay: float = 3) -> WorkflowResult:
    steps = [
        ('1. Data Connectivity Check (Zero1 & ICYMI)', 'DATA',
         ['/usr/local/bin/check_modem_data.sh', '--operators', 'Zero1,ICYMI', '--verbose']),
        ('2. SMS Test (Zero1 -> VIVIFI)', 'SMS',
         ['/usr/local/bin/smsmodem2TOmodem1.sh', '--from', 'Zero1', '--to', 'VIVIFI', '--message', 'Automated Ping Zero1 to VIVIFI']),
        ('3. Call Test (Zero1 -> VIVIFI)', 'CALL',
         ['/usr/local/bin/callmodem2TOmodem1.sh', '--from', 'Zero1', '--to', 'VIVIFI']),
        ('4. SMS Test (VIVIFI -> Zero1)', 'SMS',
         ['/usr/local/bin/smsmodem2TOmodem1.sh', '--from', 'VIVIFI', '--to', 'Zero1', '--message', 'Automated Ping VIVIFI to Zero1']),
        ('5. Call Test (VIVIFI -> Zero1)', 'CALL',
         ['/usr/local/bin/callmodem2TOmodem1.sh', '--from', 'VIVIFI', '--to', 'Zero1']),
    ]
    executions = []
    for i, (name, kind, args) in enumerate(steps):
        execution = run_remote_command_single(ssh_client, shlex.join(args), name, kind)
        executions.append(execution)
        if not execution.success:
            return WorkflowResult(success=False, executions=executions, skipped_steps=[s[0] for s in steps[i+1:]])
        if i < len(steps) - 1:
            time.sleep(settle_delay)
    return WorkflowResult(success=True, executions=executions, skipped_steps=[])


def main():
    import paramiko
    # Provision this server's verified key in known_hosts before running.
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    if path := os.environ.get('MODEM_KNOWN_HOSTS'):
        client.load_host_keys(path)
    try:
        client.connect(hostname=os.environ.get('MODEM_HOST', '192.168.1.214'),
                       port=int(os.environ.get('MODEM_PORT', '22')),
                       username=os.environ.get('MODEM_USERNAME', 'root'),
                       password=os.environ['MODEM_PASSWORD'],
                       timeout=15, auth_timeout=15, banner_timeout=15,
                       look_for_keys=False, allow_agent=False)
        client.get_transport().set_keepalive(30)
        workflow = modem_execution_worker(client)
        print(workflow.model_dump_json(indent=2))
        return 0 if workflow.success else 1
    except Exception as exc:
        failure = ExecutionResult(step='SSH connection', kind='DATA', success=False, exit_code=None,
                                  errors=[TestError(code='SSH_CONNECTION', message=str(exc))])
        print(WorkflowResult(success=False, executions=[failure], skipped_steps=['All modem tests']).model_dump_json(indent=2))
        return 1
    finally:
        client.close()


if __name__ == '__main__':
    raise SystemExit(main())
