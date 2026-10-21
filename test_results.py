#!/bin/bash
RESULT_KIND=SMS
source "$(dirname -- "${BASH_SOURCE[0]}")/modem_result.sh" || exit 70
#===============================================================================
# Script Name   : smsmodem2TOmodem1.sh
# Version       : 1.7.6
# Description   : Sends an SMS using dynamic modem discovery (mmcli).
#                 Chooses the other/free AT port (not used by incoming handler),
#                 supports friendly names for --from/--to (VIVIFI, Zero1),
#                 retries up to 3 times on FAILED/UNKNOWN, and logs only the
#                 FINAL attempt to CSV/DB (email on final FAILED).
#
# Author        : Victor James Castigo
# Date Created  : 25 July 2025
# Last Modified : 10 October 2025
#
# Friendly names mapping:
#   FROM=VIVIFI  -> operator=VIVIFI, IMEI=860537062221411, FROM_NUMBER=88603568
#   FROM=Zero1   -> operator=Zero1,  IMEI=860537061206173, FROM_NUMBER=89297580
#   TO  =VIVIFI  -> TO_NUMBER=88603568
#   TO  =Zero1   -> TO_NUMBER=89297580
#
# Usage:
#   smsmodem2TOmodem1.sh --from VIVIFI --to Zero1 --message "Automated SMS"
#   smsmodem2TOmodem1.sh --from Zero1  --to 96391124 --message "Ping"
#   (Still supports advanced flags: --operator, --imei, --port, explicit numbers)
#
# Requirements:
#   - ModemManager (mmcli) installed and running
#   - Incoming handler state file: /run/incominghandler/incominghandler_<IMEI>.state
#   - External script: /opt/call_logs_dashboard/log_outgoing_sms.sh
#   - Email (mailx) configured
#
# Exit Codes:
#   0 - Completed (success or final handled failure)
#   1 - Lock timeout or modem/port error
#===============================================================================

# Keep 'set -euo pipefail' OFF to avoid premature exit on serial timeouts.
umask 077

#=============================
# SETUP AND CONFIGURATION
#=============================
LOCK_FILE="/tmp/modem_lockfile"

MODEM_RESPONSE_FILE="$(mktemp /tmp/sms_modem_response.XXXXXX)" || {
    echo "Failed to create temporary modem response file" >&2
    exit 1
}

MAX_RETRIES_LOCK=10
RETRY_INTERVAL_LOCK=15

# Retry behavior for SMS
ATTEMPTS_MAX=2
SLEEP_BETWEEN_ATTEMPTS=5

# Logs
OUT_DIR="/var/log/outgoingsms-calls"
mkdir -p "$OUT_DIR" 2>/dev/null || true
TRACE_LOG="$OUT_DIR/outgoing_trace.log"
LOGFILE="$OUT_DIR/outgoing_sms_log.csv"
WATCHDOG="$OUT_DIR/smsmodem2TOmodem1_watchdog.log"
EMAIL_LOG="/var/log/emailalerts.log"

# Email
EMAIL="${EMAIL:-vjcastigo@ip-tribe.com,tribal.eye.alerts@ip-tribe.com}"

# Defaults (can be overridden by friendly names, args, or env)
TARGET_OPERATOR="${TARGET_OPERATOR:-VIVIFI}"
TARGET_IMEI="${TARGET_IMEI:-860537062221411}"
FROM_NUMBER="${FROM_NUMBER:-88603568}"
DEFAULT_TO="${DEFAULT_TO:-89297580}"
DEFAULT_MESSAGE=""

#=============================
# LOGGING FUNCTIONS
#=============================
ts() { date '+%Y-%m-%d %H:%M:%S'; }
watchdog()  { echo "[$(ts)] $1" >> "$WATCHDOG"; }
log_trace() { echo -e "[$(ts)] [INFO] $1"  >> "$TRACE_LOG"; }
log_send()  { echo -e "[$(ts)] [SEND] $1"  >> "$TRACE_LOG"; }
log_recv()  { while IFS= read -r line; do echo -e "[$(ts)] [RECV] $line" >> "$TRACE_LOG"; done; }
log_error() { result_diagnostic "$1"; echo -e "[$(ts)] [ERROR] $1" >> "$TRACE_LOG"; }

read_modem_response() {
    local timeout_seconds="$1"

    : > "$MODEM_RESPONSE_FILE"

    timeout "$timeout_seconds" cat <&3 \
        | tee "$MODEM_RESPONSE_FILE" \
        | log_recv

    cat "$MODEM_RESPONSE_FILE"
}

cleanup_and_exit() {
    local exit_code="${1:-0}"

    exec 3>&- 3<&- 2>/dev/null || true
    rm -f "$LOCK_FILE" 2>/dev/null || true
    rm -f "$MODEM_RESPONSE_FILE" 2>/dev/null || true

    log_trace "Lock and temporary modem response file removed"
    watchdog "SMS script completed and temporary files removed."

    exit "$exit_code"
}

#=============================
# ARGUMENT PARSING
#=============================
TO_NUMBER="$DEFAULT_TO"
SMS_TEXT_RAW="$DEFAULT_MESSAGE"
PORT=""  # if provided, bypass discovery

FROM_FRIENDLY=""
TO_FRIENDLY=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --to)
            shift
            TO_FRIENDLY="$1"
            shift
            ;;
        --from)
            shift
            FROM_FRIENDLY="$1"
            shift
            ;;
        --message)  SMS_TEXT_RAW="$2"; shift 2 ;;
        --port)     PORT="$2"; shift 2 ;;
        --operator) TARGET_OPERATOR="$2"; shift 2 ;;
        --imei)     TARGET_IMEI="$2"; shift 2 ;;
        *)
            echo "Unknown option or missing value: $1"
            echo "Usage: $0 --from <VIVIFI|Zero1> --to <VIVIFI|Zero1|number> [--message <text>] [--port </dev/ttyUSBx>] [--operator <name>] [--imei <id>]"
            exit 1 ;;
    esac
done

#=============================
# FRIENDLY NAME RESOLUTION
#=============================
to_upper() { echo "$1" | tr '[:lower:]' '[:upper:]'; }

resolve_from() {
    local name_uc
    name_uc="$(to_upper "$1")"
    case "$name_uc" in
        VIVIFI)
            TARGET_OPERATOR="VIVIFI"
            TARGET_IMEI="860537062221411"
            FROM_NUMBER="88603568"
            ;;
        ZERO1)
            TARGET_OPERATOR="Zero1"
            TARGET_IMEI="860537061206173"
            FROM_NUMBER="89297580"
            ;;
        *)
            log_error "Unknown --from '$1' (use VIVIFI or Zero1)"
            echo "ERR"; return 1 ;;
    esac
    echo "OK"; return 0
}

resolve_to() {
    local val="$1"
    local name_uc
    name_uc="$(to_upper "$val")"
    case "$name_uc" in
        VIVIFI) TO_NUMBER="88603568" ;;
        ZERO1)  TO_NUMBER="89297580" ;;
        *)
            if [[ "$val" =~ ^[0-9]+$ ]]; then
                TO_NUMBER="$val"
            else
                log_error "Unknown --to '$val' (use VIVIFI, Zero1, or a number)"
                echo "ERR"; return 1
            fi
            ;;
    esac
    echo "OK"; return 0
}

# If friendly names provided, they TAKE PRECEDENCE over explicit --operator/--imei
if [[ -n "$FROM_FRIENDLY" ]]; then
    resolve_from "$FROM_FRIENDLY" >/dev/null || exit 1
fi
if [[ -n "$TO_FRIENDLY" ]]; then
    resolve_to "$TO_FRIENDLY" >/dev/null || exit 1
fi

log_trace "Args resolved: OP=$TARGET_OPERATOR IMEI=$TARGET_IMEI FROM=$FROM_NUMBER TO=$TO_NUMBER PORT=${PORT:-auto}"
watchdog "SMS target: TO=$TO_NUMBER FROM=$FROM_NUMBER (OP=$TARGET_OPERATOR IMEI=$TARGET_IMEI)"

#=============================
# DISCOVERY (MODEM + FREE/OTHER AT PORT)
#=============================
discover_free_port() {
    # Respect explicit PORT override
    if [[ -n "$PORT" ]]; then
        log_trace "PORT override provided: $PORT"
        [[ -e "$PORT" ]] || { log_error "Provided PORT $PORT does not exist"; return 1; }
        echo "$PORT"; return 0
    fi

    command -v mmcli >/dev/null 2>&1 || { log_error "mmcli not found"; return 1; }

    local list out id operator imei equip
    local -a at_ports
    list=$(mmcli -L 2>/dev/null) || { log_error "mmcli -L failed"; return 1; }

    # Collect modem IDs
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

        # Parse operator and IMEI (covers '3GPP | imei' or 'equipment id' variants)
        operator=$(echo "$out" | awk -F': *' '/operator name/ {print $2}' | xargs | tail -n1)
        imei=$(echo     "$out" | awk -F': *' '/^[[:space:]]*3GPP[[:space:]]*\|[[:space:]]*imei/ {print $2}' | xargs | tail -n1)
        equip=$(echo    "$out" | awk -F': *' '/equipment id/ {print $2}' | xargs | tail -n1)

        mapfile -t at_ports < <(echo "$out" | grep -oE 'tty(USB|ACM)[0-9]+[[:space:]]*\(at\)' | awk '{print $1}' | sort -V)
        log_trace "Discovery: Modem $id operator=${operator:-N/A} imei=${imei:-$equip:-N/A} at_ports=${at_ports[*]:-none}"

        if [[ "$operator" == "$TARGET_OPERATOR" || "$imei" == "$TARGET_IMEI" || "$equip" == "$TARGET_IMEI" ]]; then
            chosen_id="$id"
            break
        fi
    done

    [[ -z "$chosen_id" ]] && { log_error "No modem matched operator=$TARGET_OPERATOR or IMEI=$TARGET_IMEI"; return 1; }
    [[ ${#at_ports[@]} -eq 0 ]] && { log_error "No AT ports on modem $chosen_id"; return 1; }

    # ---------- FAST PATH: use the “other” AT port than incoming handler ----------
    local state_file="/run/incominghandler/incominghandler_${TARGET_IMEI}.state"
    if [[ -f "$state_file" ]]; then
        # shellcheck disable=SC1090
        source "$state_file" || true
        if [[ -n "${PORT:-}" ]]; then
            local inbound_port other=""
            inbound_port="$(basename "$PORT")"
            for p in "${at_ports[@]}"; do
                if [[ "$p" != "$inbound_port" ]]; then other="$p"; break; fi
            done
            if [[ -n "$other" ]]; then
                local other_dev="/dev/$other"
                if ! command -v fuser >/dev/null 2>&1 || ! fuser -s "$other_dev"; then
                    log_trace "Fast-path select (other than inbound): $other_dev"
                    echo "$other_dev"; return 0
                else
                    log_trace "Fast-path other port busy: $other_dev; will probe alternatives"
                fi
            else
                log_trace "Fast-path could not find 'other' port (single AT port?)"
            fi
        else
            log_trace "State file present but PORT unset; skipping fast-path"
        fi
    else
        log_trace "No state file $state_file; skipping fast-path"
    fi

    # ---------- SLOW PATH: probe remaining AT ports ----------
    local dev line
    for p in "${at_ports[@]}"; do
        dev="/dev/$p"
        if command -v fuser >/dev/null 2>&1 && fuser -s "$dev"; then
            log_trace "Port busy: $dev"; continue
        fi
        if exec 3<> "$dev"; then
            timeout 0.5 cat <&3 >/dev/null 2>&1 || true
            echo -e "AT\r" >&3
            log_send "AT (probe $dev)"
            line=""
            read -t 2 -r line <&3 || line=""
            [[ -n "$line" ]] && log_recv "$line"
            exec 3>&- 3<&-   # close after probe
            if echo "$line" | grep -qi "OK"; then
                log_trace "Selected free/probed port: $dev"
                echo "$dev"; return 0
            else
                log_trace "Probe failed/no OK on $dev; trying next"
            fi
        else
            log_trace "Cannot open $dev; skipping"
        fi
    done

    # Fallback: take the first AT port if nothing probed OK
    dev="/dev/${at_ports[0]}"
    log_trace "Fallback to first AT port: $dev"
    echo "$dev"
    return 0
}

PORT="$(discover_free_port)" || { log_error "Port discovery failed"; cleanup_and_exit 1; }
log_trace "Using PORT=$PORT (FROM=$FROM_NUMBER TO=$TO_NUMBER)"
watchdog "Sending SMS via $PORT FROM=$FROM_NUMBER TO=$TO_NUMBER"

#=============================
# SIGNAL TRAPS
#=============================
trap 'log_trace "[TRAP] SIGTERM received, cleaning lock"; watchdog "[TRAP] SIGTERM"; cleanup_and_exit 1' TERM
trap 'log_trace "[TRAP] SIGINT received, cleaning lock";  watchdog "[TRAP] SIGINT";  cleanup_and_exit 1' INT

#=============================
# LOCKING MECHANISM
#=============================
sleep 1
log_trace "Waiting 1 second before SMS start"
watchdog "SMS script started, sleeping 1 second"

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
# ATTEMPT LOOP (up to 3)
#=============================
FINAL_STATUS=""
FINAL_TIMESTAMP=""
ATTEMPT=1

# Prepare message once (same text across attempts)
BASE_TIMESTAMP="$(ts)"
if [ -z "$SMS_TEXT_RAW" ]; then
    SMS_TEXT="$BASE_TIMESTAMP Automated SMS from $FROM_NUMBER to $TO_NUMBER"
else
    SMS_TEXT="$SMS_TEXT_RAW"
fi

send_sms_once() {
    # Open port fresh for each attempt
    if [ ! -e "$PORT" ]; then
        log_error "Modem port $PORT not found"
        echo "PORT_MISSING"; return 1
    fi

    exec 3<> "$PORT" || { log_error "Failed to open $PORT"; echo "OPEN_FAIL"; return 1; }
    log_trace "Opened port $PORT"
    watchdog "Opened $PORT for SMS attempt #$ATTEMPT"

    # Flush any garbage from modem
    timeout 1 cat <&3 > /dev/null 2>&1 || true
    log_trace "Flushed modem buffer"

    # Quiet echo, enable SMS text mode
    printf "ATE0\r"      >&3; log_send "ATE0";      read_modem_response 2 >/dev/null
    printf "AT+CMGF=1\r" >&3; log_send "AT+CMGF=1"; read_modem_response 3 >/dev/null

    # Issue CMGS
    local cmd="AT+CMGS=\"$TO_NUMBER\""
    echo -e "$cmd\r" >&3
    log_send "$cmd"
    read_modem_response 5 >/dev/null
    log_trace "Got response to CMGS (expect '>'): proceeding to send body"

    # Send body + Ctrl+Z
    echo -e "$SMS_TEXT\x1A\r" >&3
    log_send "Message text sent: $SMS_TEXT"

    # Final response
    local resp
    resp="$(read_modem_response 5)"
    log_trace "Final modem response received for attempt #$ATTEMPT"

    # Close port for this attempt
    exec 3>&- 3<&- 2>/dev/null || true
    log_trace "Closed port $PORT after attempt #$ATTEMPT"

    # Evaluate status

if grep -qiE '\+CMS ERROR|ERROR' <<< "$resp"; then
    log_error "SMS modem rejected submission: $resp"
    echo "FAILED"
    return 0
elif grep -qi '\+CMGS:' <<< "$resp"; then
    echo "SUCCESS"
    return 0
elif grep -qi 'OK' <<< "$resp"; then
    log_error "SMS response contained OK without +CMGS; submission unconfirmed"
    echo "UNKNOWN"
    return 0
else
    log_error "Unrecognized final SMS response: $(printf '%q' "$resp")"
    echo "UNKNOWN"
    return 0
fi
}

while [ $ATTEMPT -le $ATTEMPTS_MAX ]; do
    ATTEMPT_TS="$(ts)"
    log_trace "----- Outgoing SMS attempt #$ATTEMPT at $ATTEMPT_TS -----"
    watchdog "Attempt #$ATTEMPT start $ATTEMPT_TS"

    STATUS="$(send_sms_once)"
    FINAL_STATUS="$STATUS"
    FINAL_TIMESTAMP="$ATTEMPT_TS"

    if [ "$STATUS" = "SUCCESS" ]; then
        log_trace "Attempt #$ATTEMPT SUCCESS — stopping retries"
        watchdog "SMS SUCCESS on attempt #$ATTEMPT"
        break
    fi

    if [ $ATTEMPT -lt $ATTEMPTS_MAX ]; then
        log_trace "Attempt #$ATTEMPT status=$STATUS; retrying after ${SLEEP_BETWEEN_ATTEMPTS}s"
        watchdog "Retrying after ${SLEEP_BETWEEN_ATTEMPTS}s (status=$STATUS)"
        sleep "$SLEEP_BETWEEN_ATTEMPTS"
    else
        log_trace "Max attempts reached with status=$STATUS"
        watchdog "Final status after retries: $STATUS"
    fi

    ATTEMPT=$((ATTEMPT + 1))
done

#=============================
# FINAL LOGGING (CSV/DB/EMAIL)
#=============================
# Only FINAL attempt is logged to CSV and DB:
echo "$FINAL_TIMESTAMP,OUTGOING,FROM $FROM_NUMBER,$FINAL_STATUS,\"$SMS_TEXT\",TO $TO_NUMBER" >> "$LOGFILE"
log_trace "CSV logged final: status=$FINAL_STATUS at $FINAL_TIMESTAMP"

if ! /opt/call_logs_dashboard/log_outgoing_sms.sh "$FROM_NUMBER" "$SMS_TEXT" "$TO_NUMBER" "$FINAL_TIMESTAMP" "$FINAL_STATUS"; then
    log_error "DB log script failed"
else
    watchdog "DB logged final status: $FINAL_STATUS"
fi

if [ "$FINAL_STATUS" = "FAILED" ]; then
    SUBJECT="[SMS Send] $TARGET_OPERATOR $FINAL_STATUS (after $ATTEMPTS_MAX attempts)"
    BODY="SMS from $FROM_NUMBER to $TO_NUMBER failed at $FINAL_TIMESTAMP (final status: $FINAL_STATUS)."
    echo "$BODY" | mailx -s "$SUBJECT" "$EMAIL"
    echo "$(ts),Email sent to $EMAIL - Subject: $SUBJECT" >> "$EMAIL_LOG"
    log_trace "Alert email sent for final FAILED"
    watchdog "Alert email sent (final FAILED)"
else
    log_trace "Final status=$FINAL_STATUS — no alert required"
fi

#=============================
# CLEANUP
#===========================# A zero process exit alone does not prove service success.
RESULT_MESSAGE=""
[[ "$FINAL_STATUS" == SUCCESS ]] || RESULT_MESSAGE="SMS test ended with status ${FINAL_STATUS:-UNKNOWN}; see warnings and trace logs for diagnostics."
result_check "$FROM_NUMBER->$TO_NUMBER" "${FINAL_STATUS:-UNKNOWN}" "$RESULT_MESSAGE" "from=$FROM_NUMBER" "to=$TO_NUMBER" "attempts=$(( ATTEMPT > ATTEMPTS_MAX ? ATTEMPTS_MAX : ATTEMPT ))" || cleanup_and_exit 70
cleanup_and_exit 0t 0

