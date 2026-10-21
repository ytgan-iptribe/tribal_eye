# Shared reporting helper. Requires python3; source before any operation.
command -v python3 >/dev/null 2>&1 || { echo 'python3 required for structured results' >&2; exit 70; }
exec 9>&1 8>&2
RESULT_DIR=$(mktemp -d) || exit 70
RESULT_RECORDS="$RESULT_DIR/checks.jsonl"
RESULT_DIAGNOSTICS="$RESULT_DIR/diagnostics.txt"
: > "$RESULT_RECORDS"
: > "$RESULT_DIAGNOSTICS"
result_diagnostic() { printf '%s\n' "$*" >> "$RESULT_DIAGNOSTICS"; }
result_check() {
    # subject, status, error message (empty for success), then key=value details
    python3 - "$RESULT_RECORDS" "$@" <<'PY'
import json, sys
path, subject, status, message, *pairs = sys.argv[1:]
check = dict(subject=subject, status=status, success=status == 'SUCCESS',
             errors=[] if status == 'SUCCESS' else [dict(code=status, message=message)],
             details=dict(p.split('=', 1) for p in pairs))
with open(path, 'a') as f:
    f.write(json.dumps(check) + '\n')
PY
}
result_finish() {
    local rc="$1" report_rc
    # Preserve DATA's preexisting lock cleanup; never delete a lock not owned by this run.
    if [[ "$RESULT_KIND" == DATA && "${RESULT_OWNS_LOCK:-0}" == 1 ]]; then
        rm -f "$LOCK_FILE"
    fi
    python3 - "$RESULT_KIND" "$rc" "$RESULT_RECORDS" "$RESULT_DIAGNOSTICS" <<'PY' >&9 2>&8
import json, sys
kind, rc, records, diagnostics = sys.argv[1:]
rc = int(rc)
with open(records) as f:
    checks = [json.loads(line) for line in f if line.strip()]
with open(diagnostics) as f:
    warnings = list(dict.fromkeys(line.strip() for line in f if line.strip()))
errors = []
if rc != 0:
    errors.append(dict(code='SCRIPT_EXIT', message=f'Script exited with code {rc}. See warnings and stderr for diagnostics.'))
if not checks:
    errors.append(dict(code='NO_RESULT', message='Script exited before producing a service result.'))
success = rc == 0 and bool(checks) and all(c['success'] for c in checks)
payload = dict(schema_version=1, kind=kind, success=success, checks=checks, errors=errors, warnings=warnings)
print('MODEM_RESULT_JSON=' + json.dumps(payload), flush=True)
sys.exit(0 if success else (rc if 1 <= rc <= 255 else 1))
PY
    report_rc=$?
    rm -rf "$RESULT_DIR"
    trap - EXIT
    exit "$report_rc"
}
trap 'result_finish "$?"' EXIT
