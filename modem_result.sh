#!/bin/bash
RESULT_KIND=DATA
source "$(dirname -- "${BASH_SOURCE[0]}")/modem_result.sh" || exit 70
#===============================================================================
# Script Name   : check_modem_data.sh
# Version       : 1.8.6
# Purpose       : Validate 4G/LTE data connectivity via AT commands and record
#                 results for the Calls & SMS Dashboard. Sends email alert on
#                 per-operator final failure (subject per user format).
#
# Description   :
#   - Verifies CGATT, CEREG; retrieves PDP IP (CGPADDR)
#   - CPING test (harvest replies & summary; DNS-resolved IP when host target)
#   - Optional HTTP fallback via AT+HTTP*
#   - DNS assist via AT+CDNSGIP when CPING didn’t resolve host IP
#   - Captures radio stats (CSQ/CESQ) on the SAME AT port as data checks
#   - Logs to TRACE, WATCHDOG, CSV (with radio column), and SQLite (radio col)
#   - Multi-operator run with retries (retry only the failing operator)
#   - Sends one email per operator that FAILS on the last attempt (via mailx)
#   - Writes email-sent audit lines to /var/log/emailalerts.log
#
# Author        : Victor James Castigo
# Date Created  : 09-Aug-2025
# Last Modified : 06-Aug-2026
#
# Features      :
#   1) Auto port selection per operator (VIVIFI → ttyUSB2/3; Zero1 → ttyUSB7/8)
#   2) Core checks: CGATT, CEREG, CGPADDR, CPING (min/avg/max), HTTP fallback
#   3) Host targets: show resolved IP (e.g., yahoo.com (98.137.x.x))
#   4) Redundant CPING harvest from incoming handler trace
#   5) Multi-operator: --both or --operators "VIVIFI,Zero1" (order enforced)
#   6) Retry mechanism: --retries N, --backoff-sec B (retry only on FAIL)
#   7) CSV & SQLite include a 'radio' column (human-readables)
#   8) Email alert on final per-operator FAIL; concise subject (via mailx)
#
# Usage         :
#   check_modem_data.sh [OPTIONS]
#
# Options       :
#   --operator <OP>        Run for a single operator (default: VIVIFI)
#   --operators "A,B,..."  Run for multiple operators in order
#   --both                 Convenience: same as --operators "VIVIFI,Zero1"
#   --imei <IMEI>          Informational only (logged in trace)
#   --port <PATH|auto>     AT port to use (default: auto)
#   --target <host|ip>     Ping/CPING target (default: 8.8.8.8)
#   --http | --http-always Force HTTP check (even if ping succeeds)
#   --no-http              Never perform HTTP check
#   --http-url <URL>       HTTP URL for fallback (default: http://example.com)
#   --timeout-sec <N>      Max seconds to wait for CPING block (default: 35)
#   --retries <N>          Number of retry attempts on failure (default: 0)
#   --backoff-sec <N>      Base backoff seconds between retries (default: 2)
#   --email-to <addr>      Email address for alerts; if omitted, use default
#   --verbose | -v         Mirror key logs to console
#
# Defaults      :
#   operator=VIVIFI, port=auto, target=8.8.8.8, HTTP=auto, timeout=35s,
#   pings=3, summary_grace=3s, retries=0, backoff=2s, verbose=off
#
# Outputs / Paths:
#   TRACE_LOG : /var/log/data/data_check_trace.log
#   WATCHDOG  : /var/log/data/check_modem_data_watchdog.log
#   CSV_LOG   : /var/log/data/data_check_log.csv
#   DB_FILE   : /opt/call_logs_dashboard/calls.db  (table: data_logs with radio)
#   INCOMING_LOG (harvest): /var/log/incomingsms-calls/incoming_trace.log
#   EMAIL_LOG : /var/log/emailalerts.log
#   LOCK_FILE : /tmp/modem_lockfile
#===============================================================================

#set -euo pipefail

# ---------- Paths / files ----------
OUT_DIR="/var/log/data"
mkdir -p "$OUT_DIR"

TRACE_LOG="$OUT_DIR/data_check_trace.log"
WATCHDOG="$OUT_DIR/check_modem_data_watchdog.log"
CSV_LOG="$OUT_DIR/data_check_log.csv"

INCOMING_LOG="/var/log/incomingsms-calls/incoming_trace.log"
DB_FILE="/opt/call_logs_dashboard/calls.db"

EMAIL_LOG="/var/log/emailalerts.log"

LOCK_FILE="/tmp/modem_lockfile"

# ---------- Defaults ----------
TARGETS_OPERATORS=""
TARGET_OPERATOR="VIVIFI"
TARGET_IMEI="N/A"
PORT="auto"
TARGET_IP="8.8.8.8"

HTTP_MODE="auto"          # auto | always | skip
HTTP_URL="http://example.com"

PING_SENT=3
CPING_WAIT_MAX=35
CPING_SUMMARY_GRACE=3

RETRIES=0
BACKOFF_SEC=2

# Honor EMAIL env (like call script). Fall back to the default list.
DEFAULT_EMAIL="${EMAIL:-vjcastigo@ip-tribe.com,tribal.eye.alerts@ip-tribe.com}"
EMAIL_TO=""
VERBOSE=0

# ---------- Logging ----------
log_info(){  local m="[$(date '+%Y-%m-%d %H:%M:%S')] [INFO] $*";  echo "$m" >>"$TRACE_LOG"; ((VERBOSE))&&echo "$m"; }
log_error(){ result_diagnostic "$*"; local m="[$(date '+%Y-%m-%d %H:%M:%S')] [ERROR] $*"; echo "$m" >>"$TRACE_LOG"; ((VERBOSE))&&echo "$m" >&2; }
log_send(){  echo "[$(date '+%Y-%m-%d %H:%M:%S')] [SEND] $*" >>"$TRACE_LOG"; }
log_recv(){  echo "[$(date '+%Y-%m-%d %H:%M:%S')] [RECV] $*" >>"$TRACE_LOG"; }
summary(){   local m="[$(date '+%Y-%m-%d %H:%M:%S')] [SUMMARY] $*"; echo "$m" >>"$TRACE_LOG"; ((VERBOSE))&&echo "$m"; }
watchdog(){  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$WATCHDOG"; }

# ---------- Helpers ----------
is_ipv4(){ [[ "$1" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; }
canon_lower(){ echo "$1" | tr '[:upper:]' '[:lower:]'; }

http_status_text(){
  case "$1" in
    0)    echo "HTTP did not check, Ping Success!" ;;
    200)  echo "HTTP 200 OK" ;;
    600)  echo "HTTP 600 - Not HTTP PDU" ;;
    601)  echo "HTTP 601 - Network error (timeout)" ;;
    602)  echo "HTTP 602 - No memory" ;;
    603)  echo "HTTP 603 - DNS error/timeout" ;;
    704)  echo "HTTP 704 - Busy" ;;
    713)  echo "HTTP 713 - Network error" ;;
    714)  echo "HTTP 714 - No memory" ;;
    900)  echo "HTTP: module returned ERROR" ;;
    901)  echo "HTTP: stack not supported" ;;
    *)    echo "HTTP $1 - Unknown" ;;
  esac
}

# Force use of mailx for sending (to match call script) and log to EMAIL_LOG
send_email_mailx(){
  local to="$1" subject="$2" body="$3"
  if ! command -v mailx >/dev/null 2>&1; then
    log_error "mailx not found; cannot send email alert."
    return 1
  fi
  printf "%s" "$body" | mailx -s "$subject" "$to"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Email sent to $to - Subject: $subject" >> "$EMAIL_LOG"
  return 0
}

# ---------- Args ----------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --operator)      TARGET_OPERATOR="${2:-$TARGET_OPERATOR}"; shift 2 ;;
    --operators)     TARGETS_OPERATORS="${2:-}"; shift 2 ;;
    --both)          TARGETS_OPERATORS="VIVIFI,Zero1"; shift ;;
    --imei)          TARGET_IMEI="${2:-$TARGET_IMEI}"; shift 2 ;;
    --port)          PORT="${2:-$PORT}"; shift 2 ;;
    --target)        TARGET_IP="${2:-$TARGET_IP}"; shift 2 ;;
    --http|--http-always) HTTP_MODE="always"; shift ;;
    --no-http)       HTTP_MODE="skip"; shift ;;
    --http-url)      HTTP_URL="${2:-$HTTP_URL}"; shift 2 ;;
    --timeout-sec)   CPING_WAIT_MAX="${2:-$CPING_WAIT_MAX}"; shift 2 ;;
    --retries)       RETRIES="${2:-0}"; shift 2 ;;
    --backoff-sec)   BACKOFF_SEC="${2:-2}"; shift 2 ;;
    --email-to)      EMAIL_TO="${2:-}"; shift 2 ;;
    --verbose|-v)    VERBOSE=1; shift ;;
    *)               shift ;;
  esac
done

# Normalize 'both' wherever it might appear
if [[ -n "$TARGETS_OPERATORS" && "$(canon_lower "$TARGETS_OPERATORS")" == "both" ]]; then
  TARGETS_OPERATORS="VIVIFI,Zero1"
fi
if [[ -n "$TARGET_OPERATOR" && "$(canon_lower "$TARGET_OPERATOR")" == "both" ]]; then
  TARGETS_OPERATORS="VIVIFI,Zero1"
  TARGET_OPERATOR="VIVIFI"
fi

[[ -z "${TARGET_OPERATOR:-}" ]] && TARGET_OPERATOR="VIVIFI"
[[ -z "${TARGET_IP:-}"       ]] && TARGET_IP="8.8.8.8"
[[ -z "${PORT:-}"            ]] && PORT="auto"

# Email recipient
if [[ -z "$EMAIL_TO" ]]; then
  EMAIL_TO="$DEFAULT_EMAIL"
  log_info "Email recipient not provided; using default: $EMAIL_TO"
fi

# ---------- Lock (with stale lock protection) ----------
if [[ -f "$LOCK_FILE" ]]; then
  oldpid="$(cat "$LOCK_FILE" 2>/dev/null || true)"
  if [[ -n "${oldpid:-}" && ! -d "/proc/$oldpid" ]]; then
    rm -f "$LOCK_FILE"
  fi
fi

if ( set -o noclobber; echo "$$" > "$LOCK_FILE") 2>/dev/null; then
  RESULT_OWNS_LOCK=1
else
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] [ERROR] Another run is active (lock present)." >>"$TRACE_LOG"
  exit 2
fi

# Per-run timestamp for initial log lines; per-operator CSV uses its own time
watch_start_ts="$(date '+%Y-%m-%d %H:%M:%S')"
watchdog "Starting data check run at $watch_start_ts"

# ---------- Port selection ----------
pick_port_auto() {
  local op="$1"
  if [[ "$op" == "VIVIFI" ]]; then
    for p in /dev/ttyUSB2 /dev/ttyUSB3; do [[ -c "$p" ]] && echo "$p" && return 0; done
  elif [[ "$op" == "Zero1" ]]; then
    for p in /dev/ttyUSB7 /dev/ttyUSB8; do [[ -c "$p" ]] && echo "$p" && return 0; done
  fi
  for p in /dev/ttyUSB{2,3,7,8} /dev/ttyUSB{0..9}; do
    [[ -c "$p" ]] && echo "$p" && return 0
  done
  return 1
}

# ---------- Final-result exports (per operator) ----------
RES_STATUS=""
RES_PING_RECV=0
RES_HTTP_CODE=0
RES_IP_ADDR=""
RES_TARGET_LABEL=""
RES_RESOLVED_IP=""
RES_PORT=""
RES_CEREG=0
RES_CGATT=0
RES_RTT_TXT=""
RES_RADIO=""

# ---- Final rollup (per-operator) ----
declare -A SUM_STATUS SUM_PING SUM_HTTP SUM_IP SUM_PORT SUM_TGT SUM_ATT SUM_REG

# ---------- Core check for one operator ----------
run_check_for_op() {
  local OP="$1" TGT="$2" REQ_PORT="$3"
  local RUN_TS="$(date '+%Y-%m-%d %H:%M:%S')"   # harvest window start (per op)

  local port="$REQ_PORT"
  if [[ "$port" == "auto" ]]; then
    port="$(pick_port_auto "$OP" || true)"
    [[ -z "$port" ]] && { log_error "[$OP] No AT port found"; return 2; }
  fi

  log_info "Args resolved: OP=$OP IMEI=${TARGET_IMEI:-N/A} PORT=$port TARGET=$TGT"
  watchdog "[$OP] Selected port $port, target $TGT"

  # Serial I/O
  exec 3<>"$port" || { log_error "[$OP] Failed to open $port"; return 2; }
  log_info "Opened port path"

  send_at(){ log_send "$1"; printf "%s\r" "$1" >&3; }
  read_line_timeout(){
    local timeout="$1" line=""
    if IFS= read -r -t "$timeout" -u 3 line; then log_recv "$line"; printf "%s" "$line"; return 0; fi
    return 1
  }

  # State
  local CGATT=0
  local CEREG_REG=0
  local IP_ADDR="N/A"

  local PING_RECV=0
  local RTT_MIN=""; local RTT_MAX=""; local RTT_AVG=""
  local HARVEST_SUMMARY=0
  local HTTP_CODE=0
  local STATUS="FAILED"
  local RESOLVED_IP=""

  # AT session
  send_at "AT";   read_line_timeout 2 >/dev/null || true
  send_at "ATE0"; read_line_timeout 2 >/dev/null || true

  # CGATT?
  send_at "AT+CGATT?"
  for _ in {1..10}; do
    read_line_timeout 1 >/dev/null || true
    local last="$(tail -n1 "$TRACE_LOG")"
    [[ "$last" == *"+CGATT:"* ]] && { CGATT="$(echo "$last" | awk -F': ' '{print $2+0}')"; break; }
  done
  log_info "CGATT=$CGATT"

  # CEREG?
  send_at "AT+CEREG?"
  for _ in {1..10}; do
    read_line_timeout 1 >/dev/null || true
    local last="$(tail -n1 "$TRACE_LOG")"
    if [[ "$last" == *"+CEREG:"* ]]; then
      local stat="$(echo "$last" | awk -F',' '{print $2+0}')"
      [[ "$stat" -eq 1 || "$stat" -eq 5 ]] && CEREG_REG=1 || CEREG_REG=0
      break
    fi
  done
  log_info "CEREG=$CEREG_REG (registered=$CEREG_REG)"

  # IP (CID=1)
  send_at "AT+CGPADDR=1"
  for _ in {1..10}; do
    read_line_timeout 1 >/dev/null || true
    local last="$(tail -n1 "$TRACE_LOG")"
    if [[ "$last" == *"+CGPADDR:"* ]]; then
      IP_ADDR="$(echo "$last" | awk -F',' '{print $2}' | tr -d '\r\n ')"
      [[ -z "$IP_ADDR" ]] && IP_ADDR="N/A"
      break
    fi
  done
  log_info "IP=$IP_ADDR"
  watchdog "[$OP] Attached=$CGATT Reg=$CEREG_REG IP=$IP_ADDR"

  # CPING
  log_info "Step: CPING to $TGT"
  watchdog "[$OP] CPING start to $TGT"
  send_at "AT+CPING=?"
  for _ in {1..10}; do read_line_timeout 1 >/dev/null || true; [[ "$(tail -n1 "$TRACE_LOG")" == *"OK"* ]] && break; done
  local CP_TOUT=1000; local CP_INT=10000
  log_info "CPING caps detected; using tout=${CP_TOUT}ms interval=${CP_INT}ms"
  send_at "AT+CPING=\"$TGT\",1,$PING_SENT,32,$CP_TOUT,$CP_INT,64"

  local CP_START=$(date +%s) got_summary=0 grace_started=0 grace_deadline=0

  harvest_incoming(){
    local start_ts="$RUN_TS" target="$TGT"
    local found_replies=0 found_summary=0 harvest_min= harvest_max= harvest_avg=
    [[ ! -f "$INCOMING_LOG" ]] && { log_info "Incoming log not found: $INCOMING_LOG"; return 0; }

    mapfile -t lines < <(
      awk -v s="$start_ts" '
        /^\[[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}\]/ { ts=substr($0,2,19); if (ts>=s) print $0; }
      ' "$INCOMING_LOG" | grep --fixed-strings '+CPING: ' || true
    )

    local restrict_pat=""
    if is_ipv4 "$target"; then restrict_pat=",$target,"; fi

    for line in "${lines[@]}"; do
      if [[ "$line" =~ \+CPING:\ 1, ]]; then
        if [[ -z "$restrict_pat" || "$line" == *"$restrict_pat"* ]]; then
          ((found_replies++))
          if [[ -z "$RESOLVED_IP" && "$line" == *"+CPING: 1,"*","*","* ]]; then
            RESOLVED_IP="$(sed -n 's/.*+CPING:[[:space:]]*1,[^,]*,\([^,]*\).*/\1/p' <<<"$line" | tr -d '"')"
            [[ "$RESOLVED_IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || RESOLVED_IP=""
          fi
        fi
      elif [[ "$line" =~ \+CPING:\ 3, ]]; then
        found_summary=1
        harvest_min=$(sed -n 's/.*+CPING:[^,]*,[^,]*,[^,]*,[^,]*,\([0-9]\+\).*/\1/p' <<<"$line")
        harvest_max=$(sed -n 's/.*+CPING:[^,]*,[^,]*,[^,]*,[^,]*,[0-9]\+,\([0-9]\+\).*/\1/p' <<<"$line")
        harvest_avg=$(sed -n 's/.*+CPING:[^,]*,[^,]*,[^,]*,[^,]*,[0-9]\+,[0-9]\+,\([0-9]\+\).*/\1/p' <<<"$line")
      fi
    done

    (( found_replies > PING_RECV )) && PING_RECV=$found_replies
    if (( found_summary )); then
      HARVEST_SUMMARY=1
      [[ -n "$harvest_min" ]] && RTT_MIN="$harvest_min"
      [[ -n "$harvest_max" ]] && RTT_MAX="$harvest_max"
      [[ -n "$harvest_avg" ]] && RTT_AVG="$harvest_avg"
    fi

    log_info "Harvested incoming: replies=$found_replies summary=$found_summary min/avg/max=${RTT_MIN:-n/a}/${RTT_AVG:-n/a}/${RTT_MAX:-n/a}"
  }

  while :; do
    local now=$(date +%s)
    read_line_timeout 1 >/dev/null || true
    local last="$(tail -n1 "$TRACE_LOG")"
    watchdog "[$OP] waiting CPING... recv=$PING_RECV"

    if [[ "$last" == *"+CPING: 1,"* ]]; then
      if is_ipv4 "$TGT"; then
        [[ "$last" == *",$TGT,"* ]] && ((PING_RECV++))
      else
        ((PING_RECV++))
        if [[ -z "$RESOLVED_IP" ]]; then
          RESOLVED_IP="$(echo "$last" | awk -F',' '{gsub(/"/,"",$3); print $3}' | tr -d '\r\n ')"
          [[ "$RESOLVED_IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || RESOLVED_IP=""
        fi
      fi
      if (( PING_RECV >= PING_SENT )) && (( grace_started == 0 )); then
        grace_started=1; grace_deadline=$(( now + CPING_SUMMARY_GRACE ))
      fi
    fi

    if [[ "$last" == *"+CPING: 3,"* ]]; then
      RTT_MIN="$(echo "$last" | awk -F',' '{print $(NF-2)}')"
      RTT_MAX="$(echo "$last" | awk -F',' '{print $(NF-1)}')"
      RTT_AVG="$(echo "$last" | awk -F',' '{print $NF}' | tr -d '\r\n ')"
      got_summary=1
    fi

    HARVEST_SUMMARY=0
    harvest_incoming

    (( got_summary == 1 || HARVEST_SUMMARY == 1 )) && break
    (( grace_started == 1 && now >= grace_deadline )) && break
    (( now - CP_START >= CPING_WAIT_MAX )) && break
  done

  # DNS assist if hostname unresolved
  if ! is_ipv4 "$TGT" && [[ -z "$RESOLVED_IP" ]]; then
    log_info "DNS assist: AT+CDNSGIP for $TGT"
    send_at "AT+CDNSGIP=\"$TGT\""
    for _ in {1..6}; do read_line_timeout 1 >/dev/null || true; done
    local dnsl="$(grep -m1 '+CDNSGIP:' "$TRACE_LOG" | tail -n1 || true)"
    if [[ -z "$dnsl" ]]; then dnsl="$(tac "$TRACE_LOG" | grep -m1 '+CDNSGIP:' || true)"; fi
    if [[ -z "$RESOLVED_IP" && -n "$dnsl" ]]; then
      local tryip="$(sed -n 's/.*+CDNSGIP:[^,]*,[^,]*,"\([^"]*\)".*/\1/p' <<<"$dnsl")"
      if [[ "$tryip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ && "$tryip" != "0.0.0.0" ]]; then
        RESOLVED_IP="$tryip"
      fi
    fi
  fi

  # Decide HTTP
  local need_http=0
  case "$HTTP_MODE" in
    always) need_http=1 ;;
    skip)   need_http=0 ;;
    auto)   [[ "$PING_RECV" -eq 0 ]] && need_http=1 || need_http=0 ;;
  esac

  if (( need_http )); then
    log_info "Step: HTTP fallback $HTTP_URL"
    watchdog "[$OP] HTTP check $HTTP_URL"
    send_at "AT+HTTPTERM"; read_line_timeout 1 >/dev/null || true
    send_at "AT+HTTPINIT"; read_line_timeout 2 >/dev/null || true
    local last="$(tail -n1 "$TRACE_LOG")"
    if [[ "$last" == *"ERROR"* ]]; then
      HTTP_CODE=901
    else
      send_at "AT+HTTPPARA=\"CID\",1";              read_line_timeout 2 >/dev/null || true
      send_at "AT+HTTPPARA=\"URL\",\"$HTTP_URL\"";  read_line_timeout 2 >/dev/null || true
      send_at "AT+HTTPACTION=0"
      local http_deadline=$(( $(date +%s) + 12 ))
      while :; do
        read_line_timeout 1 >/dev/null || true
        last="$(tail -n1 "$TRACE_LOG")"
        if [[ "$last" == *"+HTTPACTION:"* ]]; then
          HTTP_CODE="$(echo "$last" | awk -F',' '{print $2+0}')"; break
        fi
        if [[ "$last" == *"ERROR"* ]]; then HTTP_CODE=900; break; fi
        (( $(date +%s) >= http_deadline )) && { HTTP_CODE=601; break; }
      done
      send_at "AT+HTTPTERM"; read_line_timeout 1 >/dev/null || true
    fi
  else
    HTTP_CODE=0
  fi

  # ----- Radio stats (same port FD 3) -----
  log_info "Capturing radio stats (CSQ/CESQ) for diagnostics"

  # CSQ (parse first numeric value after '+CSQ:')
  local CSQ_VAL="99"
  send_at "AT+CSQ"
  for _ in {1..6}; do
    read_line_timeout 1 >/dev/null || true
    local last="$(tail -n1 "$TRACE_LOG")"
    if [[ "$last" == *"+CSQ:"* ]]; then
      CSQ_VAL="$(echo "$last" | awk -F'[:, ]+' '{print $2}')"
      CSQ_VAL="$(echo "$CSQ_VAL" | tr -d '\r\n ')"   # trim CR/LF/spaces
      [[ -z "$CSQ_VAL" ]] && CSQ_VAL="99"
      break
    fi
  done

  # CESQ (take last two numbers: RSRQ index, RSRP index)
  local CESQ_RSRQ="255" CESQ_RSRP="255"
  send_at "AT+CESQ"
  for _ in {1..6}; do
    read_line_timeout 1 >/dev/null || true
    local last="$(tail -n1 "$TRACE_LOG")"
    if [[ "$last" == *"+CESQ:"* ]]; then
      read CESQ_RSRQ CESQ_RSRP < <(echo "$last" | awk -F'[:, ]+' '{print $(NF-1),$NF}')
      CESQ_RSRQ="$(echo "$CESQ_RSRQ" | tr -d '\r\n ')"   # trim
      CESQ_RSRP="$(echo "$CESQ_RSRP" | tr -d '\r\n ')"   # trim
      [[ -z "$CESQ_RSRQ" ]] && CESQ_RSRQ="255"
      [[ -z "$CESQ_RSRP" ]] && CESQ_RSRP="255"
      break
    fi
  done

  # Map to human-friendly units
  local RSSI_DBM="Unknown" RSRP_DBM="Unknown" RSRQ_DB="Unknown"

  # RSSI from CSQ: 0..31 => -113 + 2*n dBm; 99 => unknown
  if [[ "$CSQ_VAL" =~ ^[0-9]+$ && "$CSQ_VAL" -ne 99 ]]; then
    RSSI_DBM="$(( -113 + 2*CSQ_VAL )) dBm"
  fi

  # RSRP: 0..97 => -140..-43 dBm; else unknown
  if [[ "$CESQ_RSRP" =~ ^[0-9]+$ && "$CESQ_RSRP" -ge 0 && "$CESQ_RSRP" -le 97 ]]; then
    RSRP_DBM="$(( CESQ_RSRP - 140 )) dBm"
  fi

  # RSRQ: 0..34 => -20..-3 dB in 0.5 dB steps; else unknown
  if [[ "$CESQ_RSRQ" =~ ^[0-9]+$ && "$CESQ_RSRQ" -ge 0 && "$CESQ_RSRQ" -le 34 ]]; then
    RSRQ_DB="$(awk -v v="$CESQ_RSRQ" 'BEGIN{ printf "%.1f dB", (-20 + v*0.5) }')"
  fi

  # Strength/Quality labels – prefer RSRP if available, else fall back to RSSI
  classify_strength(){
    local v="$1"  # numeric negative dBm (e.g. -95)
    if (( v >= -90 )); then echo "Excellent"
    elif (( v >= -105 )); then echo "Good"
    elif (( v >= -115 )); then echo "Fair"
    else echo "Poor"; fi
  }
  classify_quality(){
    local q="$1"  # numeric negative dB (e.g. -14.5)
    awk -v q="$q" 'BEGIN{
      if (q> -10) print "Good";
      else if (q> -12) print "Fair";
      else print "Poor";
    }'
  }

  local STRENGTH_TXT="Unknown" QUALITY_TXT="Unknown"
  if [[ "$RSRP_DBM" != "Unknown" ]]; then
    local r=$(echo "$RSRP_DBM" | awk '{print $1}')
    STRENGTH_TXT="$(classify_strength "$r")"
  elif [[ "$RSSI_DBM" != "Unknown" ]]; then
    local r=$(echo "$RSSI_DBM" | awk '{print $1}')
    # RSSI-based thresholds (a bit stricter)
    if (( r >= -70 )); then STRENGTH_TXT="Excellent"
    elif (( r >= -85 )); then STRENGTH_TXT="Good"
    elif (( r >= -100 )); then STRENGTH_TXT="Fair"
    else STRENGTH_TXT="Poor"; fi
  fi
  if [[ "$RSRQ_DB" != "Unknown" ]]; then
    local q=$(echo "$RSRQ_DB" | awk '{print $1}')
    QUALITY_TXT="$(classify_quality "$q")"
  fi

  RES_RADIO="[RADIO] RSSI: ${RSSI_DBM}; Strength: ${STRENGTH_TXT} (RSRP ${RSRP_DBM}); Quality: ${QUALITY_TXT} (RSRQ ${RSRQ_DB})"
  log_info "$RES_RADIO"

  # ----- Decide overall result -----
  if (( PING_RECV > 0 )); then STATUS="SUCCESS"
  else
    [[ $need_http -eq 1 && "$HTTP_CODE" -eq 200 ]] && STATUS="SUCCESS" || STATUS="FAILED"
  fi

  local loss=$(( PING_SENT - PING_RECV ))
  local rtt_human="n/a"
  [[ -n "$RTT_MIN" && -n "$RTT_AVG" && -n "$RTT_MAX" ]] && rtt_human="${RTT_MIN}/${RTT_AVG}/${RTT_MAX} ms"

  local target_label="$TGT"
  if ! is_ipv4 "$TGT"; then
    if [[ -n "$RESOLVED_IP" ]]; then target_label="$TGT ($RESOLVED_IP)"
    else target_label="$TGT (unable to resolve IP)"; fi
  fi

  local http_text
  if (( need_http )); then http_text="$(http_status_text "$HTTP_CODE")"
  elif [[ "$HTTP_MODE" == "always" ]]; then http_text="$(http_status_text "$HTTP_CODE")"
  elif (( PING_RECV > 0 )); then http_text="HTTP skipped, ping OK!"
  else http_text="HTTP not performed"; fi

  summary "Operator=$OP; Port=$port; Target=$target_label; IP=$IP_ADDR; Ping sent=$PING_SENT recv=$PING_RECV loss=$loss; RTT(min/avg/max)=$rtt_human; $http_text; Result=$STATUS"
  summary "  $RES_RADIO"
  watchdog "[$OP] Result=$STATUS ping=$PING_RECV/$PING_SENT http=$HTTP_CODE"

  # Export results for wrapper
  RES_STATUS="$STATUS"
  RES_PING_RECV="$PING_RECV"
  RES_HTTP_CODE="$HTTP_CODE"
  RES_IP_ADDR="$IP_ADDR"
  RES_TARGET_LABEL="$target_label"
  RES_RESOLVED_IP="$RESOLVED_IP"
  RES_PORT="$port"
  RES_CEREG="$CEREG_REG"
  RES_CGATT="$CGATT"
  RES_RTT_TXT="$rtt_human"

  # Cleanup
  exec 3>&-
  log_info "Closing port $port"
  [[ "$STATUS" == "SUCCESS" ]] && return 0 || return 1
}

# ---------- Retry wrapper: retry only failing operator; log CSV/DB once ----------
run_with_retries() {
  local OP="$1" TGT="$2" REQ_PORT="$3"

  local attempt=0
  local rc=2

  while :; do
    attempt=$((attempt+1))
    log_info "==== [$OP] Attempt $attempt ===="
    watchdog "[$OP] Attempt $attempt"

    RES_STATUS="FAILED"; RES_PING_RECV=0; RES_HTTP_CODE=0
    RES_IP_ADDR=""; RES_TARGET_LABEL="$TGT"; RES_RESOLVED_IP=""; RES_PORT="$REQ_PORT"
    RES_CEREG=0; RES_CGATT=0; RES_RTT_TXT=""; RES_RADIO=""
    run_check_for_op "$OP" "$TGT" "$REQ_PORT"
    rc=$?

    if [[ $rc -eq 0 ]]; then
      log_info "[$OP] Success on attempt $attempt"
      break
    fi

    if (( attempt >= RETRIES + 1 )); then
      log_error "[$OP] Exhausted retries ($RETRIES)."
      break
    fi

    log_info "[$OP] Recovery: brief wait then retry"
    sleep $(( BACKOFF_SEC * attempt ))
  done

  # Log to CSV/DB exactly once per operator (final outcome)
  local ROW_TS="$(date '+%Y-%m-%d %H:%M:%S')"

  # Escape for CSV (double quotes) and SQL (single quotes)
  local RADIO_CSV="${RES_RADIO//\"/\"\"}"
  local RADIO_SQL="${RES_RADIO//\'/\'\'}"

  # CSV with dedicated radio column (quoted)
  printf "%s,DATA CHECK,%s,%s,%s,IP=%s REG=%d ATT=%d PING=%d HTTP=%d,\"%s\"\n" \
    "$ROW_TS" "$OP" "$TGT" "$RES_STATUS" \
    "$RES_IP_ADDR" "$RES_CEREG" "$RES_CGATT" "$RES_PING_RECV" "$RES_HTTP_CODE" \
    "$RADIO_CSV" >> "$CSV_LOG"
  log_info "CSV logged: $(tail -n1 "$CSV_LOG")"

  # DB insert includes radio column
  if sqlite3 "$DB_FILE" \
    "INSERT INTO data_logs (timestamp,data_type,operator_name,destination,result,radio)
     VALUES ('$ROW_TS','DATA CHECK','$OP','$TGT','$RES_STATUS','$RADIO_SQL');" >/dev/null 2>>"$TRACE_LOG"
  then
    log_info "DB row inserted (DATA CHECK, $OP, $TGT, $RES_STATUS, radio=$(echo "$RES_RADIO" | tr '\n' ' ' | cut -c1-80)...)"
    echo "Logged to DB: $ROW_TS | DATA CHECK | $OP -> $RES_TARGET_LABEL | $RES_STATUS | IP=$RES_IP_ADDR PING=$RES_PING_RECV/$PING_SENT"
  else
    log_error "sqlite insert failed"
  fi

  # Email alert if FAILED on final attempt (via mailx) + add line to EMAIL_LOG
  if [[ "$RES_STATUS" == "FAILED" ]]; then
    # Subject wants: [DATA CHECK] <OP> FAILED - <host-or-ip>( [resolved_ip])
    local subject_target="$TGT"
    if ! is_ipv4 "$TGT" && [[ -n "$RES_RESOLVED_IP" ]]; then
      subject_target="$TGT ($RES_RESOLVED_IP)"
    fi
    local SUBJECT="[DATA CHECK] $OP FAILED - $subject_target"

    local HTTP_TXT="$(http_status_text "$RES_HTTP_CODE")"
    local BODY=$(
      cat <<EOF
Operator : $OP
Result   : FAILED (after $attempt attempt(s))
Time     : $ROW_TS

Target   : $RES_TARGET_LABEL
Port     : $RES_PORT
PDP IP   : $RES_IP_ADDR
Attach   : CGATT=$RES_CGATT
Reg      : CEREG=$RES_CEREG

Ping     : sent=$PING_SENT recv=$RES_PING_RECV
RTT      : $RES_RTT_TXT
HTTP     : $HTTP_TXT (code=$RES_HTTP_CODE)

Radio    : $RES_RADIO

EOF
    )
    if send_email_mailx "$EMAIL_TO" "$SUBJECT" "$BODY"; then
      log_info "Failure email sent for $OP to $EMAIL_TO"
      watchdog "[$OP] Failure email sent"
    else
      log_error "Failed to send email for $OP"
      watchdog "[$OP] Email send FAILED"
    fi
  fi

  # Store for final rollup
  SUM_STATUS["$OP"]="$RES_STATUS"
  SUM_PING["$OP"]="$RES_PING_RECV/$PING_SENT"
  SUM_HTTP["$OP"]="$RES_HTTP_CODE"
  SUM_IP["$OP"]="$RES_IP_ADDR"
  SUM_PORT["$OP"]="$RES_PORT"
  SUM_TGT["$OP"]="$RES_TARGET_LABEL"
  SUM_ATT["$OP"]="$RES_CGATT"
  SUM_REG["$OP"]="$RES_CEREG"

  local result_status="SUCCESS" result_message=""
  if [[ $rc -ne 0 ]]; then
    result_status="FAILED"
    result_message="Data check failed for $OP: port=$RES_PORT ping=$RES_PING_RECV/$PING_SENT HTTP=$RES_HTTP_CODE ATT=$RES_CGATT REG=$RES_CEREG (rc=$rc)"
  fi
  result_check "$OP" "$result_status" "$result_message" "target=$TGT" "port=$RES_PORT" \
    "ping_received=$RES_PING_RECV" "http_code=$RES_HTTP_CODE" "attempts=$attempt" || exit 70
  return $rc
}

# ---------- Orchestration ----------
# Build operators list
ops=()
if [[ -n "$TARGETS_OPERATORS" ]]; then
  IFS=',' read -r -a ops <<< "$TARGETS_OPERATORS"
else
  ops=("$TARGET_OPERATOR")
fi

# Canonicalize and enforce order
canon_ops=()
for op in "${ops[@]}"; do
  op_trim="$(echo "$op" | xargs)"
  [[ -z "$op_trim" ]] && continue
  case "$(canon_lower "$op_trim")" in
    vivifi) canon_ops+=("VIVIFI") ;;
    zero1)  canon_ops+=("Zero1") ;;
    both)   canon_ops=("VIVIFI" "Zero1") ;;
    *)      canon_ops+=("$op_trim") ;;
  esac
done

have_v=0; have_z=0
for op in "${canon_ops[@]}"; do
  [[ "$op" == "VIVIFI" ]] && have_v=1
  [[ "$op" == "Zero1"  ]] && have_z=1
done
if (( have_v==1 && have_z==1 )); then
  order_ops=("VIVIFI" "Zero1")
else
  order_ops=("${canon_ops[@]}")
fi

log_info "Lock file created"

# If multi-operator and a single explicit port was given, force per-op auto
REQ_PORT_FOR_RUN="$PORT"
if (( ${#order_ops[@]} > 1 )) && [[ "$PORT" != "auto" ]]; then
  log_info "Multi-operator run detected; overriding --port to auto for per-op selection"
  REQ_PORT_FOR_RUN="auto"
fi

# Run each operator with its own retries
for op in "${order_ops[@]}"; do
  log_info "=== Starting check for operator $op ==="
  run_with_retries "$op" "$TARGET_IP" "$REQ_PORT_FOR_RUN" || true
done

# ---- Final summary across operators ----
if ((${#order_ops[@]})); then
  summary "Final results:"
  for op in "${order_ops[@]}"; do
    http_text="$(http_status_text "${SUM_HTTP[$op]:-0}")"
    summary "  OP=$op; Result=${SUM_STATUS[$op]:-N/A}; Target=${SUM_TGT[$op]:-N/A}; IP=${SUM_IP[$op]:-N/A}; Port=${SUM_PORT[$op]:-N/A}; Ping=${SUM_PING[$op]:-0/0}; ATT=${SUM_ATT[$op]:-0}; REG=${SUM_REG[$op]:-0}; ${http_text}"
    watchdog "[FINAL] $op => ${SUM_STATUS[$op]:-N/A}; ping=${SUM_PING[$op]:-0/0}; http=${SUM_HTTP[$op]:-0}"
  done
fi

watchdog "Data check run finished"
exit 0
