from typing import List, Optional
from pydantic import BaseModel, Field
from enum import Enum
from datetime import datetime, timezone



class VerificationObservation(BaseModel):
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    event: str
    message: str


class VerificationObservationLog(BaseModel):
    observations: list[VerificationObservation] = Field(default_factory=list)

    def append(self, event: str, message: str) -> VerificationObservation:
        observation = VerificationObservation(event=event, message=message)
        self.observations.append(observation)
        return observation


class TrendStatus(str, Enum):
    UNKNOWN = 'UNKNOWN'
    ESCALATING = 'ESCALATING'
    SUSTAINED = 'SUSTAINED'
    RESOLVING = 'RESOLVING'

class EmailContext(BaseModel):
    sender: str
    timestamp: datetime
    subject: str
    body: str
    error_code: str = "5012"
    initial_count: float = Field(default=0.0, description="Sum of count reported in alert email")


class MetricRow(BaseModel):
    timestamp: datetime
    error_code: str
    count: float


class TrendEvaluation(BaseModel):
    status: TrendStatus
    current_count: float | None = None
    peak_count: float | None = None
    baseline_count: float | None = None
    trend_description: str


class VerificationWorkflowResult(BaseModel):
    workflow_id: str
    workflow_status: str
    latest_evaluation: TrendEvaluation | None
    observation_log: VerificationObservationLog
    observation_log_path: str