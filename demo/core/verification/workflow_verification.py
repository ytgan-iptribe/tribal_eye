
from __future__ import annotations

import csv
import io
import json
import math
import os
import platform
import subprocess
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo
import ipaddress
import json
import socket
import subprocess
import shutil
import tempfile
import signal
from datetime import timezone

from urllib.parse import urlparse

from core.verification.schemas import *

import math
from datetime import datetime, timedelta, timezone

from .streaming import execute_powershell_fetch as streaming_fetch
from .streaming import run_monitoring_loop as streaming_loop



# Credentials
USERNAME = "haziqah"
PASSWORD = "Iptribe@1234"
CRED_FILE = Path.home() / ".opensearch" / "cred.xml"

# Destination Directory matching setup script logic
CRED_DIR = Path.home() / ".opensearch"

VPN_PROFILE = Path(
    "/root/projects/alert_results/demo/core/verification/opensearch"
)

OPENSEARCH_URL = (
    "https://opensearch-singtel-prod-3e7eaec0fb34e638"
    ".elb.ap-southeast-1.amazonaws.com"
)



def setup_opensearch_credentials(
    username: str,
    password: str,
    output_path: Path,
) -> bool:
    """
    Reuse matching PSCredential XML; otherwise create or replace it.
    Password is passed through stdin and never printed.
    """
    ps_executable = (
        "powershell.exe" if platform.system() == "Windows" else "pwsh"
    )

    try:
        if not username or not password:
            print("[Error] Username and password must be provided.")
            return False

        output_path = Path(output_path).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)

        process_env = os.environ.copy()
        process_env["OPENSEARCH_SETUP_USERNAME"] = username
        process_env["OPENSEARCH_SETUP_CRED_PATH"] = str(output_path)

        ps_script = r"""
$ErrorActionPreference = "Stop"
$tempPath = $null

try {
    $username = $env:OPENSEARCH_SETUP_USERNAME
    $outputPath = $env:OPENSEARCH_SETUP_CRED_PATH
    $plainPassword = [Console]::In.ReadToEnd()

    if ([string]::IsNullOrEmpty($plainPassword)) {
        throw "No password supplied."
    }

    $matches = $false

    if (Test-Path -LiteralPath $outputPath -PathType Leaf) {
        try {
            $existing = Import-Clixml -LiteralPath $outputPath

            if ($existing -is [System.Management.Automation.PSCredential]) {
                $existingPassword = $existing.GetNetworkCredential().Password

                $matches = (
                    [string]::Equals(
                        $existing.UserName,
                        $username,
                        [StringComparison]::Ordinal
                    ) -and
                    [string]::Equals(
                        $existingPassword,
                        $plainPassword,
                        [StringComparison]::Ordinal
                    )
                )
            }
        }
        catch {
            # Unreadable or invalid credentials will be replaced.
            $matches = $false
        }
    }

    if ($matches) {
        Write-Output "Existing credentials match; reusing credential file."
    }
    else {
        $securePassword = ConvertTo-SecureString `
            -String $plainPassword -AsPlainText -Force

        $credential = [System.Management.Automation.PSCredential]::new(
            $username,
            $securePassword
        )

        # Validate a temporary export before replacing the existing file.
        $tempPath = "$outputPath.$([Guid]::NewGuid().ToString('N')).tmp"

        $credential | Export-Clixml -LiteralPath $tempPath

        $verified = Import-Clixml -LiteralPath $tempPath

        if ($verified -isnot [System.Management.Automation.PSCredential]) {
            throw "Exported file does not contain a PSCredential."
        }

        if (
            -not [string]::Equals(
                $verified.UserName,
                $username,
                [StringComparison]::Ordinal
            ) -or
            -not [string]::Equals(
                $verified.GetNetworkCredential().Password,
                $plainPassword,
                [StringComparison]::Ordinal
            )
        ) {
            throw "Exported credentials did not match supplied credentials."
        }

        Move-Item -LiteralPath $tempPath -Destination $outputPath -Force
        $tempPath = $null

        Write-Output "Credential file created or updated successfully."
    }
}
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
finally {
    if ($tempPath -and (Test-Path -LiteralPath $tempPath)) {
        Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
    }
}
"""

        result = subprocess.run(
            [
                ps_executable,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                ps_script,
            ],
            input=password,
            capture_output=True,
            text=True,
            env=process_env,
            timeout=60,
        )

        if result.returncode != 0:
            print(
                "[Error] Credential setup failed: "
                f"{result.stderr.strip() or result.stdout.strip()}",
                flush=True,
            )
            return False

        if not output_path.is_file():
            print(f"[Error] Credential file not found: {output_path}")
            return False

        if platform.system() != "Windows":
            output_path.chmod(0o600)

        print(f"[Success] {result.stdout.strip()}", flush=True)
        return True

    except FileNotFoundError:
        print(f"[Error] '{ps_executable}' was not found in PATH.")
        return False

    except subprocess.TimeoutExpired:
        print("[Error] Credential setup timed out after 60 seconds.")
        return False

    except Exception as exc:
        print(f"[Error] Credential setup failed: {exc}")
        return False



def disconnect_vpn(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return

    print("[VPN] Disconnecting...", flush=True)

    process.send_signal(signal.SIGINT)

    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def connect_vpn(
    profile_path: Path,
    timeout_seconds: int = 90,
    auth_file: Path | None = None,
) -> subprocess.Popen:
    """
    Connect using a non-SAML OpenVPN profile.

    For VPN username/password authentication, auth_file must contain:
        line 1: VPN username
        line 2: VPN password

    These are VPN credentials, which may differ from OpenSearch credentials.
    """
    executable = shutil.which("openvpn")

    if executable is None:
        raise RuntimeError(
            "OpenVPN is not installed. Run: sudo apt install openvpn"
        )

    if os.geteuid() != 0:
        raise RuntimeError(
            "Run this program with root privileges to establish the VPN."
        )

    profile_path = Path(profile_path).expanduser().resolve()

    if profile_path.is_dir():
        profiles = sorted(profile_path.glob("*.ovpn"))

        if len(profiles) != 1:
            raise RuntimeError(
                f"Expected one .ovpn file in '{profile_path}', "
                f"found {len(profiles)}. Specify the exact profile file."
            )

        profile_path = profiles[0]

    if not profile_path.is_file():
        raise FileNotFoundError(f"VPN profile not found: {profile_path}")

    profile_text = profile_path.read_text(encoding="utf-8-sig")

    if "auth-federate" in profile_text:
        raise RuntimeError(
            "This profile uses AWS SAML/SSO authentication. "
            "Standard OpenVPN cannot connect; use the AWS VPN Client."
        )

    command = [
        executable,
        "--config", str(profile_path),
        "--verb", "3",
    ]

    if auth_file is not None:
        auth_file = Path(auth_file).expanduser().resolve()

        if not auth_file.is_file():
            raise FileNotFoundError(
                f"VPN authentication file not found: {auth_file}"
            )

        command.extend(["--auth-user-pass", str(auth_file)])

    print(f"[VPN] Connecting using '{profile_path}'...", flush=True)

    # A temporary file avoids blocking reads and stdout pipe congestion.
    with tempfile.TemporaryFile(mode="w+b") as log_file:
        process = subprocess.Popen(
            command,
            cwd=str(profile_path.parent),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )

        deadline = time.monotonic() + timeout_seconds
        read_position = 0
        captured_output = ""

        try:
            while time.monotonic() < deadline:
                # Read without moving the file offset used by OpenVPN.
                chunk = os.pread(
                    log_file.fileno(),
                    65536,
                    read_position,
                )

                if chunk:
                    read_position += len(chunk)
                    text = chunk.decode("utf-8", errors="replace")
                    captured_output += text
                    print(text, end="", flush=True)

                if process.poll() is not None:
                    raise RuntimeError(
                        f"OpenVPN exited with code {process.returncode}.\n"
                        f"{captured_output[-4000:]}"
                    )

                if "Initialization Sequence Completed" in captured_output:
                    print("[VPN] Connected successfully.", flush=True)
                    atexit.register(disconnect_vpn, process)
                    return process

                if "AUTH_FAILED" in captured_output:
                    raise RuntimeError("VPN authentication failed.")

                time.sleep(0.2)

            raise TimeoutError(
                f"VPN connection did not complete within "
                f"{timeout_seconds} seconds.\n"
                f"{captured_output[-4000:]}"
            )

        except BaseException:
            disconnect_vpn(process)
            raise


def vpn_already_connected(
    endpoint_url: str,
    timeout_seconds: float = 3,
) -> bool:
    """
    Check whether the endpoint is reachable through a tun/tap interface.
    This checks usable VPN connectivity, not the identity of its profile.
    """
    parsed = urlparse(endpoint_url)
    hostname = parsed.hostname
    port = parsed.port or 443

    if not hostname:
        return False

    try:
        addresses = {
            result[4][0]
            for result in socket.getaddrinfo(
                hostname,
                port,
                family=socket.AF_INET,
                type=socket.SOCK_STREAM,
            )
        }
    except OSError:
        return False

    for address in addresses:
        try:
            # Your OpenSearch endpoint resolves to private addresses.
            if not ipaddress.ip_address(address).is_private:
                continue

            result = subprocess.run(
                ["ip", "-j", "route", "get", address],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )

            if result.returncode != 0:
                continue

            routes = json.loads(result.stdout)

            if not any(
                route.get("dev", "").startswith(("tun", "tap"))
                for route in routes
            ):
                continue

            # TCP reachability avoids the known TLS hostname mismatch.
            with socket.create_connection(
                (address, port),
                timeout=timeout_seconds,
            ):
                return True

        except (
            OSError,
            ValueError,
            subprocess.TimeoutExpired,
        ):
            continue

    return False


class OpenSearchMonitor:
    
    # Provisional thresholds; confirm with stakeholders before production use.
    trend_window_minutes = 5
    trend_minimum_buckets = 3
    trend_change_threshold_percent = 20.0
    
    def __init__(
        self, email_context: EmailContext,
        script_path: str = '/root/projects/alert_results/demo/core/verification/Fetch-DashboardMetrics.ps1',
        output_dir: str = '/root/projects/alert_results/demo/core/verification/OpenSearch data',
        monitoring_duration_minutes: float = 15,
        poll_interval_seconds: float = 30,
        csv_timezone: str = 'Asia/Singapore',
        fetch_timeout_seconds: float = 60,
        observation_log_path: str | None = None,
    ):
        configuration_warnings = []
        def positive(value, default, name):
            try:
                value = float(value)
                if math.isfinite(value) and value > 0:
                    return value
            except (TypeError, ValueError, OverflowError):
                pass
            configuration_warnings.append(f'{name} invalid; using {default}.')
            return default
        monitoring_duration_minutes = positive(monitoring_duration_minutes, 15, 'duration')
        poll_interval_seconds = positive(poll_interval_seconds, 30, 'poll interval')
        fetch_timeout_seconds = positive(fetch_timeout_seconds, 60, 'fetch timeout')
        self.email_context = email_context
        self.script_path = str(script_path)
        self.output_dir = str(output_dir)
        try:
            self.csv_zone = ZoneInfo(csv_timezone)
        except Exception as exc:
            self.csv_zone = ZoneInfo('Asia/Singapore')
            configuration_warnings.append(f'Invalid CSV timezone; using Asia/Singapore: {exc}')
        try:
            Path(self.output_dir).mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            configuration_warnings.append(f'Output directory creation failed: {exc}')
        self.monitoring_duration = timedelta(minutes=monitoring_duration_minutes)
        self.poll_interval = poll_interval_seconds
        self.fetch_timeout_seconds = fetch_timeout_seconds
        self.start_time = datetime.now(timezone.utc)
        self.target_csv_path: Optional[str] = None
        self.current_status = TrendStatus.UNKNOWN
        self.parsed_metrics: list[MetricRow] = []
        self.latest_evaluation: TrendEvaluation | None = None
        self.observation_log = VerificationObservationLog()
        self._observation_states: dict[str, Any] = {}
        self._observed_metrics: dict[str, float] = {}
        self._expected_csv_path: str | None = None
        self._metric_scope: str | None = None
        self._deadline: float | None = None
        self.is_monitoring = False
        self.workflow_id = uuid4().hex
        self.observation_log_path = Path(observation_log_path) if observation_log_path else (
            Path(self.output_dir) / 'verification_logs' / f'{self.workflow_id}.jsonl'
        )
        self._persistence_error = None
        self._last_result = None
        for warning in configuration_warnings:
            self.log_observation('CONFIGURATION_WARNING', warning)
    
    def log_observation(self, event: str, message: str) -> None:
        observation = VerificationObservation(event=event, message=message)
        self.observation_log.observations.append(observation)
        try:
            self.observation_log_path.parent.mkdir(parents=True, exist_ok=True)
            # Rewrite the full in-memory history atomically; recovers missed writes.
            temporary = self.observation_log_path.with_name(self.observation_log_path.name + '.tmp')
            with temporary.open('w', encoding='utf-8') as handle:
                for entry in self.observation_log.observations:
                    handle.write(entry.model_dump_json() + '\n')
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.observation_log_path)
            if self._persistence_error is not None:
                self._persistence_error = None
                self.log_observation('LOG_WRITE_RECOVERED', 'Observation history persisted again.')
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'
            if self._persistence_error != error:
                self._persistence_error = error
                self.observation_log.observations.append(VerificationObservation(
                    event='LOG_WRITE_FAILED',
                    message=f'Keeping observations in memory: {error}',
                ))
        try:
            print(f'[{observation.timestamp.isoformat()}] {event}: {message}', flush=True)
        except Exception:
            pass
    
    def log_state_change(self, key: str, state: Any, event: str, message: str) -> None:
        if key not in self._observation_states or self._observation_states[key] != state:
            self.log_observation(event, message)
            self._observation_states[key] = state
    
    def log_metric_changes(self, metrics: list[MetricRow]) -> None:
        current_metrics = {
            f'{metric.timestamp.isoformat()}|{metric.error_code}': float(metric.count)
            for metric in metrics
        }
        for key, count in current_metrics.items():
            if key not in self._observed_metrics:
                self.log_observation('METRIC_ADDED', f'Bucket {key}; count={count}.')
            elif self._observed_metrics[key] != count:
                self.log_observation('METRIC_UPDATED', f'Bucket {key}: {self._observed_metrics[key]} -> {count}.')
        for key in sorted(self._observed_metrics.keys() - current_metrics.keys()):
            self.log_observation('METRIC_REMOVED', f'Bucket {key} absent from successfully parsed snapshot.')
        self._observed_metrics = current_metrics
    
    @staticmethod
    def _text(value) -> str:
        return value.decode(errors='replace') if isinstance(value, bytes) else (value or '')
    
    # def execute_powershell_fetch(self) -> bool:
    #     executable = 'powershell.exe' if platform.system() == 'Windows' else 'pwsh'
    #     timeout = self.fetch_timeout_seconds
    #     if self._deadline is not None:
    #         timeout = min(timeout, max(0.001, self._deadline - time.monotonic()))
    #     cmd = [executable, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
    #            '-File', self.script_path, '-MaxWindowMinutes', '5', '-MaxChunksPerRun', '1',
    #            '-OutputRoot', self.output_dir]
    #     # Catch subprocess errors only; logging failures propagate to lifecycle handling.
    #     try:
    #         result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    #     except subprocess.TimeoutExpired as exc:
    #         self.log_state_change('fetch', ('TIMEOUT', self.fetch_timeout_seconds), 'FETCH_TIMEOUT',
    #                               f'Fetch exceeded allowed timeout ({timeout:.1f}s). stderr: {self._text(exc.stderr).strip()}')
    #         return False
    #     except Exception as exc:
    #         error = f'{type(exc).__name__}: {exc}'
    #         self.log_state_change('fetch', ('FAILED', error), 'FETCH_FAILED', error)
    #         return False
    #     if result.stdout.strip():
    #         print(result.stdout.rstrip(), flush=True)
    #     if result.returncode != 0:
    #         details = result.stderr.strip() or result.stdout.strip() or 'No error output.'
    #         self.log_state_change('fetch', ('FAILED', result.returncode, details), 'FETCH_FAILED',
    #                               f'Exit code {result.returncode}; {details}')
    #         return False
    #     self.log_state_change('fetch', ('SUCCEEDED',), 'FETCH_SUCCEEDED',
    #                           'PowerShell exited with code 0; this does not independently confirm metric query success.')
    #     self.log_state_change('fetch_stderr', result.stderr.strip(), 'FETCH_WARNING' if result.stderr.strip() else 'FETCH_WARNING_CLEARED',
    #                           result.stderr.strip() or 'No PowerShell stderr warnings.')
    #     return True
        
    def execute_powershell_fetch(self):
        return streaming_fetch(self)

    def locate_target_csv(self) -> Optional[str]:
        try:
            return self._locate_target_csv()
        except Exception as exc:
            self.target_csv_path = None
            self.parsed_metrics = []
            self.log_state_change('csv_lookup', str(exc), 'CSV_LOOKUP_FAILED', str(exc))
            return None
    
    def _locate_target_csv(self) -> Optional[str]:
        date_str = datetime.now(self.csv_zone).strftime('%Y-%m-%d')
        expected = str(Path(self.output_dir) / 'Gy - Error Code Wise' / f'Gy - Error Code Wise_{date_str}.csv')
        if self._expected_csv_path is not None and expected != self._expected_csv_path:
            self.log_observation('CSV_PATH_CHANGED', f'{self._expected_csv_path} -> {expected}.')
            self._observed_metrics = {}
            self.parsed_metrics = []
            self._metric_scope = None
        self._expected_csv_path = expected
        exists = Path(expected).is_file()
        self.log_state_change('csv', (expected, exists), 'CSV_FOUND' if exists else 'CSV_MISSING',
                              f'Current-day CSV {"found" if exists else "missing"}: {expected}.')
        self.target_csv_path = expected if exists else None
        if self.target_csv_path is None:
            self.parsed_metrics = []
        return self.target_csv_path
    
    @staticmethod
    def _normalize_code(value: str) -> str:
        try:
            number = Decimal(value.strip())
            if number.is_finite() and number == number.to_integral_value():
                return str(int(number))
        except InvalidOperation:
            pass
        return value.strip()
    
    def _parse_failure(self, message: str) -> list[MetricRow]:
        self.parsed_metrics = []
        self.log_state_change('parse', ('FAILED', self.target_csv_path, message), 'PARSE_FAILED', message)
        return []
    
    def parse_csv_data(self) -> list[MetricRow]:
        # Clear current evidence on failure, but retain successful-snapshot delta history.
        self.parsed_metrics = []
        try:
            if not self.target_csv_path:
                return self._parse_failure('No current-day CSV selected.')
            path = Path(self.target_csv_path)
            before = path.stat()
            content = path.read_text(encoding='utf-8-sig')
            after = path.stat()
            if (before.st_mtime_ns, before.st_size, before.st_ino) != (after.st_mtime_ns, after.st_size, after.st_ino):
                return self._parse_failure('CSV changed during read; will retry next poll.')
            reader = csv.DictReader(io.StringIO(content, newline=''), strict=True)
            columns = {name.strip(): name for name in (reader.fieldnames or [])}
            time_cols = [c for c in columns if 'Timestamp' in c]
            code_cols = [c for c in columns if 'error_code' in c]
            value_cols = [c for c in columns if c.startswith('Sum')]
            if not all(len(c) == 1 for c in (time_cols, code_cols, value_cols)):
                return self._parse_failure(f'Expected one Timestamp, error_code, and Sum column; got {list(columns)}.')
            time_col, code_col, value_col = (columns[c[0]] for c in (time_cols, code_cols, value_cols))
            target = self._normalize_code(str(self.email_context.error_code))
            unique: dict[datetime, MetricRow] = {}
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    return self._parse_failure('Incomplete or malformed CSV row.')
                if self._normalize_code(row[code_col]) != target:
                    continue
                stamp = datetime.fromisoformat(row[time_col].strip().replace('Z', '+00:00'))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=self.csv_zone)
                stamp = stamp.astimezone(timezone.utc)
                count = float(row[value_col])
                if not math.isfinite(count) or count < 0:
                    return self._parse_failure('Count must be finite and nonnegative; zero is valid.')
                metric = MetricRow(timestamp=stamp, error_code=target, count=count)
                if stamp in unique and float(unique[stamp].count) != count:
                    return self._parse_failure(f'Conflicting counts for bucket {stamp.isoformat()}.')
                unique[stamp] = metric
            records = sorted(unique.values(), key=lambda metric: metric.timestamp)
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'
            return self._parse_failure(error)
        if self._metric_scope != self.target_csv_path:
            self._observed_metrics = {}
            self._metric_scope = self.target_csv_path
        self.parsed_metrics = records
        self.log_state_change('parse', ('SUCCEEDED', self.target_csv_path), 'PARSE_SUCCEEDED',
                              f'CSV parsed successfully: {self.target_csv_path}.')
        self.log_metric_changes(records)
        self.log_state_change('metric_availability', (self.target_csv_path, bool(records)),
                              'METRICS_AVAILABLE' if records else 'NO_MATCHING_METRICS',
                              f'{len(records)} matching bucket(s) for error code {self.email_context.error_code}.')
        return records
    
    def _unknown_trend(self, reason: str) -> TrendEvaluation:
        # Unknown evidence must not expose stale CSV counts as current metrics.
        self.current_status = TrendStatus.UNKNOWN
        self.latest_evaluation = TrendEvaluation(
            status=TrendStatus.UNKNOWN,
            current_count=None,
            peak_count=None,
            baseline_count=None,
            trend_description=reason,
        )
        self.log_state_change(
            'trend', (TrendStatus.UNKNOWN.value,), 'TREND_UNKNOWN', reason,
        )
        self.log_state_change(
            'trend_unknown_reason', reason, 'TREND_UNAVAILABLE_REASON', reason,
        )
        return self.latest_evaluation

    def evaluate_trend(self) -> TrendEvaluation:
        try:
            return self._evaluate_trend()
        except Exception as exc:
            self.log_state_change('evaluation_error', str(exc), 'EVALUATION_FAILED', str(exc))
            self.parsed_metrics = []
            return self._unknown_trend(f'Evaluation unavailable: {exc}')
    
    def _evaluate_trend(self) -> TrendEvaluation:
        """
        Evaluate recent, complete, consecutive one-minute buckets.
        CSV history remains unchanged. The email's five-minute sum is not
        used as a baseline for one-minute trend comparisons.
        """
        parse_state = self._observation_states.get('parse', ())
        if parse_state and parse_state[0] == 'FAILED':
            return self._unknown_trend(
                f'Latest CSV parsing failed: {parse_state[-1]}; retrying next poll.'
            )

        def unknown(reason):
            self.current_status = TrendStatus.UNKNOWN
            return self._unknown_trend(reason)
        now = datetime.now(timezone.utc)
        window_end = now.replace(second=0, microsecond=0)
        window_start = window_end - timedelta(
            minutes=self.trend_window_minutes
        )
        expected_latest = window_end - timedelta(minutes=1)
        minimum = int(self.trend_minimum_buckets)
        threshold = float(self.trend_change_threshold_percent)
        if not 2 <= minimum <= self.trend_window_minutes:
            return unknown("Invalid minimum bucket configuration.")
        if not math.isfinite(threshold) or threshold <= 0:
            return unknown("Invalid trend percentage threshold.")
        # Select recent complete buckets without changing parsed_metrics.
        recent = {}
        for metric in self.parsed_metrics:
            stamp = metric.timestamp
            # Parsing should already produce timezone-aware timestamps.
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                return unknown(
                    "Metric timestamp has no timezone; cannot evaluate safely."
                )
            stamp = stamp.astimezone(timezone.utc)
            if not window_start <= stamp < window_end:
                continue
            if stamp.second != 0 or stamp.microsecond != 0:
                return unknown(
                    "Metric timestamps are not aligned to one-minute buckets."
                )
            count = float(metric.count)
            if not math.isfinite(count) or count < 0:
                return unknown("Recent metric contains an invalid count.")
            if stamp in recent and recent[stamp] != count:
                return unknown("Conflicting counts for a recent minute bucket.")
            recent[stamp] = count
        # Use the consecutive sequence ending at the latest completed minute.
        # Do not substitute an older sequence if the latest minute is missing.
        sequence = []
        cursor = expected_latest
        while cursor >= window_start and cursor in recent:
            sequence.append((cursor, recent[cursor]))
            cursor -= timedelta(minutes=1)
        sequence.reverse()
        evidence = [
            {
                "timestamp": stamp.isoformat(),
                "count": count,
            }
            for stamp, count in sequence
        ]
        self.log_state_change(
            "trend_evidence",
            (
                window_start.isoformat(),
                window_end.isoformat(),
                tuple(sequence),
            ),
            "TREND_EVIDENCE",
            f"Window=[{window_start.isoformat()}, "
            f"{window_end.isoformat()}); "
            f"recent_buckets={len(recent)}; "
            f"consecutive_latest_buckets={len(sequence)}; "
            f"evidence={evidence}.",
        )
        if expected_latest not in recent:
            return unknown(
                "Latest completed minute has no matching bucket; "
                "recent evidence is insufficient. Missing is not assumed zero."
            )
        if len(sequence) < minimum:
            return unknown(
                f"Only {len(sequence)} consecutive recent bucket(s); "
                f"at least {minimum} required. Missing minutes are not "
                "assumed zero."
            )
        first_stamp, first_count = sequence[0]
        latest_stamp, latest_count = sequence[-1]
        counts = [count for _, count in sequence]
        peak = max(counts)
        change = latest_count - first_count
        # Handle zero without division by zero.
        if first_count == 0:
            percentage_change = None
            if latest_count > 0:
                status = TrendStatus.ESCALATING
                measurement = (
                    f"Count increased from zero to {latest_count:g}; "
                    "percentage change is undefined."
                )
            else:
                status = TrendStatus.SUSTAINED
                measurement = (
                    "First and latest counts are zero; "
                    "intermediate activity, if any, is recorded in evidence."
                )
        else:
            percentage_change = change / first_count * 100
            if percentage_change >= threshold:
                status = TrendStatus.ESCALATING
            elif percentage_change <= -threshold:
                status = TrendStatus.RESOLVING
            else:
                status = TrendStatus.SUSTAINED
            measurement = (
                f"Count changed from {first_count:g} to {latest_count:g} "
                f"({percentage_change:+.2f}%)."
            )
        description = (
            f"{measurement} "
            f"Compared {len(sequence)} consecutive complete minute buckets "
            f"from {first_stamp.isoformat()} to {latest_stamp.isoformat()}; "
            f"peak={peak:g}. Provisional endpoint-change rule; "
            f"threshold={threshold:g}%. Resolving does not mean resolved."
        )
        self._observation_states['trend_unknown_reason'] = None
        self.current_status = status
        self.latest_evaluation = TrendEvaluation(
            status=status,
            current_count=latest_count,
            peak_count=peak,
            baseline_count=first_count,  # First selected one-minute count, not email sum.
            trend_description=description,
        )
        # Record changing measurements even when the trend label stays unchanged.
        self.log_state_change(
            "trend_measurement",
            (
                tuple(sequence),
                threshold,
                status.value,
            ),
            "TREND_MEASURED",
            description,
        )
        # Record trend transitions separately.
        self.log_state_change(
            "trend",
            (status.value,),
            f"TREND_{status.value}",
            description,
        )
        return self.latest_evaluation
    
    def _result(self, status: str) -> VerificationWorkflowResult:
        return VerificationWorkflowResult(
            workflow_id=self.workflow_id, workflow_status=status,
            latest_evaluation=self.latest_evaluation, observation_log=self.observation_log,
            observation_log_path=str(self.observation_log_path),
        )
    
    # def run_monitoring_loop(self) -> VerificationWorkflowResult:
    #     if self.is_monitoring:
    #         self.log_observation('WORKFLOW_ALREADY_RUNNING', 'Duplicate invocation ignored; existing loop continues.')
    #         return self._result('RUNNING')
    #     if self._last_result is not None:
    #         self.log_observation('WORKFLOW_ALREADY_COMPLETED', 'Returning completed workflow; use a new instance for a new alert.')
    #         return self._result('COMPLETED')
    #     self.start_time = datetime.now(timezone.utc)
    #     self._deadline = time.monotonic() + self.monitoring_duration.total_seconds()
    #     self.is_monitoring = True
    #     self.log_observation('WORKFLOW_STARTED', f'Workflow {self.workflow_id}; duration={self.monitoring_duration.total_seconds()/60:g} minutes; delay={self.poll_interval:g}s.')
    #     try:
    #         while time.monotonic() < self._deadline:
    #             try:
    #                 success = self.execute_powershell_fetch()
    #                 self.locate_target_csv()
    #                 if not success:
    #                     self.parsed_metrics = []
    #                     self._unknown_trend('Latest fetch failed or timed out; retrying next poll.')
    #                 elif self.target_csv_path:
    #                     self.parse_csv_data()
    #                     self.evaluate_trend()
    #                 else:
    #                     self.parsed_metrics = []
    #                     self._unknown_trend('Current-day CSV is missing; retrying next poll.')
    #             except Exception as exc:
    #                 self.log_state_change('poll_error', (type(exc).__name__, str(exc)), 'POLL_FAILED', f'{type(exc).__name__}: {exc}; retrying next poll.')
    #                 self.parsed_metrics = []
    #                 self._unknown_trend('Current poll failed; verification remains unknown.')
    #             remaining = self._deadline - time.monotonic()
    #             if remaining > 0:
    #                 time.sleep(min(self.poll_interval, remaining))
    #         self.log_observation('WORKFLOW_COMPLETED', 'Monitoring window ended; completion does not imply successful verification.')
    #         self._last_result = self._result('COMPLETED')
    #         return self._last_result
    #     finally:
    #         self.is_monitoring = False
    #         self._deadline = None

    def run_monitoring_loop(self):
        return streaming_loop(self)


if __name__ == "__main__":

    vpn_process = None

    try:

        if vpn_already_connected(OPENSEARCH_URL):
            print("[VPN] Existing VPN connection is usable; reusing it.")
        else:
            vpn_process = connect_vpn(VPN_PROFILE)

        if not setup_opensearch_credentials(
            username=USERNAME,
            password=PASSWORD,
            output_path=CRED_FILE,
        ):
            raise RuntimeError("OpenSearch credential setup failed.")



        # Sample only. In production, use the received email's timestamp.
        alert_email = EmailContext(
            sender="mvne.support@ip-tribe.com",
            timestamp=datetime(
                2026, 9, 6, 10, 43, 12,
                tzinfo=ZoneInfo("Asia/Singapore"),
            ),
            subject="[Critical] Singtel BSS || DRA Gy TimerExpiry 5012 Count",
            body=(
                "Monitor DRA Gy TimerExpiry 5012 Count entered alert status. "
                "Actual Error Count: 24673.0"
            ),
            error_code="5012",
            initial_count=24673.0,
        )

        wf = OpenSearchMonitor(
            email_context=alert_email,
            script_path=(
                "/root/projects/alert_results/demo/core/verification/"
                "Fetch-DashboardMetrics.ps1"
            ),
            output_dir=(
                "/root/projects/alert_results/demo/core/verification/"
                "OpenSearch data"
            ),
            monitoring_duration_minutes=15,
            poll_interval_seconds=30,
            csv_timezone="Asia/Singapore",
            fetch_timeout_seconds=120,
        )

        result = wf.run_monitoring_loop()

        print("\nVerification Workflow Result:")
        print(result.model_dump_json(indent=2))

        print(f"\nWorkflow status: {result.workflow_status}")
        print(
            f"Observations recorded: "
            f"{len(result.observation_log.observations)}"
        )
        print(f"Observation log: {result.observation_log_path}")

        if result.latest_evaluation is not None:
            print(
                f"Latest trend: "
                f"{result.latest_evaluation.status.value}"
            )
            print(result.latest_evaluation.trend_description)

    finally:
        if vpn_process is not None:
            disconnect_vpn(vpn_process)


