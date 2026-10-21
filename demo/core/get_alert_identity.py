
"""Conservative email alert correlation. Standard library only.

Email-only keys are candidate condition matches, not proof of a shared root cause.
Keys identify conditions, not root causes/incidents or individual emails.
Prefer supplying source-native metadata from webhooks/APIs whenever available.
Unrecognized formats produce no grouping keys rather than guessing.
"""
from dataclasses import dataclass, asdict
from hashlib import sha256
import html
import json
import re
import unicodedata


@dataclass(frozen=True)
class AlertIdentity:
    correlation_key: str
    source: str
    identity: dict
    confidence: str
    automatic_grouping: bool
    explanation: str


def _digest(value):
    data = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return sha256(data.encode('utf-8')).hexdigest()


def _text(value):
    return unicodedata.normalize('NFKC', html.unescape(str(value or ''))).replace('\r\n', '\n')


def _name(value):
    return re.sub(r'\s+', ' ', _text(value)).strip().casefold()


def _rule_name(value):
    # Only known severity/state decorations; preserve codes, thresholds and resources.
    value = re.sub(r'^\[(?:critical|major|warning|resolved|firing(?::\d+)?)\]\s*', '', value, flags=re.I)
    value = re.sub(r'\s*(?:-\s*)?\b(?:critical|major|warning)\s*$', '', value, flags=re.I)
    return _name(value)


def _make(namespace, source, identity, confidence, automatic, explanation):
    key = 'alert:v1:' + _digest({'namespace': namespace, 'source': source, 'identity': identity})
    return AlertIdentity(key, source, identity, confidence, automatic, explanation)


def _labels(body):
    """Extract individual Prometheus (=) and Grafana (alternating lines) label blocks."""
    blocks = re.findall(r'^\s*Labels\s*\n(.*?)(?=^\s*(?:Annotations|Silence|Source|Values|Summary|Firing|Resolved)\s*$|\Z)',
                        body, flags=re.M | re.S)
    result = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        labels = {}
        i = 0
        while i < len(lines):
            pair = re.fullmatch(r'([\w.-]+)\s*=\s*(.+)', lines[i])
            if pair:
                labels[pair[1]] = pair[2].strip('"')
                i += 1
            elif re.fullmatch(r'[\w.-]+', lines[i]) and i + 1 < len(lines):
                labels[lines[i]] = lines[i + 1]
                i += 2
            else:
                i += 1
        if labels.get('alertname'):
            result.append(labels)
    return result


def generate_alert_identifiers(email: dict, *, source_namespace: str,
                               metadata: dict | None = None) -> list[AlertIdentity]:
    """Return zero/one/multiple condition identities, not one identity per email.

    Call AFTER IS_ALERT classification. metadata optional schema:
      source: opensearch/grafana/prometheus/cloudwatch
      rule_id: native immutable monitor/rule ID
      trigger_id: OpenSearch trigger ID (required for safe native grouping there)
      labels: full instance labels excluding observations
      resource_scope: explicit monitored target if not represented by labels
      alarm_arn: CloudWatch alarm ARN
    For grouped notifications, call once per native alert instance when using metadata.
    Email-derived fallback keys are candidates unless a CloudWatch ARN is present.
    """
    namespace = source_namespace.strip()
    if not namespace:
        raise ValueError('source_namespace must identify tenant and monitoring deployment.')
    metadata = metadata or {}
    body = _text(email.get('body'))
    subject = _text(email.get('subject')).strip()
    source = metadata.get('source', '')
    # ARN is scoped to account, region, and alarm; preserve its exact case.
    arn = metadata.get('alarm_arn')
    if not arn:
        match = re.search(r'^\s*-?\s*Alarm Arn\s*:\s*(arn:[^\n]+)', body, re.I | re.M)
        arn = match.group(1).strip() if match else None
    if arn:
        return [_make(namespace, 'cloudwatch', {'alarm_arn': arn}, 'high', True,
                      'Source-native alarm identity; ALARM/OK and changing counts share a key.')]

    if source and metadata.get('rule_id'):
        labels = dict(metadata.get('labels') or {})
        labels.pop('severity', None)
        identity = {'rule_id': str(metadata['rule_id']), 'labels': labels,
                    'resource_scope': metadata.get('resource_scope', '')}
        if metadata.get('trigger_id'):
            identity['trigger_id'] = str(metadata['trigger_id'])
        safe = source != 'opensearch' or bool(metadata.get('trigger_id'))
        return [_make(namespace, source, identity, 'high' if safe else 'medium', safe,
                      'Native rule plus instance dimensions; OpenSearch requires trigger_id to distinguish triggers.')]

    blocks = _labels(body)
    if blocks:
        source = source or ('grafana' if 'grafana_folder' in str(blocks) else 'prometheus')
        identities = []
        for labels in blocks:
            # Keep host/IP/pod/namespace/container/device/mountpoint etc.
            # Removing these would merge different affected resources.
            labels = {key: value for key, value in labels.items() if key != 'severity'}
            identities.append(_make(namespace, source, {'labels': labels}, 'medium', False,
                'Email-extracted labels; candidate match. Native rule ID is not available.'))
        return list({item.correlation_key: item for item in identities}.values())

    trigger = re.search(r'^\s*Trigger\s*:\s*(.+)$', body, re.I | re.M)
    if trigger and 'opensearch' in body.casefold():
        # Scope from subject preserves Singtel/BSS, components and rule-defining codes.
        # Never replace all numbers: 5012, 5030, N7, N40 and thresholds can define rules.
        identity = {'subject_scope': _rule_name(subject),
                    'trigger_name': _rule_name(trigger.group(1))}
        return [_make(namespace, 'opensearch', identity, 'medium', False,
            'Same named trigger and subject scope; monitor/trigger IDs absent, so grouping needs validation.')]

    return []  # Unknown/report/ticket: no unsafe catch-all normalized-body hash.


def get_email_identity(email: dict, *, mailbox_namespace: str) -> str:
    """Separate delivery identity for deduplication before counting notifications.

    Prefer Graph message IDs scoped to a mailbox. Without one, use exact payload
    timestamp/content fingerprint; distinct identical deliveries can then collide.
    """
    native = email.get('internetMessageId') or email.get('message_id') or email.get('post_id') or email.get('id')
    payload = {'native_id': str(native)} if native else {
        field: str(email.get(field) or '') for field in ('sender', 'timestamp', 'subject', 'body')
    }
    return 'email:v1:' + _digest({'mailbox': mailbox_namespace, 'payload': payload})


def get_alert_identities(email: dict) -> list[str]:
    """Extract condition keys using only the email payload.

    Timestamp and changing measured values intentionally do not enter these keys.
    Call after alert classification. Empty list means unsupported/missing identity.
    Multiple keys mean this email contains multiple monitored alert instances.
    """
    from email.utils import parseaddr

    sender = _text(email.get("sender")).strip()
    sender = (parseaddr(sender)[1] or sender).casefold()
    body = _text(email.get("body"))
    # An ARN already includes AWS account and region; delivery sender can vary.
    has_arn = re.search(r"^\s*-?\s*Alarm Arn\s*:\s*arn:", body, re.I | re.M)
    if not has_arn and not sender:
        return []
    namespace = "native-cloudwatch-arn" if has_arn else "email-sender:" + sender
    return [item.correlation_key for item in generate_alert_identifiers(
        email, source_namespace=namespace
    )]


def get_alert_identity(email: dict) -> str | None:
    """Return a shared condition key only if exactly one condition was extracted.

    None means unsupported format or a grouped notification containing multiple
    conditions. Use get_alert_identities for grouped notifications.
    Never include timestamp or observed count in a condition correlation key.
    """
    keys = get_alert_identities(email)
    return keys[0] if len(keys) == 1 else None

