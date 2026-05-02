import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, make_response, redirect, render_template_string, request, url_for
from flask_sqlalchemy import SQLAlchemy

load_dotenv()

app = Flask(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///laundry.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

APP_BASE_URL = os.getenv("APP_BASE_URL", "http://127.0.0.1:5000").rstrip("/")
INTERNAL_API_SECRET = os.getenv("INTERNAL_API_SECRET", "change-me").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@")
ABA_PAY_URL_TEMPLATE = os.getenv("ABA_PAY_URL_TEMPLATE", "").strip() or "https://link.payway.com.kh/example"
ABA_GROUP_ID = int(os.getenv("ABA_GROUP_ID", "-1002522488273"))
ABA_BOT_ID = int(os.getenv("ABA_BOT_ID", "1148497258"))
SESSION_EXPIRE_MINUTES = int(os.getenv("SESSION_EXPIRE_MINUTES", "3"))
DEFAULT_WASH_DURATION_MINUTES = int(os.getenv("DEFAULT_WASH_DURATION_MINUTES", "30"))
OFFLINE_AFTER_SECONDS = int(os.getenv("MACHINE_OFFLINE_SECONDS", "45"))
ADMIN_PANEL_USERNAME = os.getenv("ADMIN_PANEL_USERNAME", "admin").strip()
ADMIN_PANEL_PASSWORD = os.getenv("ADMIN_PANEL_PASSWORD", "admin123").strip()

MACHINE_AVAILABLE = "available"
MACHINE_RESERVED_UNPAID = "reserved_unpaid"
MACHINE_STARTING = "starting"
MACHINE_RUNNING = "running"
MACHINE_FAULT = "fault"
MACHINE_OFFLINE = "offline"

SESSION_AWAITING_PAYMENT = "awaiting_payment"
SESSION_PAYMENT_CONFIRMED = "payment_confirmed"
SESSION_RUNNING = "running"
SESSION_COMPLETED = "completed"
SESSION_EXPIRED = "expired"
SESSION_CANCELLED = "cancelled"

COMMAND_PENDING = "pending"
COMMAND_ACKED = "acked"
COMMAND_COMPLETED = "completed"

START_SERVICE = "START_SERVICE"
POWER_HOLD = "POWER_HOLD"
START_PAUSE_HOLD = "START_PAUSE_HOLD"
KNOB_CLOCKWISE = "KNOB_CLOCKWISE"
KNOB_COUNTERCLOCKWISE = "KNOB_COUNTERCLOCKWISE"

MACHINE_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{{ machine.name }}</title>
  <style>
    body { font-family: Arial, sans-serif; max-width: 760px; margin: 0 auto; padding: 24px; background: #f6f7f9; color: #1e293b; }
    .card { background: white; border-radius: 16px; padding: 20px; margin-bottom: 16px; box-shadow: 0 6px 18px rgba(15, 23, 42, 0.08); }
    .status { display: inline-block; padding: 8px 12px; border-radius: 999px; background: #e2e8f0; font-weight: 700; text-transform: uppercase; font-size: 12px; }
    .btn { display: inline-block; border: 0; border-radius: 12px; padding: 14px 18px; background: #0f766e; color: white; font-weight: 700; cursor: pointer; text-decoration: none; }
    .muted { color: #64748b; }
  </style>
</head>
<body>
  <div class="card">
    <div class="status" id="status">{{ initial.status }}</div>
    <h1>{{ machine.name }}</h1>
    <p class="muted">Machine code: {{ machine.machine_code }}</p>
    <p><strong>Price:</strong> $<span id="price">{{ initial.price }}</span></p>
    <p id="message"></p>
    <form id="reserve-form" method="post" action="/api/public/{{ machine.public_token }}/reserve">
      <button id="reserve-button" class="btn" type="submit">Reserve</button>
    </form>
  </div>

  <script>
    const reserveForm = document.getElementById("reserve-form");
    const reserveButton = document.getElementById("reserve-button");
    const statusEl = document.getElementById("status");
    const messageEl = document.getElementById("message");

    function formatClock(seconds) {
      if (seconds == null || seconds <= 0) return "00:00";
      const mins = Math.floor(seconds / 60);
      const secs = seconds % 60;
      return String(mins).padStart(2, "0") + ":" + String(secs).padStart(2, "0");
    }

    function render(machine) {
      statusEl.textContent = machine.status;
      let text = "";
      let showReserve = false;

      if (machine.status === "available") {
        text = "Machine is available. Press Reserve to hold it for " + machine.expire_minutes + " minutes.";
        showReserve = true;
      } else if (machine.status === "reserved_unpaid") {
        text = "Temporary reservation active. Payment window left: " + formatClock(machine.reservation_seconds_left) + ".";
      } else if (machine.status === "starting") {
        text = "Payment received. Waiting for ESP32 to acknowledge start.";
      } else if (machine.status === "running") {
        text = "Machine is running.";
      } else if (machine.status === "offline") {
        text = "Machine is offline.";
      } else if (machine.status === "fault") {
        text = "Machine is in fault state.";
      } else {
        text = "Machine is busy.";
      }

      messageEl.textContent = text;
      reserveForm.style.display = showReserve ? "block" : "none";
      reserveButton.disabled = !showReserve;
    }

    async function refreshMachine() {
      const res = await fetch("/api/public/{{ machine.public_token }}/status", { cache: "no-store" });
      const data = await res.json();
      render(data.machine);
    }

    render({{ initial|tojson }});
    setInterval(refreshMachine, 3000);
  </script>
</body>
</html>
"""

SESSION_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Session {{ session.session_id }}</title>
  <style>
    body { font-family: Arial, sans-serif; max-width: 760px; margin: 0 auto; padding: 24px; background: #f6f7f9; color: #1e293b; }
    .card { background: white; border-radius: 16px; padding: 20px; margin-bottom: 16px; box-shadow: 0 6px 18px rgba(15, 23, 42, 0.08); }
    .btn { display: inline-block; border-radius: 12px; padding: 14px 18px; background: #0f766e; color: white; font-weight: 700; text-decoration: none; }
    .btn.alt { background: #2563eb; }
    .status { font-size: 24px; font-weight: 700; margin-bottom: 12px; }
    .timer { font-size: 36px; font-weight: 800; margin: 8px 0; }
    .muted { color: #64748b; }
    .actions { display: flex; gap: 12px; flex-wrap: wrap; }
  </style>
</head>
<body>
  <div class="card">
    <div class="status" id="status-text">{{ initial_message }}</div>
    <div class="timer" id="timer">--:--</div>
    <p><strong>Session:</strong> {{ session.session_id }}</p>
    <p><strong>Amount:</strong> ${{ amount }}</p>
    <p><strong>Expires at:</strong> {{ expires_at }}</p>
    <p class="muted" id="detail-text">Polling every 3 seconds.</p>
  </div>

  <div class="card">
    <div class="actions">
      {% if telegram_link %}
      <a class="btn alt" href="{{ telegram_link }}" target="_blank" rel="noopener noreferrer">Connect Telegram</a>
      {% endif %}
      <a class="btn" href="{{ pay_link }}" target="_blank" rel="noopener noreferrer">Pay with ABA</a>
    </div>
  </div>

  <script>
    const sessionId = "{{ session.session_id }}";
    const statusText = document.getElementById("status-text");
    const timerEl = document.getElementById("timer");
    const detailText = document.getElementById("detail-text");

    function formatClock(seconds) {
      if (seconds == null || seconds <= 0) return "00:00";
      const mins = Math.floor(seconds / 60);
      const secs = seconds % 60;
      return String(mins).padStart(2, "0") + ":" + String(secs).padStart(2, "0");
    }

    function render(session) {
      if (session.status === "awaiting_payment") {
        statusText.textContent = "Waiting for payment";
        timerEl.textContent = formatClock(session.seconds_until_expiry);
        detailText.textContent = "Complete payment before the reservation expires.";
      } else if (session.status === "payment_confirmed") {
        statusText.textContent = "Payment received. Starting machine...";
        timerEl.textContent = "READY";
        detailText.textContent = "Backend sent the start command. Waiting for ESP32 ACK.";
      } else if (session.status === "running") {
        statusText.textContent = "Machine is running";
        timerEl.textContent = formatClock(session.remaining_seconds);
        detailText.textContent = "The ESP32 acknowledged the command and the wash is active.";
      } else if (session.status === "completed") {
        statusText.textContent = "Wash complete";
        timerEl.textContent = "DONE";
        detailText.textContent = "The machine reported the cycle as finished.";
      } else if (session.status === "expired") {
        statusText.textContent = "Reservation expired";
        timerEl.textContent = "00:00";
        detailText.textContent = "The unpaid reservation timed out.";
      } else if (session.status === "cancelled") {
        statusText.textContent = "Reservation cancelled";
        timerEl.textContent = "00:00";
        detailText.textContent = "The reservation was cancelled.";
      } else {
        statusText.textContent = session.status;
        timerEl.textContent = "--:--";
      }
    }

    async function refreshSession() {
      const res = await fetch("/api/session/" + sessionId, { cache: "no-store" });
      const data = await res.json();
      render(data.session);
    }

    render({{ initial_payload|tojson }});
    setInterval(refreshSession, 3000);
  </script>
</body>
</html>
"""

ADMIN_PANEL_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Admin Control Panel</title>
  <style>
    body { font-family: Arial, sans-serif; max-width: 980px; margin: 0 auto; padding: 24px; background: #0f172a; color: #e2e8f0; }
    .card { background: #111827; border: 1px solid #334155; border-radius: 16px; padding: 20px; margin-bottom: 16px; }
    .row { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }
    .machine-title { margin: 0 0 12px; }
    .pill { display: inline-block; padding: 6px 10px; border-radius: 999px; background: #1e293b; font-size: 12px; font-weight: 700; text-transform: uppercase; }
    .muted { color: #94a3b8; }
    .actions { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 14px; }
    .topbar { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; justify-content: space-between; }
    button { border: 0; border-radius: 12px; padding: 12px 14px; font-weight: 700; cursor: pointer; color: white; background: #2563eb; }
    button.warn { background: #b45309; }
    button.ok { background: #0f766e; }
    button.alt { background: #475569; }
    button.danger { background: #b91c1c; }
    input[type="number"] { width: 80px; border-radius: 10px; border: 1px solid #475569; background: #0f172a; color: white; padding: 10px; }
    input[type="text"] { width: min(420px, 100%); border-radius: 10px; border: 1px solid #475569; background: #0f172a; color: white; padding: 10px; }
    .log { font-family: Consolas, monospace; white-space: pre-wrap; color: #cbd5e1; font-size: 13px; }
    table { width: 100%; border-collapse: collapse; margin-top: 10px; }
    th, td { text-align: left; padding: 10px 8px; border-bottom: 1px solid #334155; vertical-align: top; }
    th { color: #94a3b8; font-size: 12px; text-transform: uppercase; }
    .stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 16px; }
    .stat-box { background: #111827; border: 1px solid #334155; border-radius: 16px; padding: 16px; }
    .stat-label { color: #94a3b8; font-size: 12px; text-transform: uppercase; }
    .stat-value { font-size: 28px; font-weight: 800; margin-top: 6px; }
  </style>
</head>
<body>
  <div class="topbar">
    <div>
      <h1>Washing Machine Admin Panel</h1>
      <p class="muted">Protected with HTTP basic auth. Control buttons enqueue commands for ESP32 polling.</p>
    </div>
    <div class="actions">
      <button class="danger" onclick="clearCommands()">Clear Recent Command Log</button>
    </div>
  </div>
  <div id="stats"></div>
  <div id="machines"></div>
  <div id="sessions"></div>
  <div id="payments"></div>
  <script>
    const statsEl = document.getElementById("stats");
    const machinesEl = document.getElementById("machines");
    const sessionsEl = document.getElementById("sessions");
    const paymentsEl = document.getElementById("payments");

    function machineCard(machine) {
      const knobId = "knob-" + machine.machine_code;
      const priceId = "price-" + machine.machine_code;
      return `
        <div class="card">
          <h2 class="machine-title">${machine.name} <span class="pill">${machine.machine_code}</span></h2>
          <div class="row">
            <div><strong>Status:</strong> ${machine.status}</div>
            <div><strong>Current Session:</strong> ${machine.current_session_id || "-"}</div>
            <div><strong>Heartbeat:</strong> ${machine.heartbeat_at || "-"}</div>
            <div><strong>Public URL:</strong> <a href="${machine.public_url}" target="_blank" style="color:#93c5fd">${machine.public_url}</a></div>
          </div>
          <div class="actions">
            <button class="warn" onclick="sendCommand('${machine.machine_code}', 'POWER_HOLD')">Hold Power 2s</button>
            <button class="ok" onclick="sendCommand('${machine.machine_code}', 'START_PAUSE_HOLD')">Hold Start/Pause 2s</button>
            <button onclick="sendKnob('${machine.machine_code}', 'KNOB_CLOCKWISE', '${knobId}')">Knob Clockwise</button>
            <button class="alt" onclick="sendKnob('${machine.machine_code}', 'KNOB_COUNTERCLOCKWISE', '${knobId}')">Knob Counterclockwise</button>
            <label>Steps <input id="${knobId}" type="number" min="1" max="12" value="1" /></label>
            <label>Price <input id="${priceId}" type="number" min="0.01" step="0.01" value="${machine.price}" /></label>
            <button onclick="updatePrice('${machine.machine_code}', '${priceId}')">Save Price</button>
          </div>
        </div>
      `;
    }

    function money(value) {
      return "$" + Number(value || 0).toFixed(2);
    }

    function renderStats(summary) {
      statsEl.innerHTML = `
        <div class="stat-grid">
          <div class="stat-box"><div class="stat-label">Total Revenue</div><div class="stat-value">${money(summary.total_revenue)}</div></div>
          <div class="stat-box"><div class="stat-label">Completed Sessions</div><div class="stat-value">${summary.completed_sessions}</div></div>
          <div class="stat-box"><div class="stat-label">Awaiting Payment</div><div class="stat-value">${summary.awaiting_payment_sessions}</div></div>
          <div class="stat-box"><div class="stat-label">Running Sessions</div><div class="stat-value">${summary.running_sessions}</div></div>
          <div class="stat-box"><div class="stat-label">Machines</div><div class="stat-value">${summary.machine_count}</div></div>
          <div class="stat-box"><div class="stat-label">Payment Events</div><div class="stat-value">${summary.payment_count}</div></div>
        </div>
      `;
    }

    function renderSessions(sessions) {
      sessionsEl.innerHTML = `
        <div class="card">
          <h3>Sessions</h3>
          <table>
            <thead>
              <tr>
                <th>Session</th>
                <th>Status</th>
                <th>Amount</th>
                <th>Machine</th>
                <th>Created</th>
                <th>Started</th>
                <th>Completed</th>
              </tr>
            </thead>
            <tbody>
              ${sessions.map(session => `
                <tr>
                  <td>${session.session_id}</td>
                  <td>${session.status}</td>
                  <td>${money(session.expected_amount)}</td>
                  <td>${session.machine_code || "-"}</td>
                  <td>${session.created_at || "-"}</td>
                  <td>${session.started_at || "-"}</td>
                  <td>${session.completed_at || "-"}</td>
                </tr>
              `).join("") || `<tr><td colspan="7">No sessions yet.</td></tr>`}
            </tbody>
          </table>
        </div>
      `;
    }

    function renderPayments(payments) {
      paymentsEl.innerHTML = `
        <div class="card">
          <h3>Payments</h3>
          <table>
            <thead>
              <tr>
                <th>Trx ID</th>
                <th>Amount</th>
                <th>Machine</th>
                <th>Session</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              ${payments.map(payment => `
                <tr>
                  <td>${payment.trx_id}</td>
                  <td>${money(payment.amount)}</td>
                  <td>${payment.machine_code}</td>
                  <td>${payment.session_id}</td>
                  <td>${payment.created_at || "-"}</td>
                </tr>
              `).join("") || `<tr><td colspan="5">No payments yet.</td></tr>`}
            </tbody>
          </table>
        </div>
      `;
    }

    async function refresh() {
      const response = await fetch("/admin/panel/data", { cache: "no-store" });
      const payload = await response.json();
      renderStats(payload.summary);
      const settingsHtml = `
        <div class="card">
          <h3>Payment Settings</h3>
          <div class="actions">
            <label>ABA Pay URL Template <input id="aba-url-template" type="text" value="${payload.settings.aba_pay_url_template || ""}" /></label>
            <button onclick="savePaymentUrl()">Save Payment URL</button>
          </div>
          <p class="muted">Supports placeholders like {amount}, {session_id}, and {machine_id}. Fixed links work too.</p>
        </div>
      `;
      machinesEl.innerHTML = payload.machines.map(machineCard).join("") + `
        ${settingsHtml}
        <div class="card">
          <h3>Recent Commands</h3>
          <div class="log">${payload.commands.map(c => `${c.created_at}  ${c.command_type}  ${c.status}  ${c.machine_code}  ${c.session_id || '-'}`).join("\\n") || "No commands yet."}</div>
        </div>
      `;
      renderSessions(payload.sessions);
      renderPayments(payload.payments);
    }

    async function sendCommand(machineCode, commandType, extra = {}) {
      const response = await fetch("/admin/panel/" + machineCode + "/command", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ command_type: commandType, ...extra })
      });
      const payload = await response.json();
      if (!response.ok) {
        alert(payload.error || "Command failed");
        return;
      }
      await refresh();
    }

    async function sendKnob(machineCode, commandType, knobId) {
      const steps = Number(document.getElementById(knobId).value || "1");
      await sendCommand(machineCode, commandType, { steps });
    }

    async function updatePrice(machineCode, priceId) {
      const price = document.getElementById(priceId).value;
      const response = await fetch("/admin/panel/" + machineCode + "/price", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ price_usd: price })
      });
      const payload = await response.json();
      if (!response.ok) {
        alert(payload.error || "Failed to update price");
        return;
      }
      await refresh();
    }

    async function clearCommands() {
      const response = await fetch("/admin/panel/clear-commands", { method: "POST" });
      const payload = await response.json();
      if (!response.ok) {
        alert(payload.error || "Failed to clear commands");
        return;
      }
      await refresh();
    }

    async function savePaymentUrl() {
      const value = document.getElementById("aba-url-template").value;
      const response = await fetch("/admin/panel/payment-url", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ aba_pay_url_template: value })
      });
      const payload = await response.json();
      if (!response.ok) {
        alert(payload.error || "Failed to save payment URL");
        return;
      }
      await refresh();
    }

    refresh();
    setInterval(refresh, 3000);
  </script>
</body>
</html>
"""


def now_utc():
    return datetime.now(timezone.utc)


def normalize_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def seconds_until(value):
    target = normalize_utc(value)
    if target is None:
        return 0
    return max(0, int((target - now_utc()).total_seconds()))


def seconds_since(value):
    target = normalize_utc(value)
    if target is None:
        return 0
    return max(0, int((now_utc() - target).total_seconds()))


def gen_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def decimal_to_str(value):
    return format(Decimal(value), ".2f")


def parse_decimal(raw_value):
    try:
        return Decimal(str(raw_value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None


def to_iso(value):
    normalized = normalize_utc(value)
    return normalized.isoformat() if normalized else None


def public_url_for(machine):
    return f"{APP_BASE_URL}/m/{machine.public_token}"


def get_setting(key, default_value=""):
    row = AppSetting.query.filter_by(key=key).first()
    return row.value if row else default_value


def set_setting(key, value):
    row = AppSetting.query.filter_by(key=key).first()
    if row:
        row.value = value
    else:
        row = AppSetting(key=key, value=value)
        db.session.add(row)
    return row


def build_pay_link(machine, session):
    template = get_setting("aba_pay_url_template", ABA_PAY_URL_TEMPLATE)
    if "{" not in template:
        return template
    return template.format(
        amount=decimal_to_str(session.expected_amount),
        session_id=session.session_id,
        machine_id=machine.machine_code,
    )


def build_telegram_link(session_id):
    if not TELEGRAM_BOT_USERNAME:
        return None
    return f"https://t.me/{TELEGRAM_BOT_USERNAME}?start=link_{quote(session_id)}"


def require_internal_secret():
    if request.headers.get("X-Internal-Secret") != INTERNAL_API_SECRET:
        return make_response(jsonify({"ok": False, "error": "unauthorized"}), 401)
    return None


def require_admin_panel_auth():
    auth = request.authorization
    if auth and auth.username == ADMIN_PANEL_USERNAME and auth.password == ADMIN_PANEL_PASSWORD:
        return None

    response = make_response("Authentication required", 401)
    response.headers["WWW-Authenticate"] = 'Basic realm="Washing Machine Admin"'
    return response


def cleanup_expired_sessions():
    expired_sessions = WashSession.query.filter_by(status=SESSION_AWAITING_PAYMENT).all()
    changed = False
    for session in expired_sessions:
        if seconds_until(session.expires_at) > 0:
            continue
        session.status = SESSION_EXPIRED
        machine = db.session.get(Machine, session.machine_id)
        if machine and machine.current_session_id == session.session_id:
            machine.current_session_id = None
            if machine.status == MACHINE_RESERVED_UNPAID:
                machine.status = MACHINE_AVAILABLE
        changed = True
    if changed:
        db.session.commit()


def computed_machine_status(machine):
    if machine.status in {MACHINE_RUNNING, MACHINE_STARTING, MACHINE_FAULT}:
        return machine.status
    if machine.heartbeat_at and seconds_since(machine.heartbeat_at) > OFFLINE_AFTER_SECONDS:
        return MACHINE_OFFLINE
    return machine.status


class Machine(db.Model):
    __tablename__ = "machines"

    id = db.Column(db.Integer, primary_key=True)
    machine_code = db.Column(db.String(64), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    price_usd = db.Column(db.Numeric(10, 2), nullable=False)
    status = db.Column(db.String(32), nullable=False, default=MACHINE_AVAILABLE)
    current_session_id = db.Column(db.String(64), nullable=True)
    device_token = db.Column(db.String(64), unique=True, nullable=False)
    public_token = db.Column(db.String(64), unique=True, nullable=False)
    wash_duration_minutes = db.Column(db.Integer, nullable=False, default=DEFAULT_WASH_DURATION_MINUTES)
    heartbeat_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=now_utc)


class WashSession(db.Model):
    __tablename__ = "wash_sessions"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(64), unique=True, nullable=False)
    machine_id = db.Column(db.Integer, db.ForeignKey("machines.id"), nullable=False)
    status = db.Column(db.String(32), nullable=False, default=SESSION_AWAITING_PAYMENT)
    expected_amount = db.Column(db.Numeric(10, 2), nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    telegram_user_id = db.Column(db.String(64), nullable=True)
    telegram_username = db.Column(db.String(64), nullable=True)
    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    completed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=now_utc)


class Command(db.Model):
    __tablename__ = "commands"

    id = db.Column(db.Integer, primary_key=True)
    command_id = db.Column(db.String(64), unique=True, nullable=False)
    machine_id = db.Column(db.Integer, db.ForeignKey("machines.id"), nullable=False)
    session_id = db.Column(db.String(64), nullable=False)
    payload_json = db.Column(db.Text, nullable=False)
    command_type = db.Column(db.String(32), nullable=False, default=START_SERVICE)
    status = db.Column(db.String(32), nullable=False, default=COMMAND_PENDING)
    duration_minutes = db.Column(db.Integer, nullable=False, default=DEFAULT_WASH_DURATION_MINUTES)
    steps = db.Column(db.Integer, nullable=False, default=1)
    acked_at = db.Column(db.DateTime(timezone=True), nullable=True)
    completed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=now_utc)


class PaymentEvent(db.Model):
    __tablename__ = "payment_events"

    id = db.Column(db.Integer, primary_key=True)
    trx_id = db.Column(db.String(128), unique=True, nullable=False)
    machine_code = db.Column(db.String(64), nullable=False)
    session_id = db.Column(db.String(64), nullable=False)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    raw_text = db.Column(db.Text, nullable=True)
    parsed_json = db.Column(db.Text, nullable=True)
    source_chat_id = db.Column(db.String(64), nullable=False)
    source_sender_id = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=now_utc)


class AppSetting(db.Model):
    __tablename__ = "app_settings"

    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.Text, nullable=False)


def serialize_machine(machine):
    reservation_seconds_left = None
    if machine.current_session_id:
        session = WashSession.query.filter_by(session_id=machine.current_session_id).first()
        if session and session.status == SESSION_AWAITING_PAYMENT:
            reservation_seconds_left = seconds_until(session.expires_at)

    return {
        "machine_code": machine.machine_code,
        "name": machine.name,
        "status": computed_machine_status(machine),
        "price": decimal_to_str(machine.price_usd),
        "current_session_id": machine.current_session_id,
        "reservation_seconds_left": reservation_seconds_left,
        "expire_minutes": SESSION_EXPIRE_MINUTES,
        "wash_duration_minutes": machine.wash_duration_minutes,
        "heartbeat_at": to_iso(machine.heartbeat_at),
        "public_url": public_url_for(machine),
        "device_token": machine.device_token,
    }


def serialize_session(session):
    machine = db.session.get(Machine, session.machine_id)
    remaining_seconds = None
    if session.status == SESSION_RUNNING and session.started_at:
        remaining_seconds = max(
            0,
            machine.wash_duration_minutes * 60 - seconds_since(session.started_at),
        )

    return {
        "session_id": session.session_id,
        "machine_id": session.machine_id,
        "machine_code": machine.machine_code if machine else None,
        "status": session.status,
        "expected_amount": decimal_to_str(session.expected_amount),
        "expires_at": to_iso(session.expires_at),
        "seconds_until_expiry": seconds_until(session.expires_at),
        "duration_minutes": machine.wash_duration_minutes,
        "remaining_seconds": remaining_seconds,
        "telegram_user_id": session.telegram_user_id,
        "telegram_username": session.telegram_username,
        "started_at": to_iso(session.started_at),
        "completed_at": to_iso(session.completed_at),
        "created_at": to_iso(session.created_at),
    }


def serialize_command(command):
    machine = db.session.get(Machine, command.machine_id)
    return {
        "command_id": command.command_id,
        "machine_id": command.machine_id,
        "session_id": command.session_id,
        "payload_json": command.payload_json,
        "command_type": command.command_type,
        "status": command.status,
        "duration_minutes": command.duration_minutes,
        "steps": command.steps,
        "acked_at": to_iso(command.acked_at),
        "completed_at": to_iso(command.completed_at),
        "created_at": to_iso(command.created_at),
        "machine_code": machine.machine_code if machine else None,
    }


def serialize_payment(payment):
    return {
        "trx_id": payment.trx_id,
        "machine_code": payment.machine_code,
        "session_id": payment.session_id,
        "amount": decimal_to_str(payment.amount),
        "created_at": to_iso(payment.created_at),
    }


def send_finish_notification(session, machine):
    if not TELEGRAM_BOT_TOKEN or not session.telegram_user_id:
        return

    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": session.telegram_user_id,
                "text": f"Wash complete for machine {machine.machine_code}. Session {session.session_id}.",
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        print(f"[notify] failed: {exc}")


def machine_auth(machine_code):
    cleanup_expired_sessions()
    token = request.headers.get("X-Machine-Token")
    machine = Machine.query.filter_by(machine_code=machine_code).first()
    if not machine or machine.device_token != token:
        return None
    return machine


def queue_machine_command(machine, command_type, session_id="", duration_minutes=0, steps=1):
    payload = {
        "type": command_type,
        "machine_code": machine.machine_code,
        "session_id": session_id,
        "duration_minutes": duration_minutes,
        "steps": steps,
    }
    command = Command(
        command_id=gen_id("cmd"),
        machine_id=machine.id,
        session_id=session_id,
        payload_json=json.dumps(payload),
        command_type=command_type,
        status=COMMAND_PENDING,
        duration_minutes=duration_minutes,
        steps=steps,
    )
    db.session.add(command)
    return command


@app.route("/health")
def health():
    cleanup_expired_sessions()
    return jsonify(
        {
            "ok": True,
            "time": to_iso(now_utc()),
            "counts": {
                "machines": Machine.query.count(),
                "sessions": WashSession.query.count(),
                "commands": Command.query.count(),
                "payments": PaymentEvent.query.count(),
            },
        }
    )


@app.route("/admin/machines", methods=["POST"])
@app.route("/setup/init", methods=["POST"])
def create_machine():
    auth = require_internal_secret()
    if auth:
        return auth

    data = request.get_json(silent=True) or {}
    machine_code = (data.get("machine_code") or "").strip()
    name = (data.get("name") or machine_code).strip()
    price = parse_decimal(data.get("price_usd"))
    duration = int(data.get("wash_duration_minutes") or DEFAULT_WASH_DURATION_MINUTES)

    if not machine_code or price is None:
        return jsonify({"ok": False, "error": "machine_code_and_price_required"}), 400

    existing = Machine.query.filter_by(machine_code=machine_code).first()
    if existing:
        return jsonify({"ok": False, "error": "machine_exists", "machine": serialize_machine(existing)}), 409

    machine = Machine(
        machine_code=machine_code,
        name=name,
        price_usd=price,
        status=MACHINE_AVAILABLE,
        device_token=gen_id("dev"),
        public_token=gen_id("pub"),
        wash_duration_minutes=duration,
    )
    db.session.add(machine)
    db.session.commit()

    return jsonify({"ok": True, "machine": serialize_machine(machine)})


@app.route("/admin/machines", methods=["GET"])
def list_machines():
    auth = require_internal_secret()
    if auth:
        return auth
    cleanup_expired_sessions()
    return jsonify({"ok": True, "machines": [serialize_machine(row) for row in Machine.query.order_by(Machine.id).all()]})


@app.route("/admin/sessions", methods=["GET"])
def list_sessions():
    auth = require_internal_secret()
    if auth:
        return auth
    cleanup_expired_sessions()
    return jsonify({"ok": True, "sessions": [serialize_session(row) for row in WashSession.query.order_by(WashSession.created_at.desc()).all()]})


@app.route("/admin/commands", methods=["GET"])
def list_commands():
    auth = require_internal_secret()
    if auth:
        return auth
    return jsonify({"ok": True, "commands": [serialize_command(row) for row in Command.query.order_by(Command.created_at.desc()).all()]})


@app.route("/admin/reset-machine/<machine_code>", methods=["POST"])
def reset_machine(machine_code):
    auth = require_internal_secret()
    if auth:
        return auth

    machine = Machine.query.filter_by(machine_code=machine_code).first()
    if not machine:
        return jsonify({"ok": False, "error": "machine_not_found"}), 404

    if machine.current_session_id:
        session = WashSession.query.filter_by(session_id=machine.current_session_id).first()
        if session and session.status not in {SESSION_COMPLETED, SESSION_EXPIRED, SESSION_CANCELLED}:
            session.status = SESSION_CANCELLED

    for command in Command.query.filter(
        Command.machine_id == machine.id,
        Command.status.in_([COMMAND_PENDING, COMMAND_ACKED]),
    ).all():
        command.status = COMMAND_COMPLETED
        command.completed_at = now_utc()

    machine.current_session_id = None
    machine.status = MACHINE_AVAILABLE
    db.session.commit()

    return jsonify({"ok": True, "machine": serialize_machine(machine)})


@app.route("/admin/delete-machine/<machine_code>", methods=["POST"])
def delete_machine(machine_code):
    auth = require_internal_secret()
    if auth:
        return auth

    machine = Machine.query.filter_by(machine_code=machine_code).first()
    if not machine:
        return jsonify({"ok": False, "error": "machine_not_found"}), 404

    Command.query.filter_by(machine_id=machine.id).delete()
    WashSession.query.filter_by(machine_id=machine.id).delete()
    PaymentEvent.query.filter_by(machine_code=machine.machine_code).delete()
    db.session.delete(machine)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/admin/nuke-db", methods=["POST"])
def nuke_db():
    auth = require_internal_secret()
    if auth:
        return auth

    PaymentEvent.query.delete()
    Command.query.delete()
    WashSession.query.delete()
    Machine.query.delete()
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/admin/panel")
def admin_panel():
    auth = require_admin_panel_auth()
    if auth:
        return auth
    return render_template_string(ADMIN_PANEL_TEMPLATE)


@app.route("/admin/panel/data")
def admin_panel_data():
    auth = require_admin_panel_auth()
    if auth:
        return auth
    cleanup_expired_sessions()
    machines = [serialize_machine(row) for row in Machine.query.order_by(Machine.id).all()]
    commands = [serialize_command(row) for row in Command.query.order_by(Command.id.desc()).limit(20).all()]
    sessions = [serialize_session(row) for row in WashSession.query.order_by(WashSession.created_at.desc()).limit(20).all()]
    payments = [serialize_payment(row) for row in PaymentEvent.query.order_by(PaymentEvent.created_at.desc()).limit(20).all()]

    total_revenue = Decimal("0.00")
    for payment in PaymentEvent.query.all():
        total_revenue += Decimal(payment.amount)

    summary = {
        "total_revenue": decimal_to_str(total_revenue),
        "completed_sessions": WashSession.query.filter_by(status=SESSION_COMPLETED).count(),
        "awaiting_payment_sessions": WashSession.query.filter_by(status=SESSION_AWAITING_PAYMENT).count(),
        "running_sessions": WashSession.query.filter_by(status=SESSION_RUNNING).count(),
        "machine_count": Machine.query.count(),
        "payment_count": PaymentEvent.query.count(),
    }
    settings = {
        "aba_pay_url_template": get_setting("aba_pay_url_template", ABA_PAY_URL_TEMPLATE),
    }

    return jsonify(
        {
            "ok": True,
            "summary": summary,
            "settings": settings,
            "machines": machines,
            "commands": commands,
            "sessions": sessions,
            "payments": payments,
        }
    )


@app.route("/admin/panel/<machine_code>/command", methods=["POST"])
def admin_panel_command(machine_code):
    auth = require_admin_panel_auth()
    if auth:
        return auth

    machine = Machine.query.filter_by(machine_code=machine_code).first()
    if not machine:
        return jsonify({"ok": False, "error": "machine_not_found"}), 404

    data = request.get_json(silent=True) or {}
    command_type = (data.get("command_type") or "").strip()
    steps = max(1, min(12, int(data.get("steps") or 1)))

    if command_type not in {POWER_HOLD, START_PAUSE_HOLD, KNOB_CLOCKWISE, KNOB_COUNTERCLOCKWISE}:
        return jsonify({"ok": False, "error": "invalid_command_type"}), 400

    command = queue_machine_command(
        machine,
        command_type=command_type,
        session_id=machine.current_session_id or "",
        duration_minutes=0,
        steps=steps,
    )
    db.session.commit()
    return jsonify({"ok": True, "command": serialize_command(command)})


@app.route("/admin/panel/clear-commands", methods=["POST"])
def admin_panel_clear_commands():
    auth = require_admin_panel_auth()
    if auth:
        return auth

    Command.query.delete()
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/admin/panel/<machine_code>/price", methods=["POST"])
def admin_panel_update_price(machine_code):
    auth = require_admin_panel_auth()
    if auth:
        return auth

    machine = Machine.query.filter_by(machine_code=machine_code).first()
    if not machine:
        return jsonify({"ok": False, "error": "machine_not_found"}), 404

    data = request.get_json(silent=True) or {}
    price = parse_decimal(data.get("price_usd"))
    if price is None or price <= 0:
        return jsonify({"ok": False, "error": "invalid_price"}), 400

    machine.price_usd = price
    db.session.commit()
    return jsonify({"ok": True, "machine": serialize_machine(machine)})


@app.route("/admin/panel/payment-url", methods=["POST"])
def admin_panel_update_payment_url():
    auth = require_admin_panel_auth()
    if auth:
        return auth

    data = request.get_json(silent=True) or {}
    value = str(data.get("aba_pay_url_template") or "").strip()
    if not value:
        return jsonify({"ok": False, "error": "payment_url_required"}), 400

    set_setting("aba_pay_url_template", value)
    db.session.commit()
    return jsonify({"ok": True, "aba_pay_url_template": value})


@app.route("/m/<public_token>")
def machine_page(public_token):
    cleanup_expired_sessions()
    machine = Machine.query.filter_by(public_token=public_token).first_or_404()
    return render_template_string(MACHINE_TEMPLATE, machine=machine, initial=serialize_machine(machine))


@app.route("/api/public/<public_token>/status")
def machine_status(public_token):
    cleanup_expired_sessions()
    machine = Machine.query.filter_by(public_token=public_token).first_or_404()
    return jsonify({"ok": True, "machine": serialize_machine(machine)})


@app.route("/api/public/<public_token>/reserve", methods=["POST"])
def reserve_machine(public_token):
    cleanup_expired_sessions()
    machine = Machine.query.filter_by(public_token=public_token).first_or_404()

    if machine.current_session_id or computed_machine_status(machine) != MACHINE_AVAILABLE:
        return jsonify({"ok": False, "error": "machine_not_available", "machine": serialize_machine(machine)}), 409

    session = WashSession(
        session_id=gen_id("ws"),
        machine_id=machine.id,
        status=SESSION_AWAITING_PAYMENT,
        expected_amount=machine.price_usd,
        expires_at=now_utc() + timedelta(minutes=SESSION_EXPIRE_MINUTES),
    )
    db.session.add(session)
    machine.current_session_id = session.session_id
    machine.status = MACHINE_RESERVED_UNPAID
    db.session.commit()

    return redirect(url_for("session_page", session_id=session.session_id))


@app.route("/session/<session_id>")
def session_page(session_id):
    cleanup_expired_sessions()
    session = WashSession.query.filter_by(session_id=session_id).first_or_404()
    machine = db.session.get(Machine, session.machine_id)
    status_messages = {
        SESSION_AWAITING_PAYMENT: "Waiting for payment",
        SESSION_PAYMENT_CONFIRMED: "Payment received. Starting machine...",
        SESSION_RUNNING: "Machine is running",
        SESSION_COMPLETED: "Wash complete",
        SESSION_EXPIRED: "Reservation expired",
        SESSION_CANCELLED: "Reservation cancelled",
    }
    return render_template_string(
        SESSION_TEMPLATE,
        session=session,
        amount=decimal_to_str(session.expected_amount),
        expires_at=normalize_utc(session.expires_at).strftime("%Y-%m-%d %H:%M:%S UTC"),
        telegram_link=build_telegram_link(session.session_id),
        pay_link=build_pay_link(machine, session),
        initial_message=status_messages.get(session.status, session.status),
        initial_payload=serialize_session(session),
    )


@app.route("/api/session/<session_id>")
def session_status(session_id):
    cleanup_expired_sessions()
    session = WashSession.query.filter_by(session_id=session_id).first_or_404()
    return jsonify({"ok": True, "session": serialize_session(session)})


@app.route("/internal/telegram/link", methods=["POST"])
def telegram_link():
    auth = require_internal_secret()
    if auth:
        return auth

    data = request.get_json(silent=True) or {}
    session = WashSession.query.filter_by(session_id=(data.get("session_id") or "").strip()).first()
    if not session:
        return jsonify({"ok": False, "error": "session_not_found"}), 404

    session.telegram_user_id = str(data.get("telegram_user_id") or "").strip() or None
    session.telegram_username = str(data.get("telegram_username") or "").strip().lstrip("@") or None
    db.session.commit()
    return jsonify({"ok": True, "session": serialize_session(session)})


@app.route("/internal/payment-events", methods=["POST"])
def payment_events():
    auth = require_internal_secret()
    if auth:
        return auth

    cleanup_expired_sessions()
    data = request.get_json(silent=True) or {}

    machine_code = (data.get("machine_code") or "").strip()
    trx_id = (data.get("trx_id") or "").strip()
    amount = parse_decimal(data.get("amount"))
    source_chat_id = int(data.get("source_chat_id") or 0)
    source_sender_id = int(data.get("source_sender_id") or 0)

    if source_chat_id != ABA_GROUP_ID or source_sender_id != ABA_BOT_ID:
        return jsonify({"ok": False, "error": "untrusted_payment_source"}), 403
    if not machine_code or not trx_id or amount is None:
        return jsonify({"ok": False, "error": "machine_code_trx_id_and_amount_required"}), 400

    existing_payment = PaymentEvent.query.filter_by(trx_id=trx_id).first()
    if existing_payment:
        existing_command = Command.query.filter_by(session_id=existing_payment.session_id).order_by(Command.id.desc()).first()
        return jsonify(
            {
                "ok": True,
                "duplicate": True,
                "session_id": existing_payment.session_id,
                "command_id": existing_command.command_id if existing_command else None,
            }
        )

    machine = Machine.query.filter_by(machine_code=machine_code).first()
    if not machine:
        return jsonify({"ok": False, "error": "machine_not_found"}), 404
    if not machine.current_session_id:
        return jsonify({"ok": False, "error": "no_active_session"}), 409

    session = WashSession.query.filter_by(session_id=machine.current_session_id, machine_id=machine.id).first()
    if not session:
        return jsonify({"ok": False, "error": "active_session_not_found"}), 404
    if session.status != SESSION_AWAITING_PAYMENT:
        return jsonify({"ok": False, "error": "active_session_not_awaiting_payment", "session_status": session.status}), 409
    if seconds_until(session.expires_at) <= 0:
        session.status = SESSION_EXPIRED
        machine.current_session_id = None
        machine.status = MACHINE_AVAILABLE
        db.session.commit()
        return jsonify({"ok": False, "error": "active_session_expired"}), 409
    if session.expected_amount != amount:
        return jsonify(
            {
                "ok": False,
                "error": "amount_mismatch",
                "expected_amount": decimal_to_str(session.expected_amount),
                "paid_amount": decimal_to_str(amount),
            }
        ), 409

    command = Command(
        command_id=gen_id("cmd"),
        machine_id=machine.id,
        session_id=session.session_id,
        payload_json=json.dumps(
            {
                "type": START_SERVICE,
                "machine_code": machine.machine_code,
                "session_id": session.session_id,
                "duration_minutes": machine.wash_duration_minutes,
                "steps": 1,
            }
        ),
        command_type=START_SERVICE,
        status=COMMAND_PENDING,
        duration_minutes=machine.wash_duration_minutes,
        steps=1,
    )
    payment = PaymentEvent(
        trx_id=trx_id,
        machine_code=machine.machine_code,
        session_id=session.session_id,
        amount=amount,
        raw_text=data.get("raw_text"),
        parsed_json=json.dumps(data.get("parsed") or {}),
        source_chat_id=str(source_chat_id),
        source_sender_id=str(source_sender_id),
    )

    session.status = SESSION_PAYMENT_CONFIRMED
    machine.status = MACHINE_STARTING
    db.session.add(command)
    db.session.add(payment)
    db.session.commit()

    return jsonify({"ok": True, "session_id": session.session_id, "command_id": command.command_id})


@app.route("/esp32/<machine_code>/heartbeat", methods=["POST"])
def esp32_heartbeat(machine_code):
    machine = machine_auth(machine_code)
    if not machine:
        return jsonify({"ok": False, "error": "invalid_machine_token"}), 401

    machine.heartbeat_at = now_utc()
    db.session.commit()
    return jsonify({"ok": True, "machine_status": computed_machine_status(machine)})


@app.route("/esp32/<machine_code>/next-command")
def esp32_next_command(machine_code):
    machine = machine_auth(machine_code)
    if not machine:
        return jsonify({"ok": False, "error": "invalid_machine_token"}), 401

    command = Command.query.filter_by(machine_id=machine.id, status=COMMAND_PENDING).order_by(Command.id.asc()).first()
    if not command:
        return jsonify({"ok": True, "has_command": False})

    return jsonify(
        {
            "ok": True,
            "has_command": True,
            "command": {
                "db_command_id": command.command_id,
                "type": command.command_type,
                "machine_code": machine.machine_code,
                "session_id": command.session_id,
                "duration_minutes": command.duration_minutes,
                "steps": command.steps,
            },
        }
    )


@app.route("/esp32/<machine_code>/ack", methods=["POST"])
def esp32_ack(machine_code):
    machine = machine_auth(machine_code)
    if not machine:
        return jsonify({"ok": False, "error": "invalid_machine_token"}), 401

    data = request.get_json(silent=True) or {}
    command = Command.query.filter_by(command_id=(data.get("db_command_id") or "").strip(), machine_id=machine.id).first()
    if not command:
        return jsonify({"ok": False, "error": "command_not_found"}), 404

    if command.status == COMMAND_PENDING:
        command.status = COMMAND_ACKED
        command.acked_at = now_utc()

    session = None
    if command.session_id:
        session = WashSession.query.filter_by(session_id=command.session_id, machine_id=machine.id).first()

    if command.command_type == START_SERVICE:
        if not session:
            return jsonify({"ok": False, "error": "session_not_found"}), 404
        if session.status == SESSION_PAYMENT_CONFIRMED:
            session.status = SESSION_RUNNING
            session.started_at = now_utc()
        machine.status = MACHINE_RUNNING
        machine.current_session_id = session.session_id

    machine.heartbeat_at = now_utc()
    db.session.commit()

    return jsonify(
        {
            "ok": True,
            "command_status": command.status,
            "session_status": session.status if session else None,
            "machine_status": machine.status,
        }
    )


@app.route("/esp32/<machine_code>/finished", methods=["POST"])
def esp32_finished(machine_code):
    machine = machine_auth(machine_code)
    if not machine:
        return jsonify({"ok": False, "error": "invalid_machine_token"}), 401

    data = request.get_json(silent=True) or {}
    session_id = (data.get("session_id") or "").strip()
    command_id = (data.get("db_command_id") or "").strip()
    session = WashSession.query.filter_by(session_id=session_id, machine_id=machine.id).first()
    if not session:
        return jsonify({"ok": False, "error": "session_not_found"}), 404

    command = None
    if command_id:
        command = Command.query.filter_by(command_id=command_id, machine_id=machine.id).first()
    if command is None:
        command = Command.query.filter(
            Command.machine_id == machine.id,
            Command.session_id == session_id,
            Command.status.in_([COMMAND_PENDING, COMMAND_ACKED]),
        ).order_by(Command.id.desc()).first()

    session.status = SESSION_COMPLETED
    session.completed_at = now_utc()
    if command:
        command.status = COMMAND_COMPLETED
        command.completed_at = now_utc()
    machine.status = MACHINE_AVAILABLE
    machine.current_session_id = None
    machine.heartbeat_at = now_utc()
    db.session.commit()

    send_finish_notification(session, machine)
    return jsonify({"ok": True, "session_status": session.status, "machine_status": machine.status})


@app.route("/esp32/<machine_code>/command-done", methods=["POST"])
def esp32_command_done(machine_code):
    machine = machine_auth(machine_code)
    if not machine:
        return jsonify({"ok": False, "error": "invalid_machine_token"}), 401

    data = request.get_json(silent=True) or {}
    command = Command.query.filter_by(command_id=(data.get("db_command_id") or "").strip(), machine_id=machine.id).first()
    if not command:
        return jsonify({"ok": False, "error": "command_not_found"}), 404

    command.status = COMMAND_COMPLETED
    command.completed_at = now_utc()
    machine.heartbeat_at = now_utc()
    db.session.commit()
    return jsonify({"ok": True, "command_status": command.status})


with app.app_context():
    db.create_all()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
