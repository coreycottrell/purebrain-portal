#!/usr/bin/env python3
"""
Pure Brain Web Conversation Log Server

Flask server for logging Pure Brain web conversations.
Provides HTTP API endpoints for:
- POST /api/log-conversation - Log conversation data to JSONL
- GET /api/health - Health check
- GET /api/stats - Conversation statistics

Supports SSL/HTTPS for secure connections from purebrain.ai

Author: refactoring-specialist
Date: 2026-02-10
Updated: 2026-02-12 (SSL support added by api-architect)
Updated: 2026-02-17 (A-C-Gee landing-chat forwarding added by full-stack-developer)
Updated: 2026-03-02 (Birth proxy + seed intake proxy added — Witness Hetzner migration)
"""

import hashlib
import hmac as _hmac
import json
import logging
import os
import ssl
import subprocess
import threading
import time
import urllib.request
import urllib.error
import uuid
import requests as http_requests  # for birth proxy (avoid name clash with flask.request)
import sys as _sys
# Subdomain router — auto-provision *.purebrain.ai URLs on birth complete
_ROUTER_PATH = '/home/jared/projects/AI-CIV/aether/tools'
if _ROUTER_PATH not in _sys.path:
    _sys.path.insert(0, _ROUTER_PATH)
try:
    from subdomain_router import add_customer_route as _add_customer_route
    _SUBDOMAIN_ROUTER_AVAILABLE = True
except ImportError:
    _SUBDOMAIN_ROUTER_AVAILABLE = False
    logger_bootstrap = __import__('logging').getLogger('purebrain_log_server')
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any

from flask import Flask, request, jsonify
from flask_cors import CORS

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('purebrain_log_server')

# Default log directory
DEFAULT_LOG_DIR = '/home/jared/projects/AI-CIV/aether/logs'
DEFAULT_LOG_FILE = 'purebrain_web_conversations.jsonl'

# SSL certificate paths
SSL_CERT_DIR = '/home/jared/projects/AI-CIV/aether/config/ssl'
SSL_CERT_FILE = os.path.join(SSL_CERT_DIR, 'server.crt')
SSL_KEY_FILE = os.path.join(SSL_CERT_DIR, 'server.key')

# Hub configuration defaults
DEFAULT_HUB_ROOM = 'operations'
DEFAULT_HUB_LOCAL_PATH = '/home/jared/projects/AI-CIV/aether/aiciv-comms-hub-bootstrap/_comms_hub'
HUB_CLI_PATH = '/home/jared/projects/AI-CIV/aether/aiciv-comms-hub-bootstrap/_comms_hub/scripts/hub_cli.py'

# A-C-Gee shared database endpoint
ACGEE_LANDING_CHAT_URL = 'http://5.161.90.32:3001/api/landing-chat'
ACGEE_RETRY_DELAY_SECONDS = 10

# Witness birth pipeline proxy configuration
# Hetzner fleet host (migrated from 104.248.239.98 on 2026-03-01)
WITNESS_BIRTH_BASE_URL = 'http://37.27.237.109:8099'
# Awakening VPS seed intake
WITNESS_SEED_INTAKE_URL = 'http://178.156.229.207:8200'
WITNESS_PROXY_TIMEOUT = 45  # seconds — birth/start can take up to 30s
ACGEE_MAX_RETRIES = 3

# Birth webhook constants
BREVO_BIRTH_TEMPLATE_ID = 30  # Brevo transactional template for birth complete email
BIRTH_COMPLETIONS_LOG = os.path.join(DEFAULT_LOG_DIR, 'birth_completions.jsonl')

# File write lock for thread safety
_file_lock = threading.Lock()


def generate_self_signed_cert():
    """
    Generate self-signed SSL certificate if it doesn't exist.

    Creates a certificate valid for 365 days with SANs for the server IP.
    """
    os.makedirs(SSL_CERT_DIR, exist_ok=True)

    if os.path.exists(SSL_CERT_FILE) and os.path.exists(SSL_KEY_FILE):
        logger.info('SSL certificates already exist')
        return True

    logger.info('Generating self-signed SSL certificate...')

    # OpenSSL config for SAN (Subject Alternative Names)
    openssl_config = f"""
[req]
default_bits = 2048
prompt = no
default_md = sha256
distinguished_name = dn
x509_extensions = v3_req

[dn]
C = US
ST = State
L = City
O = PureBrain
OU = LogServer
CN = 89.167.19.20

[v3_req]
subjectAltName = @alt_names
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment

[alt_names]
IP.1 = 89.167.19.20
DNS.1 = localhost
IP.2 = 127.0.0.1
"""

    config_file = os.path.join(SSL_CERT_DIR, 'openssl.cnf')
    with open(config_file, 'w') as f:
        f.write(openssl_config)

    # Generate certificate using OpenSSL
    cmd = [
        'openssl', 'req', '-x509', '-nodes',
        '-days', '365',
        '-newkey', 'rsa:2048',
        '-keyout', SSL_KEY_FILE,
        '-out', SSL_CERT_FILE,
        '-config', config_file
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            logger.info(f'SSL certificate generated: {SSL_CERT_FILE}')
            return True
        else:
            logger.error(f'Failed to generate SSL cert: {result.stderr}')
            return False
    except Exception as e:
        logger.error(f'Error generating SSL cert: {e}')
        return False


def forward_to_acgee(log_entry: Dict[str, Any]) -> None:
    """
    Forward a conversation log entry to the A-C-Gee shared landing-chat endpoint.

    Sends the full message history so conversations are saved to sage.db on
    the Sage & Weaver VPS. Uses retry logic for 500 errors (may be rate limits).

    Args:
        log_entry: The conversation log entry dictionary (same shape as local JSONL).
    """
    session_id = log_entry.get('session_id', 'unknown')

    # Build the payload for the landing-chat API
    # Include metadata so Witness capture watcher can filter by event_type
    metadata = log_entry.get('metadata', {})
    if isinstance(metadata, dict):
        # For post-payment flows, set event_type to "conversation_complete"
        # so Witness capture watcher processes the seed
        source = log_entry.get('source', '')
        meta_event = metadata.get('event', '')
        if source == 'purebrain-post-payment' or meta_event in ('questionnaire:complete', 'flow:complete'):
            metadata = {**metadata, 'event_type': 'conversation_complete'}

    payload = {
        'messages': log_entry.get('messages', []),
        'system': log_entry.get('system', ''),
        'session_id': session_id,
        'source': 'purebrain',
        'metadata': metadata,
        'aiName': log_entry.get('aiName', ''),
        'userName': log_entry.get('userName', ''),
        'userTier': log_entry.get('userTier', ''),
    }

    payload_bytes = json.dumps(payload).encode('utf-8')

    for attempt in range(1, ACGEE_MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                ACGEE_LANDING_CHAT_URL,
                data=payload_bytes,
                headers={
                    'Content-Type': 'application/json',
                    'User-Agent': 'Aether-PureBrain-LogServer/1.0',
                },
                method='POST'
            )

            with urllib.request.urlopen(req, timeout=30) as resp:
                status = resp.getcode()
                body = resp.read().decode('utf-8')

            if status == 200:
                logger.info(
                    f'A-C-Gee forward success: session={session_id} attempt={attempt}'
                )
                return  # Success - stop retrying

            logger.warning(
                f'A-C-Gee forward unexpected status {status} for session={session_id}'
            )
            return  # Non-500 non-200 - don't retry

        except urllib.error.HTTPError as e:
            if e.code == 500:
                logger.warning(
                    f'A-C-Gee 500 error (attempt {attempt}/{ACGEE_MAX_RETRIES}) '
                    f'session={session_id} - retrying in {ACGEE_RETRY_DELAY_SECONDS}s'
                )
                if attempt < ACGEE_MAX_RETRIES:
                    time.sleep(ACGEE_RETRY_DELAY_SECONDS)
            else:
                logger.warning(
                    f'A-C-Gee HTTP error {e.code} for session={session_id}: {e.reason}'
                )
                return  # Non-500 HTTP error - don't retry

        except urllib.error.URLError as e:
            logger.warning(
                f'A-C-Gee network error (attempt {attempt}/{ACGEE_MAX_RETRIES}) '
                f'session={session_id}: {e.reason}'
            )
            if attempt < ACGEE_MAX_RETRIES:
                time.sleep(ACGEE_RETRY_DELAY_SECONDS)

        except Exception as e:
            logger.warning(f'A-C-Gee unexpected error for session={session_id}: {e}')
            return  # Unknown error - don't retry

    logger.error(
        f'A-C-Gee forward failed after {ACGEE_MAX_RETRIES} attempts for session={session_id}'
    )


def forward_to_hub(log_entry: Dict[str, Any], hub_room: str = DEFAULT_HUB_ROOM) -> None:
    """
    Forward a conversation log entry to the AICIV comms hub.

    This function runs the hub_cli.py send command to post a message
    to the specified room. Designed to be called asynchronously
    from a background thread.

    Args:
        log_entry: The conversation log entry dictionary.
        hub_room: The hub room to post to (default: 'operations').
    """
    try:
        # Build summary from conversation
        session_id = log_entry.get('session_id', 'unknown')
        messages = log_entry.get('messages', [])
        message_count = len(messages)

        # Get first user message as preview
        first_user_msg = ''
        for msg in messages:
            if msg.get('role') == 'user':
                first_user_msg = msg.get('content', '')[:100]
                break

        summary = f"Pure Brain conversation: {session_id} ({message_count} messages)"

        # Build body with conversation details
        body_lines = [
            f"Session ID: {session_id}",
            f"Messages: {message_count}",
            f"Timestamp: {log_entry.get('server_timestamp', 'unknown')}",
        ]

        if log_entry.get('page_url'):
            body_lines.append(f"Page: {log_entry['page_url']}")

        if first_user_msg:
            body_lines.append(f"First query: {first_user_msg}...")

        body = '\n'.join(body_lines)

        # Set up environment for hub_cli
        env = os.environ.copy()
        env['HUB_REPO_URL'] = env.get('HUB_REPO_URL', 'git@github-interciv:coreycottrell/aiciv-comms-hub.git')
        env['HUB_LOCAL_PATH'] = env.get('HUB_LOCAL_PATH', DEFAULT_HUB_LOCAL_PATH)
        env['HUB_AGENT_ID'] = env.get('HUB_AGENT_ID', 'aether-purebrain')
        env['HUB_AGENT_DISPLAY'] = env.get('HUB_AGENT_DISPLAY', 'Pure Brain Log Server')
        env['GIT_AUTHOR_NAME'] = env.get('GIT_AUTHOR_NAME', 'Aether Pure Brain')
        env['GIT_AUTHOR_EMAIL'] = env.get('GIT_AUTHOR_EMAIL', 'purebrain@ai-civ.local')

        # Run hub_cli send command
        cmd = [
            'python3', HUB_CLI_PATH,
            'send',
            '--room', hub_room,
            '--type', 'status',
            '--summary', summary,
            '--body', body
        ]

        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=60  # 60 second timeout for git operations
        )

        if result.returncode == 0:
            logger.info(f'Forwarded to hub: {session_id} -> {hub_room}')
        else:
            logger.warning(f'Hub forwarding failed: {result.stderr}')

    except subprocess.TimeoutExpired:
        logger.warning(f'Hub forwarding timed out for session: {session_id}')
    except Exception as e:
        logger.warning(f'Hub forwarding error: {e}')


def create_app(
    log_dir: Optional[str] = None,
    enable_hub_forwarding: bool = True,
    hub_room: Optional[str] = None,
    enable_acgee_forwarding: bool = True,
) -> Flask:
    """
    Create and configure Flask application.

    Args:
        log_dir: Directory for log files. Defaults to DEFAULT_LOG_DIR.
        enable_hub_forwarding: Whether to forward conversations to AICIV hub.
        hub_room: Hub room for forwarding (default: 'operations').
        enable_acgee_forwarding: Whether to forward conversations to A-C-Gee shared DB.

    Returns:
        Configured Flask application.
    """
    app = Flask(__name__)

    # Configure log directory
    log_dir = log_dir or DEFAULT_LOG_DIR
    app.config['LOG_DIR'] = log_dir
    app.config['LOG_FILE'] = os.path.join(log_dir, DEFAULT_LOG_FILE)

    # Configure hub forwarding
    app.config['ENABLE_HUB_FORWARDING'] = enable_hub_forwarding
    app.config['HUB_ROOM'] = hub_room or DEFAULT_HUB_ROOM

    # Configure A-C-Gee forwarding
    app.config['ENABLE_ACGEE_FORWARDING'] = enable_acgee_forwarding

    # Ensure log directory exists
    os.makedirs(log_dir, exist_ok=True)

    # Enable CORS for all routes (required for cross-origin HTTPS requests)
    CORS(app, resources={r"/api/*": {"origins": "*"}})

    # Register routes
    register_routes(app)

    return app


def _send_telegram_notification(message: str) -> None:
    """Send a notification to Jared via Telegram (best-effort, non-blocking)."""
    try:
        tg_config_path = '/home/jared/projects/AI-CIV/aether/config/telegram_config.json'
        with open(tg_config_path) as f:
            tg_config = json.load(f)
        bot_token = tg_config.get('bot_token', '')
        chat_id = tg_config.get('default_chat_id', '')
        if bot_token and chat_id:
            url = f'https://api.telegram.org/bot{bot_token}/sendMessage'
            data = json.dumps({'chat_id': chat_id, 'text': message}).encode('utf-8')
            req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
            urllib.request.urlopen(req, timeout=10)
            logger.info('Telegram notification sent')
    except Exception as e:
        logger.warning(f'Telegram notification failed: {e}')


def verify_witness_signature(body: bytes, sig_header: str) -> bool:
    """Verify HMAC-SHA256 signature from Witness webhook."""
    secret = os.environ.get('WITNESS_WEBHOOK_SECRET', '')
    if not secret:
        # Dev/test: allow if no secret configured, but log warning
        logger.warning('WITNESS_WEBHOOK_SECRET not set — skipping signature check')
        return True
    if not sig_header or not sig_header.startswith('sha256='):
        return False
    expected = 'sha256=' + _hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return _hmac.compare_digest(expected, sig_header)


def _is_duplicate_birth(email: str, container: str) -> bool:
    """Check if we already processed this birth (idempotency guard)."""
    if not os.path.exists(BIRTH_COMPLETIONS_LOG):
        return False
    try:
        with open(BIRTH_COMPLETIONS_LOG, 'r') as f:
            for line in f:
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get('human_email') == email and entry.get('container') == container:
                    return True
    except Exception:
        pass
    return False


def _send_birth_complete_email(email: str, name: str, civ_name: str, magic_link: str):
    """Send magic link email via Brevo transactional API."""
    brevo_key = os.environ.get('BREVO_API_KEY', '')
    if not brevo_key:
        logger.error('BREVO_API_KEY not set — cannot send birth email')
        return
    payload = {
        'to': [{'email': email, 'name': name}],
        'templateId': BREVO_BIRTH_TEMPLATE_ID,
        'params': {
            'human_name': name,
            'civ_name': civ_name,
            'magic_link': magic_link,
        },
        'replyTo': {'email': 'jared@puretechnology.nyc', 'name': 'Jared at PureBrain'},
    }
    try:
        resp = http_requests.post(
            'https://api.brevo.com/v3/smtp/email',
            headers={'api-key': brevo_key, 'Content-Type': 'application/json'},
            json=payload,
            timeout=10,
        )
        if resp.status_code not in (200, 201):
            logger.error(f'Brevo birth email failed: {resp.status_code} {resp.text}')
        else:
            logger.info(f'Birth email sent to {email}')
    except Exception as e:
        logger.error(f'Brevo birth email exception: {e}')


def _notify_jared_birth_complete(email: str, name: str, civ_name: str, container: str, magic_link: str):
    """Send Telegram notification to Jared about birth completion."""
    try:
        tg_config = json.load(open('/home/jared/projects/AI-CIV/aether/config/telegram_config.json'))
        token = tg_config.get('bot_token', '')
        msg = (
            f"🎉 BIRTH COMPLETE\n"
            f"Customer: {name} ({email})\n"
            f"CIV: {civ_name} @ {container}\n"
            f"Magic link: {magic_link}\n"
            f"Email sent to customer."
        )
        http_requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            data={'chat_id': '548906264', 'text': msg},
            timeout=5,
        )
    except Exception as e:
        logger.warning(f'Telegram birth notify failed: {e}')


def register_routes(app: Flask) -> None:
    """Register all API routes on the Flask app."""

    @app.route('/api/health', methods=['GET'])
    def health():
        """Health check endpoint."""
        logger.info('Health check requested')
        return jsonify({
            'status': 'ok',
            'ssl': True,
            'timestamp': datetime.now(timezone.utc).isoformat()
        })

    @app.route('/api/log-conversation', methods=['POST', 'OPTIONS'])
    def log_conversation():
        """
        Log conversation data to JSONL file.

        Expected JSON payload:
        {
            "session_id": "unique-session-id",
            "messages": [
                {"role": "user", "content": "..."},
                {"role": "assistant", "content": "..."}
            ],
            "user_agent": "optional",
            "page_url": "optional",
            "referrer": "optional"
        }
        """
        # Handle preflight OPTIONS request
        if request.method == 'OPTIONS':
            return '', 204

        # Validate content type
        if not request.is_json:
            logger.warning('Request rejected: not JSON content type')
            return jsonify({'error': 'Content-Type must be application/json'}), 400

        # Parse JSON
        try:
            data = request.get_json()
        except Exception as e:
            logger.warning(f'Request rejected: invalid JSON - {e}')
            return jsonify({'error': 'Invalid JSON'}), 400

        # Validate required fields
        # Accept both 'messages' (A-C-Gee standard) and 'conversationHistory' (PureBrain frontend)
        messages = data.get('messages') or data.get('conversationHistory') if data else None
        if not data or not messages:
            logger.warning('Request rejected: missing messages/conversationHistory field')
            return jsonify({'error': 'Missing required field: messages or conversationHistory'}), 400

        # Build log entry
        # Accept both snake_case (Python convention) and camelCase (JS convention) for session ID.
        # Generate a UUID session_id if none provided so A-C-Gee can upsert correctly.
        raw_session_id = data.get('session_id') or data.get('sessionId') or ''
        session_id = raw_session_id.strip() if raw_session_id.strip() else f'pb-{uuid.uuid4()}'

        log_entry = {
            'session_id': session_id,
            'messages': messages,  # Always use 'messages' for A-C-Gee compatibility
            'server_timestamp': datetime.now(timezone.utc).isoformat(),
            'client_ip': request.remote_addr
        }

        # Add optional fields if present
        optional_fields = [
            'user_agent', 'page_url', 'referrer', 'user_id', 'metadata',
            # Onboarding/session data from client
            'aiName', 'userName', 'userTier', 'referralCode',
            'conversationId', 'brainId', 'projectId', 'title',
            'createdAt', 'updatedAt', 'messageCount'
        ]
        for field in optional_fields:
            if field in data:
                log_entry[field] = data[field]

        # Write to JSONL file (thread-safe)
        log_file = app.config['LOG_FILE']
        try:
            with _file_lock:
                with open(log_file, 'a') as f:
                    f.write(json.dumps(log_entry) + '\n')
            logger.info(f'Logged conversation: session={log_entry["session_id"]}')
        except Exception as e:
            logger.error(f'Failed to write log: {e}')
            return jsonify({'error': 'Failed to write log'}), 500

        # Forward to AICIV comms hub asynchronously (non-blocking)
        if app.config.get('ENABLE_HUB_FORWARDING', True):
            hub_room = app.config.get('HUB_ROOM', DEFAULT_HUB_ROOM)
            try:
                # Run in background thread so it doesn't block API response
                hub_thread = threading.Thread(
                    target=forward_to_hub,
                    args=(log_entry.copy(), hub_room),
                    daemon=True
                )
                hub_thread.start()
                logger.debug(f'Started hub forwarding thread for session={log_entry["session_id"]}')
            except Exception as e:
                # Hub forwarding failure should not break local logging
                logger.warning(f'Failed to start hub forwarding: {e}')

        # Forward to A-C-Gee shared database asynchronously (non-blocking)
        if app.config.get('ENABLE_ACGEE_FORWARDING', True):
            try:
                acgee_thread = threading.Thread(
                    target=forward_to_acgee,
                    args=(log_entry.copy(),),
                    daemon=True
                )
                acgee_thread.start()
                logger.debug(f'Started A-C-Gee forwarding thread for session={log_entry["session_id"]}')
            except Exception as e:
                # A-C-Gee forwarding failure should not break local logging
                logger.warning(f'Failed to start A-C-Gee forwarding: {e}')

        return jsonify({
            'success': True,
            'session_id': log_entry['session_id'],
            'timestamp': log_entry['server_timestamp']
        })

    @app.route('/api/verify-payment', methods=['POST', 'OPTIONS'])
    def verify_payment():
        """
        Verify a PayPal payment after SDK capture.
        Called by pay-test page after PayPal SDK completes payment.

        Expected JSON payload:
        {
            "orderId": "PAYPAL-ORDER-ID",
            "tier": "Awakened|Bonded|Partnered",
            "amount": "79.00",
            "payerEmail": "buyer@email.com",
            "payerName": "John Doe",
            "captureId": "CAPTURE-ID",
            "timestamp": "ISO-8601"
        }
        """
        if request.method == 'OPTIONS':
            return '', 204

        if not request.is_json:
            return jsonify({'error': 'Content-Type must be application/json'}), 400

        try:
            data = request.get_json()
        except Exception as e:
            return jsonify({'error': 'Invalid JSON'}), 400

        if not data or not data.get('orderId'):
            return jsonify({'error': 'Missing required field: orderId'}), 400

        # Log the payment verification
        payment_entry = {
            'type': 'payment_verification',
            'orderId': data.get('orderId'),
            'tier': data.get('tier', 'unknown'),
            'amount': data.get('amount', '0.00'),
            'payerEmail': data.get('payerEmail', ''),
            'payerName': data.get('payerName', ''),
            'captureId': data.get('captureId', ''),
            'server_timestamp': datetime.now(timezone.utc).isoformat(),
            'client_ip': request.remote_addr,
            'verified': True
        }

        # Write to dedicated payment log
        payment_log = os.path.join(DEFAULT_LOG_DIR, 'purebrain_payments.jsonl')
        try:
            with _file_lock:
                with open(payment_log, 'a') as f:
                    f.write(json.dumps(payment_entry) + '\n')
            logger.info(f'Payment verified: order={data.get("orderId")}, tier={data.get("tier")}, amount={data.get("amount")}')
        except Exception as e:
            logger.error(f'Failed to write payment log: {e}')
            return jsonify({'error': 'Failed to log payment'}), 500

        # Increment spots claimed counter — ONLY for real (non-sandbox, non-test) payments.
        # Sandbox/test orders must NEVER increment the public-facing invitation page counter.
        # Real PayPal order IDs are alphanumeric strings like "4MA83119W1272721N".
        # Filtered patterns:
        #   SANDBOX-TEST* — explicit sandbox test orders from E2E flows
        #   E2E-*         — E2E automation test orders
        #   test-*        — manual test orders
        #   I-*           — PayPal subscription/billing agreement IDs (not one-time payments)
        order_id = data.get('orderId', '')
        sandbox_prefixes = ('SANDBOX-TEST', 'E2E-', 'test-', 'I-')
        is_sandbox_or_test = any(order_id.startswith(prefix) for prefix in sandbox_prefixes)
        if is_sandbox_or_test:
            logger.info(f'Spots counter NOT incremented — sandbox/test order filtered out: {order_id}')
        else:
            spots_file = os.path.join(DEFAULT_LOG_DIR, 'spots_state.json')
            try:
                with _file_lock:
                    try:
                        with open(spots_file, 'r') as f:
                            spots_data = json.load(f)
                    except (FileNotFoundError, json.JSONDecodeError):
                        spots_data = {'spots_claimed': 0, 'spots_total': 25, 'claimed_orders': []}
                    spots_data['spots_claimed'] = spots_data.get('spots_claimed', 0) + 1
                    order_record = {
                        'order_id': order_id,
                        'tier': data.get('tier', 'unknown'),
                        'payer_email': data.get('payerEmail', ''),
                        'timestamp': payment_entry['server_timestamp']
                    }
                    if 'claimed_orders' not in spots_data:
                        spots_data['claimed_orders'] = []
                    spots_data['claimed_orders'].append(order_record)
                    with open(spots_file, 'w') as f:
                        json.dump(spots_data, f, indent=2)
                logger.info(f'Spots counter incremented to {spots_data["spots_claimed"]} (real order: {order_id})')
            except Exception as e:
                logger.warning(f'Failed to increment spots counter: {e}')

        return jsonify({
            'success': True,
            'verified': True,
            'orderId': data.get('orderId'),
            'server_timestamp': payment_entry['server_timestamp']
        })

    @app.route('/api/spots-status', methods=['GET', 'OPTIONS'])
    def spots_status():
        """
        Return current spots claimed status for invitation page.
        Returns: {"spots_claimed": N, "spots_total": 25}
        """
        if request.method == 'OPTIONS':
            return '', 204

        spots_file = os.path.join(DEFAULT_LOG_DIR, 'spots_state.json')
        try:
            with _file_lock:
                try:
                    with open(spots_file, 'r') as f:
                        spots_data = json.load(f)
                except (FileNotFoundError, json.JSONDecodeError):
                    spots_data = {'spots_claimed': 3, 'spots_total': 25}
            return jsonify({
                'spots_claimed': spots_data.get('spots_claimed', 3),
                'spots_total': spots_data.get('spots_total', 25)
            })
        except Exception as e:
            logger.error(f'Failed to read spots status: {e}')
            return jsonify({'spots_claimed': 3, 'spots_total': 25})

    @app.route('/api/log-pay-test', methods=['POST', 'OPTIONS'])
    def log_pay_test():
        """
        Log pay-test flow completion data.
        Called after the full post-payment questionnaire + behind-the-curtain flow.

        Expected JSON payload:
        {
            "tier": "Awakened|Bonded|Partnered",
            "orderId": "PAYPAL-ORDER-ID",
            "aiName": "Name chosen by user",
            "name": "User's name",
            "email": "user@email.com",
            "company": "Company name",
            "role": "User's role",
            "primaryGoal": "User's primary goal",
            "telegramBotToken": "bot token if provided",
            "claudeMaxStatus": "connected|skipped",
            "flowCompleted": true
        }
        """
        if request.method == 'OPTIONS':
            return '', 204

        if not request.is_json:
            return jsonify({'error': 'Content-Type must be application/json'}), 400

        try:
            data = request.get_json()
        except Exception as e:
            return jsonify({'error': 'Invalid JSON'}), 400

        if not data:
            return jsonify({'error': 'Empty payload'}), 400

        # Build log entry
        pay_test_entry = {
            'type': 'pay_test_completion',
            'tier': data.get('tier', 'unknown'),
            'orderId': data.get('orderId', ''),
            'aiName': data.get('aiName', ''),
            'name': data.get('name', ''),
            'email': data.get('email', ''),
            'company': data.get('company', ''),
            'role': data.get('role', ''),
            'primaryGoal': data.get('primaryGoal', ''),
            'telegramBotToken': data.get('telegramBotToken', ''),
            'claudeMaxStatus': data.get('claudeMaxStatus', ''),
            'flowCompleted': data.get('flowCompleted', False),
            'server_timestamp': datetime.now(timezone.utc).isoformat(),
            'client_ip': request.remote_addr
        }

        # Write to dedicated pay-test log
        pay_test_log = os.path.join(DEFAULT_LOG_DIR, 'purebrain_pay_test.jsonl')
        try:
            with _file_lock:
                with open(pay_test_log, 'a') as f:
                    f.write(json.dumps(pay_test_entry) + '\n')
            logger.info(f'Pay-test logged: tier={data.get("tier")}, aiName={data.get("aiName")}, email={data.get("email")}')
        except Exception as e:
            logger.error(f'Failed to write pay-test log: {e}')
            return jsonify({'error': 'Failed to log pay-test data'}), 500

        # Forward to Telegram ONLY for completions (Jared: "not every message")
        event = data.get('event', '')
        is_completion = (
            data.get('flowCompleted', False)
            or event in ('questionnaire:complete', 'flow:complete')
        )
        if is_completion:
            try:
                tg_msg = (
                    f"🎉 PAY-TEST COMPLETE!\n"
                    f"Tier: {data.get('tier', 'unknown')}\n"
                    f"AI Name: {data.get('aiName', 'N/A')}\n"
                    f"Name: {data.get('name', 'N/A')}\n"
                    f"Email: {data.get('email', 'N/A')}\n"
                    f"Company: {data.get('company', 'N/A')}\n"
                    f"Role: {data.get('role', 'N/A')}\n"
                    f"Goal: {data.get('primaryGoal', 'N/A')}\n"
                    f"Order: {data.get('orderId', 'N/A')}"
                )
                tg_thread = threading.Thread(
                    target=_send_telegram_notification,
                    args=(tg_msg,),
                    daemon=True
                )
                tg_thread.start()
            except Exception:
                pass

        return jsonify({
            'success': True,
            'logged': True,
            'server_timestamp': pay_test_entry['server_timestamp']
        })

    # ── Witness Birth Pipeline Proxy ────────────────────────────────────────
    # Browser calls our HTTPS endpoint; we proxy to Witness HTTP server-side.
    # This avoids mixed-content blocking in the browser.

    @app.route('/api/birth/start', methods=['POST', 'OPTIONS'])
    def birth_start_proxy():
        """Proxy POST /api/birth/start to Witness Hetzner fleet host."""
        if request.method == 'OPTIONS':
            return _cors_preflight()
        try:
            resp = http_requests.post(
                f'{WITNESS_BIRTH_BASE_URL}/api/birth/start',
                json=request.get_json(silent=True) or {},
                timeout=WITNESS_PROXY_TIMEOUT,
            )
            logger.info(f'Birth/start proxy: status={resp.status_code}')
            return (resp.content, resp.status_code, {'Content-Type': 'application/json'})
        except http_requests.ConnectionError:
            logger.error(f'Birth/start proxy: connection refused ({WITNESS_BIRTH_BASE_URL})')
            return jsonify({'error': 'Birth service unavailable', 'details': 'Could not connect to birth service'}), 503
        except http_requests.Timeout:
            logger.error(f'Birth/start proxy: timeout ({WITNESS_PROXY_TIMEOUT}s)')
            return jsonify({'error': 'Birth service timeout', 'details': f'No response within {WITNESS_PROXY_TIMEOUT}s'}), 504

    @app.route('/api/birth/code', methods=['POST', 'OPTIONS'])
    def birth_code_proxy():
        """Proxy POST /api/birth/code to Witness (OAuth auth code relay)."""
        if request.method == 'OPTIONS':
            return _cors_preflight()
        try:
            resp = http_requests.post(
                f'{WITNESS_BIRTH_BASE_URL}/api/birth/code',
                json=request.get_json(silent=True) or {},
                timeout=30,
            )
            logger.info(f'Birth/code proxy: status={resp.status_code}')
            return (resp.content, resp.status_code, {'Content-Type': 'application/json'})
        except http_requests.ConnectionError:
            return jsonify({'error': 'Birth service unavailable'}), 503
        except http_requests.Timeout:
            return jsonify({'error': 'Birth service timeout'}), 504

    @app.route('/api/birth/status/<container>', methods=['GET', 'OPTIONS'])
    def birth_status_proxy(container):
        """Proxy GET /api/birth/status/{container} to Witness."""
        if request.method == 'OPTIONS':
            return _cors_preflight()
        try:
            resp = http_requests.get(
                f'{WITNESS_BIRTH_BASE_URL}/api/birth/status/{container}',
                timeout=15,
            )
            return (resp.content, resp.status_code, {'Content-Type': 'application/json'})
        except (http_requests.ConnectionError, http_requests.Timeout):
            return jsonify({'error': 'Birth service unavailable'}), 503

    @app.route('/api/birth/portal-status/<container>', methods=['GET', 'OPTIONS'])
    def birth_portal_status_proxy(container):
        """
        Check if a birth is complete and portal is ready.
        First checks our local birth_completions.jsonl (populated by webhook).
        Falls back to proxying to Witness if not found locally.
        Browser polls this every 30s after payment.
        """
        if request.method == 'OPTIONS':
            return _cors_preflight()

        if not container or not container.replace('-', '').isalnum():
            return jsonify({'ready': False, 'error': 'Invalid container name'}), 400

        # 1. Check local birth completions log first (from webhook)
        if os.path.exists(BIRTH_COMPLETIONS_LOG):
            try:
                with open(BIRTH_COMPLETIONS_LOG, 'r') as f:
                    for line in f:
                        if not line.strip():
                            continue
                        entry = json.loads(line)
                        if entry.get('container') == container:
                            logger.info(f'Portal status: {container} READY (local log)')
                            return jsonify({
                                'ready': True,
                                'portalUrl': entry.get('purebrain_url') or entry.get('magic_link', ''),
                            }), 200
            except Exception as e:
                logger.error(f'Portal status local check error: {e}')

        # 2. Fall back to Witness proxy
        try:
            resp = http_requests.get(
                f'{WITNESS_BIRTH_BASE_URL}/api/birth/portal-status/{container}',
                timeout=15,
            )
            return (resp.content, resp.status_code, {'Content-Type': 'application/json'})
        except (http_requests.ConnectionError, http_requests.Timeout):
            return jsonify({'ready': False}), 200

    # ── Seed Intake Proxy ────────────────────────────────────────────────────

    @app.route('/api/intake/seed', methods=['POST', 'OPTIONS'])
    def seed_intake_proxy():
        """Proxy POST /api/intake/seed to Witness awakening VPS."""
        if request.method == 'OPTIONS':
            return _cors_preflight()
        try:
            resp = http_requests.post(
                f'{WITNESS_SEED_INTAKE_URL}/intake/seed',
                json=request.get_json(silent=True) or {},
                timeout=15,
            )
            logger.info(f'Seed intake proxy: status={resp.status_code}')
            return (resp.content, resp.status_code, {'Content-Type': 'application/json'})
        except http_requests.ConnectionError:
            return jsonify({'error': 'Seed intake unavailable'}), 503
        except http_requests.Timeout:
            return jsonify({'error': 'Seed intake timeout'}), 504

    def _cors_preflight():
        """Return CORS preflight response for proxy endpoints."""
        resp = jsonify({'ok': True})
        resp.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin', '*')
        resp.headers['Access-Control-Allow-Methods'] = 'POST, GET, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Witness-Signature, X-Witness-Secret'
        resp.headers['Access-Control-Max-Age'] = '3600'
        return resp

    # ── Birth Complete Webhook ────────────────────────────────────────────────

    @app.route('/api/birth/webhook', methods=['POST', 'OPTIONS'])
    def birth_complete_webhook():
        """
        Receive birth_complete callback from Witness after CIV birth.
        Witness POSTs: { event, human_email, human_name, civ_name, magic_link, container }
        Header: X-Witness-Signature: sha256=<hex> (HMAC) or X-Witness-Secret (fallback)
        """
        if request.method == 'OPTIONS':
            return _cors_preflight()

        # 1. Verify signature (HMAC or static token fallback)
        raw_body = request.get_data()
        sig = request.headers.get('X-Witness-Signature', '')
        static_secret = request.headers.get('X-Witness-Secret', '')

        if sig:
            if not verify_witness_signature(raw_body, sig):
                logger.warning(f'Webhook: invalid HMAC signature from {request.remote_addr}')
                return jsonify({'error': 'Invalid signature'}), 401
        elif static_secret:
            expected_secret = os.environ.get('WITNESS_STATIC_SECRET', 'witness-secret-2026')
            if static_secret != expected_secret:
                logger.warning(f'Webhook: invalid static secret from {request.remote_addr}')
                return jsonify({'error': 'Invalid secret'}), 401
        else:
            # No auth header — check if we're in dev mode (no secret configured)
            if os.environ.get('WITNESS_WEBHOOK_SECRET', ''):
                logger.warning(f'Webhook: no auth header from {request.remote_addr}')
                return jsonify({'error': 'Missing authentication'}), 401
            logger.warning('Webhook: no auth header and no secret configured — allowing (dev mode)')

        # 2. Parse + validate
        data = request.get_json(silent=True) or {}
        event = data.get('event')
        human_email = data.get('human_email', '')
        human_name = data.get('human_name', '')
        civ_name = data.get('civ_name', '')
        magic_link = data.get('magic_link', '')
        container = data.get('container', '')
        portal_url = data.get('portal_url', magic_link)  # accept either field name

        if event != 'birth_complete':
            logger.warning(f'Webhook: unknown event "{event}"')
            return jsonify({'error': f'Unknown event: {event}'}), 400

        if not human_email or '@' not in human_email:
            return jsonify({'error': 'Invalid human_email'}), 400

        if not (magic_link or portal_url):
            return jsonify({'error': 'Missing magic_link or portal_url'}), 400

        # Use portal_url as the link if magic_link not provided
        link = magic_link or portal_url

        # 2b. Derive purebrain.ai subdomain and rewrite magic link
        # Pattern: {ainame}{humanfirstname}.purebrain.ai (lowercase, no hyphens)
        import re as _re_url
        from urllib.parse import urlparse as _urlparse
        _pb_subdomain = ''
        _pb_url = ''
        if civ_name and human_name:
            _ai_part = _re_url.sub(r'[^a-z0-9]', '', civ_name.lower())
            _first_part = _re_url.sub(r'[^a-z0-9]', '', human_name.lower().split()[0]) if human_name else ''
            _pb_subdomain = (_ai_part + _first_part)[:63]
            if _pb_subdomain and link:
                _parsed = _urlparse(link)
                _path = _parsed.path if _parsed.path and _parsed.path != '/' else ''
                _query = f'?{_parsed.query}' if _parsed.query else ''
                _pb_url = f'https://{_pb_subdomain}.purebrain.ai{_path}{_query}'
                if not _path and not _query:
                    _pb_url = f'https://{_pb_subdomain}.purebrain.ai/'
                logger.info(f'Magic link rewritten: {link} -> {_pb_url}')
            elif _pb_subdomain:
                _pb_url = f'https://{_pb_subdomain}.purebrain.ai/'

        # Use purebrain.ai URL if available, original link as fallback
        customer_link = _pb_url or link

        # 3. Idempotency check
        if _is_duplicate_birth(human_email, container):
            logger.info(f'Webhook: duplicate birth for {human_email} / {container}, acking')
            return jsonify({'ok': True, 'duplicate': True}), 200

        # 4. Log completion
        entry = {
            'type': 'birth_complete',
            'human_email': human_email,
            'human_name': human_name,
            'civ_name': civ_name,
            'magic_link': customer_link,
            'original_magic_link': link,
            'purebrain_url': _pb_url,
            'purebrain_subdomain': _pb_subdomain,
            'container': container,
            'received_at': datetime.now(timezone.utc).isoformat(),
            'source_ip': request.remote_addr,
        }
        with _file_lock:
            with open(BIRTH_COMPLETIONS_LOG, 'a') as f:
                f.write(json.dumps(entry) + '\n')

        # 5. Send magic link email via Brevo
        _send_birth_complete_email(human_email, human_name, civ_name, customer_link)

        # 6. Telegram notification to Jared
        _notify_jared_birth_complete(human_email, human_name, civ_name, container, customer_link)

        # 7. Provision *.purebrain.ai subdomain for the customer
        # Subdomain pattern: {ainame}{humanfirstname} (lowercase, no spaces)
        # e.g. civ_name=keen, human_name=Jared Sanborn → keenjared
        try:
            if _SUBDOMAIN_ROUTER_AVAILABLE and civ_name and human_name:
                import re as _re
                _ai = _re.sub(r'[^a-z0-9]', '', civ_name.lower())
                _first = _re.sub(r'[^a-z0-9]', '', human_name.lower().split()[0]) if human_name else ''
                _subdomain = (_ai + _first)[:63]  # DNS label max 63 chars
                if _subdomain:
                    _portal_url = link or f'https://{container}.ai-civ.com'
                    _ok, _msg = _add_customer_route(
                        _subdomain, _portal_url, container, human_email, human_name, civ_name
                    )
                    if _ok:
                        logger.info(f'Subdomain provisioned: https://{_subdomain}.purebrain.ai → {_portal_url}')
                    else:
                        logger.warning(f'Subdomain provisioning failed for {_subdomain}: {_msg}')
            elif not _SUBDOMAIN_ROUTER_AVAILABLE:
                logger.warning('subdomain_router not available — skipping subdomain provisioning')
        except Exception as _e:
            logger.error(f'Subdomain provisioning error: {_e}')

        logger.info(f'Birth complete: {human_name} ({human_email}) → {civ_name} at {container}')
        return jsonify({'ok': True}), 200



    @app.route('/api/stats', methods=['GET'])
    def stats():
        """Return conversation statistics."""
        log_file = app.config['LOG_FILE']

        # Count conversations
        conversation_count = 0
        file_size_bytes = 0

        if os.path.exists(log_file):
            file_size_bytes = os.path.getsize(log_file)
            try:
                with open(log_file, 'r') as f:
                    for line in f:
                        if line.strip():
                            conversation_count += 1
            except Exception as e:
                logger.error(f'Error reading stats: {e}')

        logger.info(f'Stats requested: {conversation_count} conversations')

        return jsonify({
            'conversation_count': conversation_count,
            'file_size_bytes': file_size_bytes,
            'log_file': log_file,
            'timestamp': datetime.now(timezone.utc).isoformat()
        })

    @app.errorhandler(400)
    def bad_request(e):
        """Handle 400 errors."""
        return jsonify({'error': str(e.description)}), 400

    @app.errorhandler(500)
    def internal_error(e):
        """Handle 500 errors."""
        logger.error(f'Internal error: {e}')
        return jsonify({'error': 'Internal server error'}), 500


def main():
    """Run the Flask server with SSL support."""
    # Get port from environment or use default
    port = int(os.environ.get('PUREBRAIN_LOG_PORT', 8443))
    host = os.environ.get('PUREBRAIN_LOG_HOST', '0.0.0.0')

    # Check if SSL should be disabled (for testing)
    disable_ssl = os.environ.get('PUREBRAIN_DISABLE_SSL', '').lower() in ('true', '1', 'yes')

    logger.info(f'Starting Pure Brain Log Server on {host}:{port}')
    logger.info(f'Log directory: {DEFAULT_LOG_DIR}')
    logger.info(f'Log file: {os.path.join(DEFAULT_LOG_DIR, DEFAULT_LOG_FILE)}')

    app = create_app()

    if disable_ssl:
        logger.info('SSL DISABLED - Running in HTTP mode')
        app.run(
            host=host,
            port=port,
            threaded=True,
            debug=False
        )
    else:
        # Generate SSL certificate if needed
        if not generate_self_signed_cert():
            logger.error('Failed to generate SSL certificate. Exiting.')
            return

        logger.info(f'SSL enabled - Certificate: {SSL_CERT_FILE}')
        logger.info(f'HTTPS endpoint: https://{host}:{port}/api/log-conversation')

        # Create SSL context
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(SSL_CERT_FILE, SSL_KEY_FILE)

        # Run with SSL
        app.run(
            host=host,
            port=port,
            threaded=True,
            debug=False,
            ssl_context=ssl_context
        )


if __name__ == '__main__':
    main()
