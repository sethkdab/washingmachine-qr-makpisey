import os
import uuid
import hmac
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import requests
from flask import Flask, request, jsonify, redirect, abort, make_response
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, or_

# =========================================================
# Config
# =========================================================

app = Flask(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///laundry.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

APP_BASE_URL = os.getenv("APP_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
APP_SECRET = os.getenv("APP_SECRET", "change-me")
INTERNAL_API_SECRET = os.getenv("INTERNAL_API_SECRET", "change-me-too")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "YourLaundryBot")

# Example:
# ABA_PAY_URL_TEMPLATE="https://pay.example.com/pay?amount={amount}&session_id={session_id}&machine_id={machine_id}"
ABA_PAY_URL_TEMPLATE = os.getenv("ABA_PAY_URL_TEMPLATE", "")

SESSION_HOLD_MINUTES = int(os.getenv("SESSION_HOLD_MINUTES", "3"))
SESSION_EXPIRE_MINUTES = int(os.getenv("SESSION_EXPIRE_MINUTES", "15"))
DEFAULT_WASH_DURATION_MINUTES = int(os.getenv("DEFAULT_WASH_DURATION_MINUTES", "30"))


# =========================================================
# Helpers
# =========================================================

def now_utc():
    return datetime.now(timezone.utc)

def gen_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"

def money_to_str(value) -> str:
    return f"{Decimal(value):.2f}"

def require_internal_secret():
    secret = request.headers.get("X-Internal-Secret", "")
    if not hmac.compare_digest(secret, INTERNAL_API_SECRET):
        abort(401, description="Invalid internal secret")

def require_machine_auth(machine):
    token = request.headers.get("X-Machine-Token", "")
    if not machine or not hmac.compare_digest(token, machine.device_token):
        abort(401, description="Invalid machine token")

def send_telegram_message(chat_id: int, text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text
    }

    try:
        r = requests.post(url, json=payload, timeout=10)
        return r.ok
    except Exception:
        return False

def session_is_expired(session) -> bool:
    if not session.expires_at:
        return False
    return session.expires_at <= now_utc()

def cleanup_expired_sessions():
    expired = WashSession.query.filter(
        WashSession.status.in_(["awaiting_payment", "reserved"]),
        WashSession.expires_at.isnot(None),
        WashSession.expires_at <= now_utc()
    ).all()

    for s in expired:
        s.status = "expired"

    if expired:
        db.session.commit()

def machine_public_url(machine):
    return f"{APP_BASE_URL}/m/{machine.public_token}"

def telegram_connect_link(session_id: str):
    # Customer taps this to open your bot and connect notifications
    return f"https://t.me/{TELEGRAM_BOT_USERNAME}?start=link_{session_id}"


# =========================================================
# Models
# =========================================================

class Machine(db.Model):
    __tablename__ = "machines"

    id = db.Column(db.Integer, primary_key=True)
    machine_code = db.Column(db.String(64), unique=True, nullable=False)
    public_token = db.Column(db.String(128), unique=True, nullable=False, index=True)
    device_token = db.Column(db.String(128), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    price_usd = db.Column(db.Numeric(10, 2), nullable=False, default=Decimal("1.00"))
    status = db.Column(db.String(40), nullable=False, default="available")
    current_session_id = db.Column(db.String(64), nullable=True)
    wash_duration_minutes = db.Column(db.Integer, nullable=False, default=DEFAULT_WASH_DURATION_MINUTES)
    heartbeat_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=now_utc)


class WashSession(db.Model):
    __tablename__ = "wash_sessions"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(64), unique=True, nullable=False, index=True)
    machine_id = db.Column(db.Integer, db.ForeignKey("machines.id"), nullable=False, index=True)
    status = db.Column(db.String(40), nullable=False, default="awaiting_payment")
    expected_amount = db.Column(db.Numeric(10, 2), nullable=False)
    telegram_user_id = db.Column(db.BigInteger, nullable=True)
    telegram_username = db.Column(db.String(120), nullable=True)
    payment_trx_id = db.Column(db.String(120), nullable=True, unique=True)
    payment_raw = db.Column(db.Text, nullable=True)
    payment_confirmed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    completed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    hold_until = db.Column(db.DateTime(timezone=True), nullable=True)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=now_utc)

    machine = db.relationship("Machine", backref="sessions")


class Command(db.Model):
    __tablename__ = "commands"

    id = db.Column(db.Integer, primary_key=True)
    command_id = db.Column(db.String(64), unique=True, nullable=False, index=True)
    machine_id = db.Column(db.Integer, db.ForeignKey("machines.id"), nullable=False, index=True)
    session_id = db.Column(db.String(64), nullable=False, index=True)
    command_type = db.Column(db.String(40), nullable=False)  # START_SERVICE
    payload_json = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(40), nullable=False, default="pending")  # pending, acked
    first_sent_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_sent_at = db.Column(db.DateTime(timezone=True), nullable=True)
    acked_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=now_utc)

    machine = db.relationship("Machine", backref="commands")


class PaymentEvent(db.Model):
    __tablename__ = "payment_events"

    id = db.Column(db.Integer, primary_key=True)
    machine_id = db.Column(db.Integer, db.ForeignKey("machines.id"), nullable=False, index=True)
    trx_id = db.Column(db.String(120), unique=True, nullable=False, index=True)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    raw_text = db.Column(db.Text, nullable=False)
    parsed_json = db.Column(db.Text, nullable=True)
    processed = db.Column(db.Boolean, nullable=False, default=False)
    matched_session_id = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=now_utc)

    machine = db.relationship("Machine", backref="payment_events")


# =========================================================
# Setup route
# =========================================================

@app.route("/setup/init", methods=["POST"])
def setup_init():
    """
    One-time helper to create a machine quickly.
    Protect this endpoint or remove it later.
    """
    require_internal_secret()

    body = request.get_json(silent=True) or {}
    machine_code = (body.get("machine_code") or "").strip()
    name = (body.get("name") or "").strip() or machine_code
    price_usd = Decimal(str(body.get("price_usd", "1.00")))
    wash_duration_minutes = int(body.get("wash_duration_minutes", DEFAULT_WASH_DURATION_MINUTES))

    if not machine_code:
        return jsonify({"error": "machine_code is required"}), 400

    existing = Machine.query.filter_by(machine_code=machine_code).first()
    if existing:
        return jsonify({
            "message": "Machine already exists",
            "machine_code": existing.machine_code,
            "public_url": machine_public_url(existing),
            "device_token": existing.device_token
        }), 200

    machine = Machine(
        machine_code=machine_code,
        public_token=gen_id("pub"),
        device_token=gen_id("dev"),
        name=name,
        price_usd=price_usd,
        status="available",
        wash_duration_minutes=wash_duration_minutes
    )
    db.session.add(machine)
    db.session.commit()

    return jsonify({
        "message": "Machine created",
        "machine_code": machine.machine_code,
        "public_url": machine_public_url(machine),
        "device_token": machine.device_token
    }), 201


# =========================================================
# Health
# =========================================================

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "time": now_utc().isoformat()
    })


# =========================================================
# Customer web flow
# =========================================================

@app.route("/m/<public_token>", methods=["GET"])
def machine_page(public_token):
    cleanup_expired_sessions()

    machine = Machine.query.filter_by(public_token=public_token).first_or_404()

    active_session = None
    if machine.current_session_id:
        active_session = WashSession.query.filter_by(session_id=machine.current_session_id).first()

    is_busy = machine.status in ["running", "busy", "starting"]
    is_available = machine.status == "available"

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>{machine.name}</title>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <style>
            body {{ font-family: Arial, sans-serif; max-width: 720px; margin: 0 auto; padding: 24px; line-height: 1.5; }}
            .card {{ border: 1px solid #ddd; border-radius: 12px; padding: 16px; margin-bottom: 16px; }}
            .btn {{
                display: inline-block; padding: 12px 18px; border-radius: 10px; text-decoration: none;
                background: black; color: white; margin-right: 8px; margin-top: 8px;
            }}
            .muted {{ color: #666; }}
            code {{ background: #f2f2f2; padding: 2px 6px; border-radius: 6px; }}
        </style>
    </head>
    <body>
        <h1>{machine.name}</h1>
        <div class="card">
            <p><strong>Status:</strong> {machine.status.upper()}</p>
            <p><strong>Price:</strong> ${money_to_str(machine.price_usd)}</p>
            <p><strong>Estimated duration:</strong> {machine.wash_duration_minutes} minutes</p>
        </div>
    """

    if is_busy:
        html += f"""
        <div class="card">
            <h3>Machine is busy</h3>
            <p>Please wait until the current wash finishes.</p>
            <p class="muted">Refresh this page later.</p>
        </div>
        """
    else:
        html += f"""
        <div class="card">
            <h3>Start a wash session</h3>
            <p>This machine has one fixed service and one fixed price.</p>
            <form method="POST" action="/api/public/{public_token}/reserve">
                <button class="btn" type="submit">Reserve This Machine</button>
            </form>
        </div>
        """

    html += """
    </body>
    </html>
    """
    return html


@app.route("/api/public/<public_token>/reserve", methods=["POST"])
def reserve_machine(public_token):
    cleanup_expired_sessions()

    machine = Machine.query.filter_by(public_token=public_token).first_or_404()

    if machine.status != "available":
        return make_response("""
            <h2>Machine is busy</h2>
            <p>Please try again later.</p>
        """, 409)

    # Prevent multiple open sessions for one machine
    open_session = WashSession.query.filter(
        WashSession.machine_id == machine.id,
        WashSession.status.in_(["awaiting_payment", "reserved", "paid", "starting", "running"])
    ).first()
    if open_session:
        return make_response("""
            <h2>Machine already has an active session</h2>
            <p>Please try again later.</p>
        """, 409)

    session = WashSession(
        session_id=gen_id("ws"),
        machine_id=machine.id,
        status="awaiting_payment",
        expected_amount=machine.price_usd,
        hold_until=now_utc() + timedelta(minutes=SESSION_HOLD_MINUTES),
        expires_at=now_utc() + timedelta(minutes=SESSION_EXPIRE_MINUTES)
    )
    db.session.add(session)

    machine.status = "reserved"
    machine.current_session_id = session.session_id
    db.session.commit()

    connect_link = telegram_connect_link(session.session_id)

    pay_url = None
    if ABA_PAY_URL_TEMPLATE:
        pay_url = ABA_PAY_URL_TEMPLATE.format(
            amount=money_to_str(session.expected_amount),
            session_id=session.session_id,
            machine_id=machine.machine_code
        )

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Session Reserved</title>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <style>
            body {{ font-family: Arial, sans-serif; max-width: 720px; margin: 0 auto; padding: 24px; line-height: 1.5; }}
            .card {{ border: 1px solid #ddd; border-radius: 12px; padding: 16px; margin-bottom: 16px; }}
            .btn {{
                display: inline-block; padding: 12px 18px; border-radius: 10px; text-decoration: none;
                background: black; color: white; margin-right: 8px; margin-top: 8px;
            }}
            .muted {{ color: #666; }}
        </style>
    </head>
    <body>
        <h1>Session Reserved</h1>

        <div class="card">
            <p><strong>Machine:</strong> {machine.name}</p>
            <p><strong>Session ID:</strong> {session.session_id}</p>
            <p><strong>Amount:</strong> ${money_to_str(session.expected_amount)}</p>
            <p><strong>Expires at:</strong> {session.expires_at.isoformat()}</p>
        </div>

        <div class="card">
            <h3>Optional: Get Telegram notification</h3>
            <p>Tap below to connect your Telegram account so the bot can notify you when the wash is done.</p>
            <a class="btn" href="{connect_link}">Connect Telegram</a>
        </div>
    """

    if pay_url:
        html += f"""
        <div class="card">
            <h3>Pay with ABA</h3>
            <p>Tap below to continue to payment.</p>
            <a class="btn" href="{pay_url}">Pay with ABA</a>
        </div>
        """
    else:
        html += """
        <div class="card">
            <h3>Pay with ABA</h3>
            <p class="muted">Set ABA_PAY_URL_TEMPLATE in Render environment variables.</p>
        </div>
        """

    html += f"""
        <div class="card">
            <h3>What happens next</h3>
            <p>Once payment is detected, the machine will start automatically.</p>
            <p class="muted">You may leave this page open if you want to see updates later.</p>
        </div>
    </body>
    </html>
    """
    return html


@app.route("/api/session/<session_id>", methods=["GET"])
def get_session(session_id):
    cleanup_expired_sessions()

    session = WashSession.query.filter_by(session_id=session_id).first_or_404()
    machine = session.machine

    return jsonify({
        "session_id": session.session_id,
        "machine_code": machine.machine_code,
        "machine_name": machine.name,
        "status": session.status,
        "expected_amount": money_to_str(session.expected_amount),
        "telegram_connected": bool(session.telegram_user_id),
        "payment_trx_id": session.payment_trx_id,
        "created_at": session.created_at.isoformat(),
        "started_at": session.started_at.isoformat() if session.started_at else None,
        "completed_at": session.completed_at.isoformat() if session.completed_at else None,
        "expires_at": session.expires_at.isoformat() if session.expires_at else None
    })


# =========================================================
# Telegram bot linking
# =========================================================

@app.route("/internal/telegram/link", methods=["POST"])
def internal_telegram_link():
    """
    Called by your Telegram bot service after user clicks:
    https://t.me/<bot>?start=link_<session_id>

    Example body:
    {
      "session_id": "ws_xxx",
      "telegram_user_id": 123456789,
      "telegram_username": "alice"
    }
    """
    require_internal_secret()
    body = request.get_json(silent=True) or {}

    session_id = (body.get("session_id") or "").strip()
    telegram_user_id = body.get("telegram_user_id")
    telegram_username = (body.get("telegram_username") or "").strip() or None

    if not session_id or not telegram_user_id:
        return jsonify({"error": "session_id and telegram_user_id are required"}), 400

    session = WashSession.query.filter_by(session_id=session_id).first()
    if not session:
        return jsonify({"error": "Session not found"}), 404

    if session.status in ["expired", "completed", "cancelled"]:
        return jsonify({"error": f"Cannot link Telegram to session in status {session.status}"}), 409

    session.telegram_user_id = int(telegram_user_id)
    session.telegram_username = telegram_username
    db.session.commit()

    return jsonify({
        "ok": True,
        "session_id": session.session_id,
        "telegram_user_id": session.telegram_user_id,
        "telegram_username": session.telegram_username
    })


# =========================================================
# Payment confirmation from Telegram worker
# =========================================================

@app.route("/internal/payment-events", methods=["POST"])
def internal_payment_events():
    """
    Your Telethon worker should call this after it reads the ABA message.

    Example body:
    {
      "machine_code": "WM-01",
      "trx_id": "177634032546561",
      "amount": "1.00",
      "raw_text": "$1.00 paid by ... Trx. ID: 177634032546561 ...",
      "parsed": {
        "payer_name": "HUY RAVY",
        "masked_account": "*411",
        "apv": "494779"
      }
    }
    """
    require_internal_secret()
    cleanup_expired_sessions()

    body = request.get_json(silent=True) or {}
    machine_code = (body.get("machine_code") or "").strip()
    trx_id = (body.get("trx_id") or "").strip()
    amount_raw = str(body.get("amount") or "").strip()
    raw_text = (body.get("raw_text") or "").strip()
    parsed = body.get("parsed") or {}

    if not machine_code or not trx_id or not amount_raw or not raw_text:
        return jsonify({"error": "machine_code, trx_id, amount, raw_text are required"}), 400

    machine = Machine.query.filter_by(machine_code=machine_code).first()
    if not machine:
        return jsonify({"error": "Machine not found"}), 404

    # De-dup by transaction ID
    existing_event = PaymentEvent.query.filter_by(trx_id=trx_id).first()
    if existing_event:
        return jsonify({
            "ok": True,
            "message": "Transaction already processed",
            "trx_id": trx_id,
            "matched_session_id": existing_event.matched_session_id
        }), 200

    try:
        amount = Decimal(amount_raw)
    except Exception:
        return jsonify({"error": "Invalid amount"}), 400

    payment_event = PaymentEvent(
        machine_id=machine.id,
        trx_id=trx_id,
        amount=amount,
        raw_text=raw_text,
        parsed_json=json.dumps(parsed)
    )
    db.session.add(payment_event)
    db.session.flush()

    # Find one matching pending session for this machine
    candidate = WashSession.query.filter(
        WashSession.machine_id == machine.id,
        WashSession.status.in_(["awaiting_payment", "reserved"]),
        WashSession.expected_amount == amount,
        WashSession.expires_at > now_utc()
    ).order_by(WashSession.created_at.asc()).first()

    if not candidate:
        db.session.commit()
        return jsonify({
            "ok": False,
            "message": "No matching active session found",
            "trx_id": trx_id
        }), 404

    # Confirm payment
    candidate.payment_trx_id = trx_id
    candidate.payment_raw = raw_text
    candidate.payment_confirmed_at = now_utc()
    candidate.status = "paid"

    machine.status = "starting"
    machine.current_session_id = candidate.session_id

    # Queue command for ESP32
    cmd = Command(
        command_id=gen_id("cmd"),
        machine_id=machine.id,
        session_id=candidate.session_id,
        command_type="START_SERVICE",
        payload_json=json.dumps({
            "command_id": gen_id("cmd_payload"),
            "type": "START_SERVICE",
            "session_id": candidate.session_id,
            "machine_code": machine.machine_code,
            "duration_minutes": machine.wash_duration_minutes
        }),
        status="pending"
    )
    db.session.add(cmd)

    payment_event.processed = True
    payment_event.matched_session_id = candidate.session_id

    db.session.commit()

    if candidate.telegram_user_id:
        send_telegram_message(
            candidate.telegram_user_id,
            f"Payment received for {machine.name}. Your wash will start now."
        )

    return jsonify({
        "ok": True,
        "machine_code": machine.machine_code,
        "session_id": candidate.session_id,
        "trx_id": trx_id,
        "command_id": cmd.command_id
    })


# =========================================================
# ESP32 endpoints
# =========================================================

@app.route("/esp32/<machine_code>/heartbeat", methods=["POST"])
def esp32_heartbeat(machine_code):
    machine = Machine.query.filter_by(machine_code=machine_code).first_or_404()
    require_machine_auth(machine)

    machine.heartbeat_at = now_utc()
    db.session.commit()

    return jsonify({
        "ok": True,
        "machine_code": machine.machine_code,
        "server_time": now_utc().isoformat()
    })


@app.route("/esp32/<machine_code>/next-command", methods=["GET"])
def esp32_next_command(machine_code):
    machine = Machine.query.filter_by(machine_code=machine_code).first_or_404()
    require_machine_auth(machine)

    machine.heartbeat_at = now_utc()

    pending = Command.query.filter_by(
        machine_id=machine.id,
        status="pending"
    ).order_by(Command.created_at.asc()).first()

    if not pending:
        db.session.commit()
        return jsonify({
            "ok": True,
            "has_command": False
        })

    now = now_utc()
    if not pending.first_sent_at:
        pending.first_sent_at = now
    pending.last_sent_at = now

    db.session.commit()

    payload = json.loads(pending.payload_json)

    return jsonify({
        "ok": True,
        "has_command": True,
        "command": {
            "db_command_id": pending.command_id,
            **payload
        }
    })


@app.route("/esp32/<machine_code>/ack", methods=["POST"])
def esp32_ack(machine_code):
    machine = Machine.query.filter_by(machine_code=machine_code).first_or_404()
    require_machine_auth(machine)

    body = request.get_json(silent=True) or {}
    db_command_id = (body.get("db_command_id") or "").strip()
    result = (body.get("result") or "done").strip()

    if not db_command_id:
        return jsonify({"error": "db_command_id is required"}), 400

    cmd = Command.query.filter_by(
        command_id=db_command_id,
        machine_id=machine.id
    ).first()

    if not cmd:
        return jsonify({"error": "Command not found"}), 404

    if cmd.status == "acked":
        return jsonify({"ok": True, "message": "Already acknowledged"}), 200

    cmd.status = "acked"
    cmd.acked_at = now_utc()

    session = WashSession.query.filter_by(session_id=cmd.session_id).first()
    if session and cmd.command_type == "START_SERVICE":
        session.status = "running"
        session.started_at = now_utc()

        machine.status = "running"
        machine.current_session_id = session.session_id

    db.session.commit()

    return jsonify({
        "ok": True,
        "command_id": cmd.command_id,
        "result": result
    })


@app.route("/esp32/<machine_code>/finished", methods=["POST"])
def esp32_finished(machine_code):
    machine = Machine.query.filter_by(machine_code=machine_code).first_or_404()
    require_machine_auth(machine)

    body = request.get_json(silent=True) or {}
    session_id = (body.get("session_id") or "").strip()

    if not session_id:
        return jsonify({"error": "session_id is required"}), 400

    session = WashSession.query.filter_by(session_id=session_id, machine_id=machine.id).first()
    if not session:
        return jsonify({"error": "Session not found"}), 404

    session.status = "completed"
    session.completed_at = now_utc()

    machine.status = "available"
    machine.current_session_id = None
    machine.heartbeat_at = now_utc()

    db.session.commit()

    if session.telegram_user_id:
        send_telegram_message(
            session.telegram_user_id,
            f"Your clothes are ready to pick up from {machine.name}."
        )

    return jsonify({
        "ok": True,
        "session_id": session.session_id,
        "machine_code": machine.machine_code,
        "machine_status": machine.status
    })


# =========================================================
# Admin/debug helpers
# =========================================================

@app.route("/admin/machines", methods=["GET"])
def admin_machines():
    require_internal_secret()
    rows = Machine.query.order_by(Machine.id.asc()).all()

    return jsonify([
        {
            "machine_code": m.machine_code,
            "name": m.name,
            "status": m.status,
            "price_usd": money_to_str(m.price_usd),
            "public_url": machine_public_url(m),
            "heartbeat_at": m.heartbeat_at.isoformat() if m.heartbeat_at else None,
            "current_session_id": m.current_session_id
        }
        for m in rows
    ])


@app.route("/admin/sessions", methods=["GET"])
def admin_sessions():
    require_internal_secret()
    rows = WashSession.query.order_by(WashSession.created_at.desc()).limit(100).all()

    return jsonify([
        {
            "session_id": s.session_id,
            "machine_code": s.machine.machine_code,
            "status": s.status,
            "expected_amount": money_to_str(s.expected_amount),
            "payment_trx_id": s.payment_trx_id,
            "telegram_user_id": s.telegram_user_id,
            "created_at": s.created_at.isoformat(),
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "completed_at": s.completed_at.isoformat() if s.completed_at else None,
            "expires_at": s.expires_at.isoformat() if s.expires_at else None
        }
        for s in rows
    ])


# =========================================================
# Boot
# =========================================================

with app.app_context():
    db.create_all()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)