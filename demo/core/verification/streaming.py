"""Drop-in methods for the existing OpenSearchMonitor; retain parsing/trend code."""
import codecs
import os
import platform
import queue
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime,timezone
from .telemetry import emit_stage,emit_output


def _stop(process):
    try:
        if os.name == 'posix':
            os.killpg(process.pid,signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


def execute_powershell_fetch(self):
    executable = 'powershell.exe' if platform.system() == 'Windows' else 'pwsh'
    timeout = self.fetch_timeout_seconds
    if self._deadline is not None:
        timeout = min(timeout,max(.001,self._deadline-time.monotonic()))
    cmd = [executable,'-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
           '-File',str(self.script_path),'-MaxWindowMinutes','5','-MaxChunksPerRun','1',
           '-OutputRoot',str(self.output_dir)]
    process = None
    outputs = {'stdout':'','stderr':''}
    messages = queue.Queue(maxsize=128)
    finished = set()
    timed_out = False

    def reader(stream,pipe):
        decoder = codecs.getincrementaldecoder('utf-8')('replace')
        try:
            while True:
                chunk = os.read(pipe.fileno(),4096)
                if not chunk:
                    break
                text = decoder.decode(chunk)
                if text:
                    messages.put((stream,text))
            tail = decoder.decode(b'',final=True)
            if tail:
                messages.put((stream,tail))
        except Exception as exc:
            messages.put(('stderr',f'Output reader error ({stream}): {exc}\n'))
        finally:
            pipe.close()
            messages.put((stream,None))

    try:
        process = subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                   start_new_session=os.name=='posix')
        deadline = time.monotonic()+timeout
        threads = []
        for stream,pipe in (('stdout',process.stdout),('stderr',process.stderr)):
            thread = threading.Thread(target=reader,args=(stream,pipe),daemon=True)
            thread.start()
            threads.append(thread)
        while len(finished)<2 or process.poll() is None:
            if time.monotonic()>=deadline and not timed_out:
                timed_out=True
                _stop(process)
                # Briefly drain buffered output after killing the process.
                deadline=time.monotonic()+2
            elif timed_out and time.monotonic()>=deadline:
                break
            try:
                stream,text=messages.get(timeout=.05)
            except queue.Empty:
                continue
            if text is None:
                finished.add(stream)
                continue
            # Bound diagnostic tails; the full output is stored in SQLite.
            outputs[stream]=(outputs[stream]+text)[-65536:]
            console=sys.stdout if stream=='stdout' else sys.stderr
            console.write(text)
            console.flush()
            emit_output(self,stream,text)
        code=process.wait(timeout=5)
        if timed_out:
            self.log_state_change('fetch',('TIMEOUT',self.fetch_timeout_seconds),
                'FETCH_TIMEOUT',f'Fetch exceeded allowed timeout ({timeout:.1f}s). '
                f'stderr: {outputs["stderr"].strip()}')
            return False
        if code != 0:
            details=outputs['stderr'].strip() or outputs['stdout'].strip() or 'No error output.'
            self.log_state_change('fetch',('FAILED',code,details),'FETCH_FAILED',f'Exit code {code}; {details}')
            return False
        self.log_state_change('fetch',('SUCCEEDED',),'FETCH_SUCCEEDED',
            'PowerShell exited with code 0; this does not independently confirm metric query success.')
        stderr=outputs['stderr'].strip()
        self.log_state_change('fetch_stderr',stderr,'FETCH_WARNING' if stderr else 'FETCH_WARNING_CLEARED',
                             stderr or 'No PowerShell stderr warnings.')
        return True
    except Exception as exc:
        error=f'{type(exc).__name__}: {exc}'
        self.log_state_change('fetch',('FAILED',error),'FETCH_FAILED',error)
        return False
    finally:
        if process is not None and process.poll() is None:
            _stop(process)


def run_monitoring_loop(self):
    if self.is_monitoring:
        self.log_observation('WORKFLOW_ALREADY_RUNNING','Duplicate invocation ignored; existing loop continues.')
        return self._result('RUNNING')
    if self._last_result is not None:
        return self._last_result
    self.start_time=datetime.now(timezone.utc)
    self._deadline=time.monotonic()+self.monitoring_duration.total_seconds()
    self.is_monitoring=True
    try:
        emit_stage(self,'EXECUTING_VERIFICATION','Monitoring workflow started')
        self.log_observation('WORKFLOW_STARTED',
            f'Workflow {self.workflow_id}; duration={self.monitoring_duration.total_seconds()/60:g} minutes; delay={self.poll_interval:g}s.')
        
        while time.monotonic()<self._deadline:
            try:
                emit_stage(self,'EXECUTING_VERIFICATION_POWERSHELL','Fetching metrics')
                
                
                
                emit_stage(
                    self,
                    'EXECUTING_VERIFICATION_POWERSHELL',
                    'Fetching metrics',
                )

                fetch = self.execute_powershell_fetch
                started = time.monotonic()

                print(
                    f"[FETCH START] class={type(self).__name__} "
                    f"implementation={fetch.__qualname__}",
                    flush=True,
                )

                success = fetch()

                print(
                    f"[FETCH END] duration={time.monotonic() - started:.3f}s "
                    f"success={success}",
                    flush=True,
                )



                emit_stage(self,'EXECUTING_VERIFICATION_LOCATE_CSV','Locating current-day CSV')
                self.locate_target_csv()
                if not success:
                    self.parsed_metrics=[]
                    self._unknown_trend('Latest fetch failed or timed out; retrying next poll.')
                elif self.target_csv_path:
                    emit_stage(self,'EXECUTING_VERIFICATION_PARSE_CSV','Parsing metrics CSV')
                    self.parse_csv_data()
                    emit_stage(self,'EXECUTING_VERIFICATION_EVALUATE_TREND','Evaluating metric trend')
                    self.evaluate_trend()
                else:
                    self.parsed_metrics=[]
                    self._unknown_trend('Current-day CSV is missing; retrying next poll.')
            except Exception as exc:
                self.log_state_change('poll_error',(type(exc).__name__,str(exc)),
                    'POLL_FAILED',f'{type(exc).__name__}: {exc}; retrying next poll.')
                self.parsed_metrics=[]
                self._unknown_trend('Current poll failed; verification remains unknown.')
            
            remaining=self._deadline-time.monotonic()
            if remaining>0:
                emit_stage(self,'EXECUTING_VERIFICATION','Waiting for next poll')
                time.sleep(min(self.poll_interval,remaining))
        
        self.log_observation('WORKFLOW_COMPLETED',
            'Monitoring window ended; completion does not imply successful verification.')
        self._last_result=self._result('COMPLETED')
        emit_stage(self,'COMPLETE_VERIFICATION',
                   'Monitoring window ended; see observations for evidence and trend')
        return self._last_result
    except BaseException as exc:
        emit_stage(self,'FAILED_VERIFICATION','Verification workflow aborted',
                   error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        self.is_monitoring=False
        self._deadline=None
