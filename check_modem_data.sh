#!/bin/bash
RESULT_KIND=CALL
source "$(dirname -- "${BASH_SOURCE[0]}")/modem_result.sh" || exit 70
#===============================================================================
# Script Name   : callmodem2TOmodem1.sh
# Version       : 1.6.3
# Description   : Places an outgoing call using dynamic modem discovery (mmcli).
#                 Chooses the other/free AT port (not used by incoming handler),
#                 retries on MISSED/FAILED, logs to CSV/DB, and emails on failure.
#                 Supports friendly names for --from/--to: VIVIFI, Zero1, or a number.
#                 External no-answer (ring->release w/o BEGIN) => STATUS=RING-RELEASE,
#                 but only if ring duration/events exceed thresholds (see tunables).
#
# Author        : Victor James Castigo
# Date Created  : 15 July 2025
# Last Modified : 06 August 2026
#
# Features      :
#   - Friendly names mapping:
#       FROM=VIVIFI -> operator=VIVIFI, IMEI=860537062221411, FROM_NUMBER=88603568
#       FROM=Zero1  -> operator=Zero1,  IMEI=860537061206173, FROM_NUMBER=89297580
#       TO=VIVIFI   -> TO_NUMBER=88603568
#       TO=Zero1    -> TO_NUMBER=89297580
#   - Dynamic modem discovery by operator name or IMEI (ModemManager/mmcli)
#   - FAST-PATH: picks the “other” AT port than the incoming handler (no probe)
#   - SLOW-PATH: probes remaining AT ports and selects one that answers “OK”
#   - Full trace logging (SEND/RECV/ERROR) with watchdog heartbeat
#   - Automatic retry on MISSED/FAILED/ambiguous (up to 2 attempts)
#   - Cross-checks incoming trace for BEGIN/END duration windows (robust epoch math)
#   - External no-answer (ring->release without BEGIN) => SUCCESS with 0s
#   - Logs to CSV and SQLite DB via external script
#   - Email alerting on final failure/missed with 0 duration
#   - Sectioned, readable, and secure (umask 077)
#   - Updated POST_DIAL_SLEEP="${POST_DIAL_SLEEP:-10}
#
# Usage (friendly names):
#   callmodem2TOmodem1.sh --from VIVIFI --to Zero1
#   callmodem2TOmodem1.sh --from Zero1  --to VIVIFI
#   callmodem2TOmodem1.sh --from VIVIFI --to 96391124
#   callmodem2TOmodem1.sh --from Zero1  --to 96391124
#
# Still supported (explicit):
#   callmodem2TOmodem1.sh [--to <phone>] [--port </dev/ttyUSBx>] [--operator <name>] [--imei <id>] [--from <phone>]
#
# Requirements:
#   - ModemManager (mmcli) installed and running
#   - Incoming handler trace at /var/log/incomingsms-calls/incoming_trace.log
#   - State file from incoming handler: /run/incominghandler/incominghandler_<IMEI>.state
#   - External script: /opt/call_logs_dashboard/log_outgoing_call.sh
#   - Email (mailx) configured
#
# Exit Codes:
#   0 - Completed (success or final handled failure)
#   1 - Lock timeout or modem/port error
#===============================================================================

#set -euo pipefail  # leave off to avoid premature exit on serial timeouts
umask 077

#=============================
# CONFIG / CONSTANTS
#=============================
LOCK_FILE="/tmp/modem_lockfile"

# Unique per-process response file. Avoids ownership collisions between
# portal/flaskapp and interactive users, and prevents concurrent runs from
# reading stale modem responses.
MODEM_RESPONSE_FILE="$(mktemp /tmp/call_modem_response.XXXXXX)" || {
  echo "Failed to create temporary modem response file" >&2
  exit 1
}

MAX_RETRIES_LOCK=10
RETRY_INTERVAL_LOCK=15

ATTEMPTS_MAX=2
SLEEP_BETWEEN_ATTEMPTS=5
#POST_DIAL_SLEEP=10
POST_DIAL_SLEEP="${POST_DIAL_SLEEP:-10}"

# Trace scanning
TRACE_LOOKBACK_LINES=${TRACE_LOOKBACK_LINES:-300}
TRACE_MATCH_WINDOW_SECS=40
INCOMING_MATCH_GRACE_SECS=${INCOMING_MATCH_GRACE_SECS:-45}

# RING-RELEASE tunables
RING_PERIOD_SEC=${RING_PERIOD_SEC:-5}
RING_START_OFFSET=${RING_START_OFFSET:-1}
MIN_ALERT_SEC=${MIN_ALERT_SEC:-4}             # min seconds between first RING and RELEASE
MIN_RING_EVENTS=${MIN_RING_EVENTS:-2}         # need at least 2 ring indications
FAST_NO_CARRIER_SEC=${FAST_NO_CARRIER_SEC:-3} # NO CARRIER earlier than this -> FAILED

# Force-fail numbers
FORCE_FAIL_NUMBERS=("1111" "2222")
RETRY_ON_FORCE_FAIL=${RETRY_ON_FORCE_FAIL:-1}  # 0 = no retry on forced fail, 1 = allow retries (default)

OUT_DIR="/var/log/outgoingsms-calls"
mkdir -p "$OUT_DIR" 2>/dev/null || true
TRACE_LOG="$OUT_DIR/outgoing_trace.log"
LOGFILE="$OUT_DIR/outgoing_voice_call_log.csv"
WATCHDOG="$OUT_DIR/callmodem2TOmodem1_watchdog.log"

INCOMING_TRACE="/var/log/incomingsms-calls/incoming_trace.log"
EMAIL_LOG="/var/log/emailalerts.log"

EMAIL="${EMAIL:-vjcastigo@ip-tribe.com,tribal.eye.alerts@ip-tribe.com}"

# Defaults (can be overridden by args/env or friendly names)
TARGET_OPERATOR="${TARGET_OPERATOR:-VIVIFI}"
TARGET_IMEI="${TARGET_IMEI:-860537062221411}"
FROM_NUMBER="${FROM_NUMBER:-88603568}"
DEFAULT_TO="${DEFAULT_TO:-89297580}"  # default callee if --to is not provided

# Known internal SIM numbers (callee in this list => internal)
INTERNAL_NUMBERS=("88603568" "89297580")

#=============================
# HELPERS
#=============================
is_internal_number() {
  local num="$1"
  for n in "${INTERNAL_NUMBERS[@]}"; do [[ "$num" == "$n" ]] && return 0; done
  return 1
}
is_forced_fail_number() {
  local num="$1"
  for n in "${FORCE_FAIL_NUMBERS[@]}"; do [[ "$num" == "$n" ]] && return 0; done
  return 1
}

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log_trace() { echo -e "[$(ts)] [INFO] $1" >> "$TRACE_LOG"; }
log_send()  { echo -e "[$(ts)] [SEND] $1" >> "$TRACE_LOG"; }
log_recv()  { while IFS= read -r line; do echo -e "[$(ts)] [RECV] $line" >> "$TRACE_LOG"; done; }
log_error() { result_diagnostic "$1"; echo -e "[$(ts)] [ERROR] $1" >> "$TRACE_LOG"; }
watchdog()  { echo "[$(ts)] $1" >> "$WATCHDOG"; }

read_modem_response() {
  local timeout_seconds="$1"

  # Clear this run's private response buffer before each modem read.
  : > "$MODEM_RESPONSE_FILE"

  timeout "$timeout_seconds" cat <&3 \
    | tee "$MODEM_RESPONSE_FILE" \
    | log_recv

  cat "$MODEM_RESPONSE_FILE"
}

cleanup_and_exit() {
  local exit_code="${1:-0}"

  exec 3>&- 3<&- 2>/dev/null || true
  rm -f "$LOCK_FILE" "$MODEM_RESPONSE_FILE" 2>/dev/null || true
  log_trace "Lock and temporary modem response file removed for callmodem2TOmodem1.sh"
  watchdog "Script completed and temporary files removed."
  exit "$exit_code"
}

#=============================
# ARGUMENTS (friendly names)
#=============================
TO_NUMBER="$DEFAULT_TO"
PORT=""
FROM_FRIENDLY=""
TO_FRIENDLY=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --to)    TO_FRIENDLY="$2"; shift 2 ;;
    --from)  FROM_FRIENDLY="$2"; shift 2 ;;
    --port)  PORT="$2"; shift 2 ;;
    --operator) TARGET_OPERATOR="$2"; shift 2 ;;
    --imei)  TARGET_IMEI="$2"; shift 2 ;;
    *) echo "Unknown option: $1
Usage: $0 --from <VIVIFI|Zero1|number> --to <VIVIFI|Zero1|number> [--port </dev/ttyUSBx>] [--operator <name>] [--imei <id>]"
       exit 1 ;;
  esac
done

to_upper() { echo "$1" | tr '[:lower:]' '[:upper:]'; }
resolve_from() {
  local name_uc; name_uc="$(to_upper "$1")"
  case "$name_uc" in
    VIVIFI) TARGET_OPERATOR="VIVIFI"; TARGET_IMEI="860537062221411"; FROM_NUMBER="88603568" ;;
    ZERO1)  TARGET_OPERATOR="Zero1";  TARGET_IMEI="860537061206173"; FROM_NUMBER="89297580" ;;
    *) [[ "$1" =~ ^[0-9]+$ ]] && FROM_NUMBER="$1" || { log_error "Unknown --from '$1' (use VIVIFI, Zero1, or a number)"; exit 1; } ;;
  esac
}
resolve_to() {
  local v="$1"; local u; u="$(to_upper "$v")"
  case "$u" in
    VIVIFI) TO_NUMBER="88603568" ;;
    ZERO1)  TO_NUMBER="89297580" ;;
    *) [[ "$v" =~ ^[0-9]+$ ]] && TO_NUMBER="$v" || { log_error "Unknown --to '$v' (use VIVIFI, Zero1, or a number)"; exit 1; } ;;
  esac
}
[[ -n "$FROM_FRIENDLY" ]] && resolve_from "$FROM_FRIENDLY"
[[ -n "$TO_FRIENDLY"   ]] && resolve_to   "$TO_FRIENDLY"

log_trace "Args resolved: OP=$TARGET_OPERATOR IMEI=$TARGET_IMEI FROM=$FROM_NUMBER TO=$TO_NUMBER PORT=${PORT:-auto}"
watchdog "Call target: TO=$TO_NUMBER FROM=$FROM_NUMBER (OP=$TARGET_OPERATOR IMEI=$TARGET_IMEI)"

#=============================
# DISCOVERY (MODEM + FREE/OTHER AT PORT)
#=============================
discover_free_port() {
  if [[ -n "$PORT" ]]; then
    log_trace "PORT override provided: $PORT"
    [[ -e "$PORT" ]] || { log_error "Provided PORT $PORT does not exist"; return 1; }
    echo "$PORT"; return 0
  fi

  command -v mmcli >/dev/null 2>&1 || { log_error "mmcli not found"; return 1; }

  local list out id operator imei equip
  local -a at_ports
  list=$(mmcli -L 2>/dev/null) || { log_error "mmcli -L failed"; return 1; }

  local ids=()
  while read -r line; do
    [[ -z "$line" ]] && continue
    id=$(echo "$line" | grep -oE 'Modem/[0-9]+' | awk -F'/' '{print $2}')
    [[ -n "$id" ]] && ids+=("$id")
  done <<< "$list"
  [[ ${#ids[@]} -eq 0 ]] && { log_error "No modems found"; return 1; }

  local chosen_id=""
  at_ports=()

  for id in "${ids[@]}"; do
    out=$(mmcli -m "$id" 2>/dev/null) || { log_error "mmcli -m $id failed; skip"; continue; }
    operator=$(echo "$out" | awk -F': *' '/operator name/ {print $2}' | xargs | tail -n1)
    imei=$(echo "$out" | awk -F': *' '/^[[:space:]]*3GPP[[:space:]]*\|[[:space:]]*imei/ {print $2}' | xargs | tail -n1)
    equip=$(echo "$out" | awk -F': *' '/equipment id/ {print $2}' | xargs | tail -n1)
    mapfile -t at_ports < <(echo "$out" | grep -oE 'tty(USB|ACM)[0-9]+[[:space:]]*\(at\)' | awk '{print $1}' | sort -V)
    log_trace "Discovery: Modem $id operator=${operator:-N/A} imei=${imei:-N/A} at_ports=${at_ports[*]:-none}"
    if [[ "$operator" == "$TARGET_OPERATOR" || "$imei" == "$TARGET_IMEI" || "$equip" == "$TARGET_IMEI" ]]; then
      chosen_id="$id"; break
    fi
  done

  [[ -z "$chosen_id" ]] && { log_error "No modem matched operator=$TARGET_OPERATOR or IMEI=$TARGET_IMEI"; return 1; }
  [[ ${#at_ports[@]} -eq 0 ]] && { log_error "No AT ports on modem $chosen_id"; return 1; }

  local state_file="/run/incominghandler/incominghandler_${TARGET_IMEI}.state"
  if [[ -f "$state_file" ]]; then
    # shellcheck disable=SC1090
    source "$state_file" || true
    if [[ -n "${PORT:-}" ]]; then
      local inbound_port other=""; inbound_port="$(basename "$PORT")"
      for p in "${at_ports[@]}"; do if [[ "$p" != "$inbound_port" ]]; then other="$p"; break; fi; done
      if [[ -n "$other" ]]; then
        local other_dev="/dev/$other"
        if ! command -v fuser >/dev/null 2>&1 || ! fuser -s "$other_dev"; then
          log_trace "Fast-path select (other than inbound): $other_dev"; echo "$other_dev"; return 0
        else
          log_trace "Fast-path other port busy: $other_dev; will probe alternatives"
        fi
      else
        log_trace "Fast-path unable to find other port (single AT port?)"
      fi
    else
      log_trace "State file missing PORT=…; skipping fast-path"
    fi
  else
    log_trace "No state file $state_file; skipping fast-path"
  fi

  local dev line
  for p in "${at_ports[@]}"; do
    dev="/dev/$p"
    if command -v fuser >/dev/null 2>&1 && fuser -s "$dev"; then log_trace "Port busy: $dev"; continue; fi
    if exec 3<> "$dev"; then
      timeout 0.5 cat <&3 >/dev/null 2>&1 || true
      echo -e "AT\r" >&3
      log_send "AT (probe $dev)"
      line=""; read -t 2 -r line <&3 || line=""
      [[ -n "$line" ]] && log_recv "$line"
      exec 3>&-
      if echo "$line" | grep -qi "OK"; then log_trace "Selected free/probed port: $dev"; echo "$dev"; return 0; fi
      log_trace "Probe failed/no OK on $dev; trying next"
    else
      log_trace "Cannot open $dev; skipping"
    fi
  done

  dev="/dev/${at_ports[0]}"; log_trace "Fallback to first AT port: $dev"; echo "$dev"; return 0
}

PORT="$(discover_free_port)" || { log_error "Port discovery failed"; cleanup_and_exit 1; }
log_trace "Using PORT=$PORT (FROM=$FROM_NUMBER TO=$TO_NUMBER)"
watchdog "Placing call via $PORT FROM=$FROM_NUMBER TO=$TO_NUMBER"

#=============================
# SIGNAL TRAPS & LOCK
#=============================
trap 'log_trace "[TRAP] SIGTERM received, cleaning lock"; watchdog "[TRAP] SIGTERM"; cleanup_and_exit 1' TERM
trap 'log_trace "[TRAP] SIGINT received, cleaning lock";  watchdog "[TRAP] SIGINT";  cleanup_and_exit 1' INT

sleep 1
log_trace "Waiting 1 second before call start"
watchdog "Script started, sleeping 1 second before call"

attempts=0
while [ -e "$LOCK_FILE" ] && [ $attempts -lt $MAX_RETRIES_LOCK ]; do
  log_trace "Another script is running. Retrying in $RETRY_INTERVAL_LOCK seconds... Attempt $((attempts + 1))"
  watchdog "Lock exists; waiting ($attempts/$MAX_RETRIES_LOCK)"
  sleep $RETRY_INTERVAL_LOCK
  attempts=$((attempts + 1))
done
if [ $attempts -ge $MAX_RETRIES_LOCK ]; then
  log_error "Maximum lock retries reached. Exiting."
  watchdog "Gave up after $MAX_RETRIES_LOCK lock attempts."
  exit 1
fi

touch "$LOCK_FILE"
log_trace "Lock file created"
watchdog "Lock acquired"

#=============================
# PRIME VOICE URCs
#=============================
prime_modem_for_voice_urcs() {
  local P="$1"
  exec 3<> "$P" || return 1
  log_trace "Priming modem for voice URCs on $P"
  printf "ATE0\r"            >&3
  printf "AT+CLIP=1\r"       >&3
  printf "AT+COLP=1\r"       >&3
  printf "AT+CLCC=1\r"       >&3
  printf "AT+CMER=0,0,0,0\r" >&3   # disable CIEV URCs
  sleep 0.5
  exec 3>&-
  return 0
}
prime_modem_for_voice_urcs "$PORT" || { log_error "Failed to prime modem $PORT"; cleanup_and_exit 1; }

#=============================
# INCOMING-TRACE MATCHING HELPERS
#=============================
scan_incoming_for_end() {
  local atd_ts="$1" from_num="$2"
  local start_epoch end_epoch end_ts
  start_epoch=$(date -d "$atd_ts" +%s 2>/dev/null || date +%s)
  end_epoch=$(( start_epoch + INCOMING_MATCH_GRACE_SECS ))
  end_ts=$(date -d "@$end_epoch" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "$atd_ts")
  log_trace "Incoming END scan window: start=$atd_ts end=$end_ts from=$from_num (grace=${INCOMING_MATCH_GRACE_SECS}s)"
  tail -n "$TRACE_LOOKBACK_LINES" "$INCOMING_TRACE" 2>/dev/null | \
  awk -v start="$atd_ts" -v end="$end_ts" -v from="$from_num" '
    match($0, /^\[([0-9-]+ [0-9:]+)\]\s+(\[[A-Z]+\])?\s*(.*)$/, m) {
      ts = m[1]; payload = m[3];
      if (ts < start || ts > end) next;
      if (payload ~ ("\\+CLIP: \"" from "\"") || payload ~ ("Answered call from " from)) { seen_from = 1; }
      if (seen_from == 1 && payload ~ /VOICE CALL: END:[[:space:]]*[0-9]+/) {
        if (match(payload, /VOICE CALL: END:[[:space:]]*([0-9]+)/, d)) { print d[1]; exit; }
      }
    }
  '
}

#=============================
# OUTGOING CALL LOOP
#=============================
ATTEMPT=1
FINAL_STATUS=""
FINAL_DURATION=0
FINAL_TIMESTAMP=""
SUCCESS=0
ALREADY_LOGGED=0
CURRENT_ATTEMPT=0

while [ $ATTEMPT -le $ATTEMPTS_MAX ]; do
  TIMESTAMP=$(ts)
  log_trace "----- Outgoing call attempt #$ATTEMPT at $TIMESTAMP -----"
  watchdog "Attempt #$ATTEMPT start $TIMESTAMP"

  # Force-fail guard
  if is_forced_fail_number "$TO_NUMBER"; then
    STATUS="FAILED"
    DURATION=0; DURATION_DB="0"; DURATION_CSV="0 seconds"; DURATION_STR="$DURATION_CSV"
    log_trace "TO_NUMBER=$TO_NUMBER is in FORCE_FAIL_NUMBERS -> mark FAILED"

    if (( RETRY_ON_FORCE_FAIL == 0 )); then
      FINAL_STATUS="$STATUS"
      FINAL_DURATION="$DURATION_DB"
      FINAL_TIMESTAMP="$TIMESTAMP"
      CURRENT_ATTEMPT="$ATTEMPT"
      echo "$FINAL_TIMESTAMP,CALL FROM ${FROM_NUMBER},TO ${TO_NUMBER},${FINAL_STATUS},${DURATION_STR},Attempt #$CURRENT_ATTEMPT" >> "$LOGFILE"
      log_trace "CSV logged: ${FINAL_STATUS} ${DURATION_STR} Attempt #$CURRENT_ATTEMPT"
      /opt/call_logs_dashboard/log_outgoing_call.sh "$FROM_NUMBER" "$TO_NUMBER" "$FINAL_STATUS" "$FINAL_DURATION" "$FINAL_TIMESTAMP" "$CURRENT_ATTEMPT"
      watchdog "DB logged: ${FINAL_STATUS} ${FINAL_DURATION} seconds Attempt #$CURRENT_ATTEMPT"
      # email if 0s
      if [[ "$FINAL_DURATION" =~ ^[0-9]+$ ]] && [ "$FINAL_DURATION" -eq 0 ]; then
        SUBJECT="[VOICE CALL] ${TARGET_OPERATOR} ${FINAL_STATUS} - ${FROM_NUMBER} → ${TO_NUMBER}"
        BODY="Call from $FROM_NUMBER to $TO_NUMBER at $FINAL_TIMESTAMP ended with status: $FINAL_STATUS (Duration: 0 seconds, Attempts: $CURRENT_ATTEMPT)."
        echo "$BODY" | mailx -s "$SUBJECT" "$EMAIL"
        echo "$FINAL_TIMESTAMP,Email sent to $EMAIL - Subject: $SUBJECT" >> "$EMAIL_LOG"
        log_trace "Alert email sent for $FINAL_STATUS, duration 0 seconds, attempt #$CURRENT_ATTEMPT"
      fi
      ALREADY_LOGGED=1
      break
    else
      log_trace "RETRY_ON_FORCE_FAIL=1 -> proceeding with retry flow"
      # (we intentionally fall through so retry logic applies)
    fi
  fi

  # Ensure port exists
  if [ ! -e "$PORT" ]; then
    log_error "Modem port $PORT not found"
    watchdog "Port $PORT missing; abort."
    cleanup_and_exit 1
  fi

  # Open port and dial
  exec 3<> "$PORT"
  log_trace "Opened port $PORT"
  watchdog "Opened $PORT for dialing"
  timeout 1 cat <&3 > /dev/null

  ATD_TIMESTAMP=$(ts)
  DIAL_CMD="ATD$TO_NUMBER;"
  echo -e "$DIAL_CMD\r" >&3
  log_send "$DIAL_CMD"
  watchdog "ATD sent at $ATD_TIMESTAMP"

  RESPONSE=$(read_modem_response 5)
  sleep "$POST_DIAL_SLEEP"
  watchdog "Slept $POST_DIAL_SLEEP seconds during call"

  POST_HANGUP=""
  if [[ "$RESPONSE" != *"VOICE CALL: END"* ]]; then
    echo -e "AT+CHUP\r" >&3
    log_send "AT+CHUP"
    POST_HANGUP=$(read_modem_response 5)
  fi

  exec 3>&-
  log_trace "Closed port $PORT"
  watchdog "Closed port after hangup"

  FULL_RESPONSE="$RESPONSE"$'\n'"$POST_HANGUP"

  STATUS=""
  DURATION=0
  DURATION_DB=""
  DURATION_CSV=""
  DURATION_STR=""

  # SUCCESS by local END
  RAW_DURATION=$(echo "$FULL_RESPONSE" | grep -oE 'VOICE CALL: END: *[0-9]+' | grep -oE '[0-9]+')
  if [[ -n "$RAW_DURATION" ]]; then
    DURATION=$((10#$RAW_DURATION))
    STATUS="SUCCESS"
    log_trace "Matched VOICE CALL: END from local modem output: duration=${DURATION}s"
  fi

  # Quick NO CARRIER guard
  if [[ -z "$STATUS" ]]; then
    ATD_EPOCH=$(date -d "$ATD_TIMESTAMP" +%s 2>/dev/null || date +%s)
    NO_CARRIER_TS="$(awk -v start="$ATD_TIMESTAMP" '
      match($0, /^\[([0-9-]+ [0-9:]+)\]\s+\[RECV\]\s*NO CARRIER/, m) { ts=m[1]; if (ts>=start) { print ts; exit } }
    ' "$TRACE_LOG")"
    if [[ -n "$NO_CARRIER_TS" ]]; then
      NC_EPOCH=$(date -d "$NO_CARRIER_TS" +%s 2>/dev/null || echo $ATD_EPOCH)
      if (( NC_EPOCH - ATD_EPOCH < FAST_NO_CARRIER_SEC )); then
        STATUS="FAILED"
        log_trace "NO CARRIER arrived < ${FAST_NO_CARRIER_SEC}s after ATD -> mark FAILED"
      fi
    fi
  fi

  # External RING->RELEASE (with thresholds)
  if [[ -z "$STATUS" ]]; then
    IS_EXTERNAL=1; is_internal_number "$TO_NUMBER" && IS_EXTERNAL=0
    SAW_RING=0; SAW_RELEASE=0; RING_EVENTS=0
    echo "$FULL_RESPONSE" | grep -qE '\+CLCC:\s*[0-9]+,0,2,' && SAW_RING=1
    echo "$FULL_RESPONSE" | grep -qE '\+CLCC:\s*[0-9]+,0,6,' && SAW_RELEASE=1
    RING_EVENTS=$(echo "$FULL_RESPONSE" | grep -cE '\+CLCC:\s*[0-9]+,0,2,')

    if [[ $IS_EXTERNAL -eq 1 && $SAW_RING -eq 1 && $SAW_RELEASE -eq 1 && "$FULL_RESPONSE" != *"VOICE CALL: BEGIN"* ]]; then
      END_TS="$(ts)"

      RING_TS="$(awk -v num="$TO_NUMBER" -v start="$ATD_TIMESTAMP" -v end="$END_TS" '
        match($0, /^\[([0-9-]+ [0-9:]+)\]/, m) {
          ts = m[1]
          if (ts >= start && ts <= end && $0 ~ num && $0 ~ /\+CLCC:/ && $0 ~ /,0,2,/) { print ts; exit }
        }' "$TRACE_LOG")"
      REL_TS="$(awk -v num="$TO_NUMBER" -v start="$ATD_TIMESTAMP" -v end="$END_TS" '
        match($0, /^\[([0-9-]+ [0-9:]+)\]/, m) {
          ts = m[1]
          if (ts >= start && ts <= end && $0 ~ num && $0 ~ /\+CLCC:/ && $0 ~ /,0,6,/) { rel=ts }
        } END { if (rel) print rel }' "$TRACE_LOG")"

      [[ -z "$RING_TS" ]] && RING_TS="$ATD_TIMESTAMP"
      [[ -z "$REL_TS"  ]] && REL_TS="$END_TS"

      RING_EPOCH=$(date -d "$RING_TS" +%s 2>/dev/null || date +%s)
      REL_EPOCH=$(date -d "$REL_TS"  +%s 2>/dev/null || date +%s)
      ALERT_DURATION=$(( REL_EPOCH - RING_EPOCH ))
      (( ALERT_DURATION < 0 )) && ALERT_DURATION=0

      if (( ALERT_DURATION >= MIN_ALERT_SEC && RING_EVENTS >= MIN_RING_EVENTS )); then
        STATUS="RING-RELEASE"
        RING_COUNT_EST=$(awk -v d="$ALERT_DURATION" -v p="${RING_PERIOD_SEC:-5}" 'BEGIN{printf("%d", (d/p)+0.5)}')
        (( RING_COUNT_EST < 1 )) && RING_COUNT_EST=1
        DURATION="$RING_COUNT_EST"
        if [ "$RING_COUNT_EST" -eq 1 ]; then
          DURATION_CSV="RING-RELEASE (~1 ring)"
        else
          DURATION_CSV="RING-RELEASE (~${RING_COUNT_EST} rings)"
        fi
        DURATION_DB="$RING_COUNT_EST"
        DURATION_STR="$DURATION_CSV"
        log_trace "External RING->RELEASE accepted: rings=${RING_EVENTS}, alert=${ALERT_DURATION}s (~${RING_COUNT_EST} rings)"
      else
        log_trace "External RING->RELEASE too short: rings=${RING_EVENTS}, alert=${ALERT_DURATION}s -> not classifying"
      fi
    fi
  fi

  # Ambiguity checks → incoming trace
  if [[ -z "$STATUS" ]]; then
    watchdog "Ambiguous; scanning incoming trace for END (FROM=$FROM_NUMBER)"
    DUR_FROM_INCOMING="$(scan_incoming_for_end "$ATD_TIMESTAMP" "$FROM_NUMBER")"
    if [[ "$DUR_FROM_INCOMING" =~ ^[0-9]+$ ]] && [ "$DUR_FROM_INCOMING" -ge 0 ]; then
      STATUS="SUCCESS"
      DURATION=$((10#$DUR_FROM_INCOMING))
      DURATION_DB="$DURATION"
      DURATION_CSV="${DURATION} seconds"
      DURATION_STR="$DURATION_CSV"
      log_trace "Matched VOICE CALL: END in incoming trace: duration=${DURATION}s"
      watchdog "Incoming END matched; marking SUCCESS (${DURATION}s)"
    fi
  fi

  # Still ambiguous → MISSED or FAILED
  if [[ -z "$STATUS" ]]; then
    watchdog "Still ambiguous; scanning incoming trace for MISSED_CALL (FROM=$FROM_NUMBER)"
    MISSED_FOUND=0
    atd_epoch=$(date -d "$ATD_TIMESTAMP" +%s 2>/dev/null)
    while read -r logline; do
      if [[ "$logline" =~ ^\[([0-9-]+\ [0-9:]+)\].*MISSED_CALL.*$FROM_NUMBER ]]; then
        logts="${BASH_REMATCH[1]}"; log_epoch=$(date -d "$logts" +%s 2>/dev/null)
        if [[ -n "$log_epoch" && -n "$atd_epoch" && $log_epoch -ge $atd_epoch && $((log_epoch - atd_epoch)) -le $TRACE_MATCH_WINDOW_SECS ]]; then
          MISSED_FOUND=1; break
        fi
      fi
    done < <(tail -n "$TRACE_LOOKBACK_LINES" "$INCOMING_TRACE")

    if [ "$MISSED_FOUND" = "1" ] && [ -z "$STATUS" ]; then
      STATUS="MISSED"
      DURATION=0; DURATION_DB="0"; DURATION_CSV="0 seconds"; DURATION_STR="$DURATION_CSV"
      log_trace "Matched MISSED_CALL in incoming trace at $logts for FROM_NUMBER=$FROM_NUMBER"
      watchdog "MISSED_CALL detected near ATD time"
    else
      STATUS="FAILED"
      log_trace "No MISSED/END match; marking FAILED"
    fi
  fi

  # Build duration strings if needed
  if [[ -z "$DURATION_DB" || -z "$DURATION_CSV" || -z "$DURATION_STR" ]]; then
    if [[ "$DURATION" =~ ^[0-9]+$ ]]; then
      DURATION_DB="$DURATION"
      DURATION_CSV="${DURATION} seconds"
      DURATION_STR="$DURATION_CSV"
    else
      DURATION_DB="0"
      DURATION_CSV="$DURATION"
      DURATION_STR="$DURATION_CSV"
    fi
  fi

  # Retry & logging
  if [ "$STATUS" = "SUCCESS" ] || [ "$STATUS" = "RING-RELEASE" ]; then
    FINAL_STATUS="$STATUS"
    FINAL_DURATION="$DURATION_DB"
    FINAL_TIMESTAMP="$TIMESTAMP"
    SUCCESS=1
    CURRENT_ATTEMPT="$ATTEMPT"
    echo "$FINAL_TIMESTAMP,CALL FROM ${FROM_NUMBER},TO ${TO_NUMBER},${FINAL_STATUS},${DURATION_STR},Attempt #$CURRENT_ATTEMPT" >> "$LOGFILE"
    log_trace "CSV logged: ${FINAL_STATUS} ${DURATION_STR} Attempt #$CURRENT_ATTEMPT"
    /opt/call_logs_dashboard/log_outgoing_call.sh "$FROM_NUMBER" "$TO_NUMBER" "$FINAL_STATUS" "$FINAL_DURATION" "$FINAL_TIMESTAMP" "$CURRENT_ATTEMPT"
    watchdog "DB logged: ${FINAL_STATUS} ${FINAL_DURATION} seconds Attempt #$CURRENT_ATTEMPT"
    break
  elif [ $ATTEMPT -lt $ATTEMPTS_MAX ]; then
    log_trace "Status $STATUS; will retry after ${SLEEP_BETWEEN_ATTEMPTS}s (next Attempt #$((ATTEMPT+1)))."
    watchdog "$STATUS; sleeping ${SLEEP_BETWEEN_ATTEMPTS}s before retry"
    sleep "$SLEEP_BETWEEN_ATTEMPTS"
  else
    FINAL_STATUS="$STATUS"
    FINAL_DURATION="$DURATION_DB"
    FINAL_TIMESTAMP="$TIMESTAMP"
    CURRENT_ATTEMPT="$ATTEMPT"
    log_trace "Max retries reached. Final status: $FINAL_STATUS"
    watchdog "Final status after retries: $FINAL_STATUS"
  fi

  ATTEMPT=$((ATTEMPT + 1))
done

#=============================
# FINAL LOGGING + EMAIL ALERT
#=============================
if [ "$SUCCESS" != "1" ] && [ "$ALREADY_LOGGED" != "1" ]; then
  # If we never incremented, ATTEMPT currently points to "next" attempt; use CURRENT_ATTEMPT set above.
  if [[ "$CURRENT_ATTEMPT" -le 0 ]]; then
    CURRENT_ATTEMPT=$(( ATTEMPT - 1 ))
    (( CURRENT_ATTEMPT < 1 )) && CURRENT_ATTEMPT=1
  fi

  if [[ "$FINAL_DURATION" =~ ^[0-9]+$ ]]; then
    FINAL_DURATION_STR="${FINAL_DURATION} seconds"
  else
    FINAL_DURATION_STR="$FINAL_DURATION"
  fi

  echo "$FINAL_TIMESTAMP,CALL FROM ${FROM_NUMBER},TO ${TO_NUMBER},$FINAL_STATUS,${FINAL_DURATION_STR},Attempt #$CURRENT_ATTEMPT" >> "$LOGFILE"
  log_trace "CSV logged: $FINAL_STATUS ${FINAL_DURATION_STR} Attempt #$CURRENT_ATTEMPT"
  /opt/call_logs_dashboard/log_outgoing_call.sh "$FROM_NUMBER" "$TO_NUMBER" "$FINAL_STATUS" "$FINAL_DURATION" "$FINAL_TIMESTAMP" "$CURRENT_ATTEMPT"
  watchdog "DB logged: $FINAL_STATUS ${FINAL_DURATION}, Attempt #$CURRENT_ATTEMPT"

  if { [ "$FINAL_STATUS" = "FAILED" ] || [ "$FINAL_STATUS" = "MISSED" ]; } && [[ "$FINAL_DURATION" =~ ^[0-9]+$ ]] && [ "$FINAL_DURATION" -eq 0 ]; then
    SUBJECT="[VOICE CALL] ${TARGET_OPERATOR} ${FINAL_STATUS} - ${FROM_NUMBER} → ${TO_NUMBER}"
    BODY="Call from $FROM_NUMBER to $TO_NUMBER at $FINAL_TIMESTAMP ended with status: $FINAL_STATUS (Duration: $FINAL_DURATION_STR, Attempts: $CURRENT_ATTEMPT)."
    echo "$BODY" | mailx -s "$SUBJECT" "$EMAIL"
    echo "$FINAL_TIMESTAMP,Email sent to $EMAIL - Subject: $SUBJECT" >> "$EMAIL_LOG"
    log_trace "Alert email sent for $FINAL_STATUS, duration $FINAL_DURATION_STR, attempt #$CURRENT_ATTEMPT"
    watchdog "Alert email sent ($FINAL_STATUS)"
  else
    log_trace "Final status=$FINAL_STATUS duration=${FINAL_DURATION_STR}—no email sent"
    watchdog "Final=$FINAL_STATUS ${FINAL_DURATION_STR}—no email"
  fi
fi

# A zero process exit alone does not prove service success.
RESULT_MESSAGE=""
[[ "$FINAL_STATUS" == SUCCESS ]] || RESULT_MESSAGE="CALL test ended with status ${FINAL_STATUS:-UNKNOWN}; see warnings and trace logs for diagnostics."
result_check "$FROM_NUMBER->$TO_NUMBER" "${FINAL_STATUS:-UNKNOWN}" "$RESULT_MESSAGE" "from=$FROM_NUMBER" "to=$TO_NUMBER" "attempts=$CURRENT_ATTEMPT" "duration_or_ring_count=$FINAL_DURATION" "modem_response=${FULL_RESPONSE:-}" || cleanup_and_exit 70
cleanup_and_exit 0

