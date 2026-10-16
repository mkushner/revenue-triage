"""revenue-triage: one stateless server, two routes.

POST /stripe-webhook  invoice.payment_failed -> triage -> Slack ping on a transition
                      invoice.paid           -> rewrite that invoice's pings as recovered
POST /slack           every Slack slash command of the app (one Request URL), dispatched by `command`
"""

import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import urllib.request
from collections import OrderedDict
from urllib.parse import parse_qs, urlencode

import stripe
import yaml
from flask import Flask, abort, request

import engine
from stripe_io import StripeReader

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("revenue-triage")


def load_config():
    """YAML from $TRIAGE_CONFIG (inline) or a file, with ${VAR} expanded from the environment."""
    text = os.environ.get("TRIAGE_CONFIG")
    if not text:
        with open(os.environ.get("CONFIG_PATH", "config.yml")) as f:
            text = f.read()
    text = re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), text)
    config = yaml.safe_load(text)
    for route in config["routes"]:
        for channel in route["channels"]:
            if not config["channels"].get(channel):
                raise ValueError(f"route {route['states']} posts to '{channel}', which has no channel id")
    return config


config = load_config()
reader = StripeReader(os.environ["STRIPE_API_KEY"])
stripe_webhook_secret = os.environ["STRIPE_WEBHOOK_SECRET"]
slack_bot_token = os.environ["SLACK_BOT_TOKEN"]
slack_signing_secret = os.environ["SLACK_SIGNING_SECRET"]

# (event id, channel) pairs already posted: Stripe redelivers an event until it gets a 2xx.
delivered = OrderedDict()

app = Flask(__name__)


@app.get("/")
def health():
    return "ok"


@app.post("/stripe-webhook")
def stripe_webhook():
    payload = request.get_data()
    try:
        stripe.WebhookSignature.verify_header(
            payload, request.headers.get("Stripe-Signature"), stripe_webhook_secret, stripe.Webhook.DEFAULT_TOLERANCE
        )
    except stripe.SignatureVerificationError:
        abort(400)

    event = json.loads(payload)
    if event["type"] not in ("invoice.payment_failed", "invoice.paid"):
        return "ignored"
    invoice = event["data"]["object"]
    if not engine.triageable(invoice):
        return "ignored"
    if event["type"] == "invoice.paid":
        return mark_recovered(invoice)
    return triage_failure(event, invoice)


def triage_failure(event, invoice):
    pi_status, charges = reader.payment_attempts(invoice["id"])
    state, code, entered = engine.triage(invoice, charges, config, pi_status)
    if not entered:
        return state
    channels = [c for c in engine.channels_for(state, invoice, config) if (event["id"], c) not in delivered]
    if not channels:
        return state

    text = engine.render_ping(invoice, code, state, config)
    failed = False
    for channel in channels:
        if slack_api("chat.postMessage", channel=config["channels"][channel], text=text, unfurl_links="false"):
            delivered[(event["id"], channel)] = True
        else:
            failed = True
    while len(delivered) > 10_000:
        delivered.popitem(last=False)
    # A 500 makes Stripe redeliver; channels that got the ping are skipped then.
    return ("slack delivery failed", 500) if failed else state


def mark_recovered(invoice):
    """Rewrites a paid invoice's pings, found by invoice id in Slack: Stripe can't tell what was pinged
    (an invoice paid after 3DS/OTP has no failed charge, yet was pinged)."""
    failed, pending = False, []
    for channel in dict.fromkeys(config["channels"].values()):
        pings = find_pings(channel, invoice)
        if pings is None:
            failed = True
            continue
        # Skip already-rewritten pings (Stripe redelivery).
        pending += [(channel, m) for m in pings if not m["text"].startswith(engine.RECOVERED_PREFIX)]
    if not pending:
        return ("slack history failed", 500) if failed else "nothing pinged"

    _, charges = reader.payment_attempts(invoice["id"])
    failed_attempts = sum(c["status"] == "failed" for c in charges)
    for channel, message in pending:
        text = engine.render_recovered(message["text"], invoice, failed_attempts)
        if not slack_api("chat.update", channel=channel, ts=message["ts"], text=text):
            failed = True
    return ("slack update failed", 500) if failed else "recovered"


def find_pings(channel, invoice):
    """Messages in `channel` since the invoice was created that mention its id, or None if Slack failed."""
    found, cursor = [], ""
    while True:
        body = slack_api("conversations.history", channel=channel, oldest=str(invoice["created"]), limit=200, cursor=cursor)
        if body is None:
            return None
        found += [m for m in body["messages"] if invoice["id"] in m.get("text", "")]
        cursor = (body.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return found


def digest(form):
    days = 7
    items = []
    for invoice_id in reader.failed_invoice_ids(int(time.time()) - days * 86400):
        invoice = reader.invoice(invoice_id)
        if not engine.triageable(invoice):
            continue
        pi_status, charges = reader.payment_attempts(invoice_id) if invoice["status"] == "open" else (None, [])
        code = engine.attempt_code(pi_status, charges)
        items.append({"invoice": invoice, "code": code, "state": engine.current_state(invoice, code, config)})
    return engine.render_digest(items, days)


# Every slash command hits POST /slack.
COMMANDS = {"/digest": digest}


@app.post("/slack")
def slack_command():
    body = request.get_data()
    if not slack_signature_ok(body, request.headers):
        abort(401)
    form = {key: values[0] for key, values in parse_qs(body.decode()).items()}
    handler = COMMANDS.get(form.get("command"))
    if handler is None:
        return f"Unknown command {form.get('command')}"
    # Slack wants an answer within 3s: ack now, post the result to response_url.
    run_in_background(respond, handler, form)
    return f"Working on {form['command']}…"


def respond(handler, form):
    try:
        text = handler(form)
    except Exception:
        log.exception("%s failed", form["command"])
        text = f"{form['command']} failed, see the server log."
    post_json(form["response_url"], {"text": text})


def run_in_background(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


def slack_signature_ok(body, headers):
    # https://api.slack.com/authentication/verifying-requests-from-slack
    timestamp = headers.get("X-Slack-Request-Timestamp", "")
    if not timestamp.isdigit() or abs(time.time() - int(timestamp)) > 300:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(slack_signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, headers.get("X-Slack-Signature", ""))


def slack_api(method, **params):
    """Calls a Slack Web API method with the bot token; returns the response body, or None on any failure."""
    req = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=urlencode(params).encode(),
        headers={"Authorization": f"Bearer {slack_bot_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.load(resp)
    except OSError as e:
        log.error("slack %s failed: %s", method, e)
        return None
    if not body.get("ok"):
        log.error("slack %s: %s", method, body.get("error"))
        return None
    return body


def post_json(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except OSError as e:
        log.error("POST to %s failed: %s", url, e)
        return False
