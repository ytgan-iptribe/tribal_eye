from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field

class TestStatus(str, Enum):
    PASS='PASS'
    FAIL='FAIL'
    UNKNOWN='UNKNOWN'
class AlertSeverity(str, Enum):
    WARNING='WARNING'
    MAJOR='MAJOR'
    CRITICAL='CRITICAL'
class CheckModemDataResult(BaseModel):
    decision: TestStatus
    exit_code: int
    stdout: str
    stderr: str
    error_message: Optional[str]=None
class SMSModemTestResult(CheckModemDataResult):
    sender_number: Optional[str]=None
    receiver_number: Optional[str]=None
    status_text: Optional[str]=None
class VoiceCallTestResult(SMSModemTestResult):
    duration_str: Optional[str]=None
class StepExecutionGroup(BaseModel):
    step_1_data: list[CheckModemDataResult]=Field(default_factory=list)
    step_2_sms_z2v: list[SMSModemTestResult]=Field(default_factory=list)
    step_3_sms_v2z: list[SMSModemTestResult]=Field(default_factory=list)
    step_4_call_z2v: list[VoiceCallTestResult]=Field(default_factory=list)
    step_5_call_v2z: list[VoiceCallTestResult]=Field(default_factory=list)

from datetime import datetime
from typing import Literal
from pydantic import ConfigDict, field_validator, model_validator

class ModemTelemetryEvent(BaseModel):
    """Transport schema shared by worker and API; extra structured results retained."""
    model_config = ConfigDict(extra='allow')
    event_id: str = Field(min_length=1)
    email_id: str = Field(min_length=1)
    execution_id: str = Field(min_length=1)
    simulation_run_id: Optional[str] = None
    sequence: int = Field(ge=1)
    timestamp: datetime
    event: str = Field(min_length=1)
    message: str = ''
    kind: Literal['status', 'log']
    step: Optional[Literal['data','sms_z2v','sms_v2z','call_z2v','call_v2z']] = None
    phase: Optional[Literal['initial','retest']] = None
    check_number: Optional[Literal[1,2]] = None
    attempt: Optional[int] = Field(default=None, ge=1)
    stream: Optional[Literal['stdout','stderr']] = None
    state: Optional[Literal['QUEUED','EXECUTING','COMPLETED','FAILED']] = None
    current_status: bool = False

    @field_validator('timestamp')
    @classmethod
    def aware_timestamp(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('timestamp must include timezone')
        return value

    @model_validator(mode='after')
    def consistent_kind(self):
        expected = 'log' if self.event in ('STDOUT','STDERR') else 'status'
        if self.kind != expected:
            raise ValueError('kind does not match event')
        if self.kind == 'log':
            if self.stream != self.event.lower() or self.step is None:
                raise ValueError('log requires matching stream and script step')
            if self.current_status:
                raise ValueError('log cannot update current status')
        elif self.stream is not None:
            raise ValueError('status cannot have stream')
        if self.current_status and self.state is None:
            raise ValueError('current status requires state')
        return self
