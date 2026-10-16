import hashlib
import hmac
import json
import os
import time

import pytest

from test_engine import CONFIG, charge, invoice

os.environ.update({
    "STRIPE_API_KEY": "rk_test_x",
    "STRIPE_WEBHOOK_SECRET": "whsec_test",
    "SLACK_BOT_TOKEN": "xoxb-test",
    "SLACK_SIGNING_SECRET": "slack_secret",
    "TRIAGE_CONFIG": json.dumps(CONFIG),
})

import app  # noqa: E402
import engine  # noqa: E402
from engine import NEEDS_YOU  # noqa: E402

PAID_AT = 1_790_000_000


class FakeSlack:
    """Stands in for app.slack_api: records calls, serves `history` for conversations.history."""

    def __init__(self):
        self.calls, self.history, self.down = [], {}, set()

    def __call__(self, method, **params):
        self.calls.append((method, params))
        if params.get("channel") in self.down:
            return None
        if method == "conversations.history":
            return {"ok": True, "messages": self.history.get(params["channel"], []), "response_metadata": {"next_cursor": ""}}
        return {"ok": True, "ts": "1.0"}

    def sent(self, method):
        return [p for m, p in self.calls if m == method]


@pytest.fixture
def client():
    app.delivered.clear()
    return app.app.test_client()


@pytest.fixture
def slack(monkeypatch):
    fake = FakeSlack()
    monkeypatch.setattr(app, "slack_api", fake)
    return fake


@pytest.fixture
def attempts(monkeypatch):
    """Sets what reader.payment_attempts returns and records which invoices it was asked about."""
    state = {"result": (None, []), "asked": []}

    def payment_attempts(invoice_id):
        state["asked"].append(invoice_id)
        return state["result"]

    monkeypatch.setattr(app.reader, "payment_attempts", payment_attempts)
    return state


@pytest.fixture
def responses(monkeypatch):
    sent = []
    monkeypatch.setattr(app, "post_json", lambda url, payload: sent.append((url, payload["text"])) or True)
    return sent


def event(event_type="invoice.payment_failed", event_id="evt_1", **invoice_overrides):
    return {"id": event_id, "type": event_type, "data": {"object": invoice(created=1_780_000_000, **invoice_overrides)}}


def paid_event():
    return event("invoice.paid", status="paid", status_transitions={"paid_at": PAID_AT})


def post_stripe(client, evt):
    payload = json.dumps(evt)
    ts = int(time.time())
    sig = hmac.new(b"whsec_test", f"{ts}.{payload}".encode(), hashlib.sha256).hexdigest()
    return client.post("/stripe-webhook", data=payload, headers={"Stripe-Signature": f"t={ts},v1={sig}"})


def post_slack(client, body, secret="slack_secret"):
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
    return client.post(
        "/slack",
        data=body,
        headers={"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig,
                 "Content-Type": "application/x-www-form-urlencoded"},
    )


def test_rejects_bad_stripe_signature(client, slack):
    resp = client.post("/stripe-webhook", data="{}", headers={"Stripe-Signature": "t=1,v1=bad"})
    assert resp.status_code == 400
    assert slack.calls == []


def test_needs_you_pings_once_even_when_stripe_redelivers(client, slack, attempts):
    attempts["result"] = ("requires_payment_method", [charge("do_not_honor"), charge("insufficient_funds")])
    evt = event(attempt_count=2)
    assert post_stripe(client, evt).status_code == 200
    assert post_stripe(client, evt).status_code == 200
    posts = slack.sent("chat.postMessage")
    assert [p["channel"] for p in posts] == ["C_BILLING"]
    assert posts[0]["text"].startswith(":red_circle: needs you")
    assert "in_1" in posts[0]["text"]


def test_awaiting_3ds_pings_needs_you(client, slack, attempts):
    attempts["result"] = ("requires_action", [])
    assert post_stripe(client, event(attempt_count=0, next_payment_attempt=None)).get_data(as_text=True) == "needs_you"
    assert engine.AWAITING_AUTH_TEXT in slack.sent("chat.postMessage")[0]["text"]


def test_soft_failure_is_silent(client, slack, attempts):
    attempts["result"] = ("requires_payment_method", [charge("insufficient_funds")])
    assert post_stripe(client, event()).get_data(as_text=True) == "wait"
    assert slack.calls == []


def test_manual_invoices_and_other_events_are_ignored(client, slack):
    assert post_stripe(client, event(collection_method="send_invoice")).get_data(as_text=True) == "ignored"
    assert post_stripe(client, event("customer.updated")).get_data(as_text=True) == "ignored"


def test_slack_failure_returns_500_and_redelivery_only_hits_the_missing_channel(client, slack, attempts):
    attempts["result"] = ("requires_payment_method", [charge("do_not_honor")])
    slack.down = {"C_BIG"}
    evt = event(amount_due=60000)
    assert post_stripe(client, evt).status_code == 500
    slack.down = set()
    assert post_stripe(client, evt).status_code == 200
    assert [p["channel"] for p in slack.sent("chat.postMessage")] == ["C_BILLING", "C_BIG", "C_BIG"]


def test_paid_invoice_without_a_ping_is_left_alone(client, slack, attempts):
    assert post_stripe(client, paid_event()).get_data(as_text=True) == "nothing pinged"
    assert {p["channel"] for p in slack.sent("conversations.history")} == {"C_BILLING", "C_BIG"}
    assert slack.sent("chat.update") == []
    assert attempts["asked"] == []  # no Stripe call unless there is something to rewrite


def test_paid_invoice_rewrites_its_ping_as_recovered(client, slack, attempts):
    attempts["result"] = ("succeeded", [charge(None, status="succeeded"), charge("do_not_honor")])
    ping = engine.render_ping(invoice(), "do_not_honor", NEEDS_YOU, CONFIG)
    other = engine.render_ping(invoice(id="in_2"), "do_not_honor", NEEDS_YOU, CONFIG)
    slack.history = {"C_BILLING": [{"ts": "111.1", "text": ping}, {"ts": "222.2", "text": other}]}

    evt = paid_event()
    assert post_stripe(client, evt).get_data(as_text=True) == "recovered"
    assert slack.sent("conversations.history")[0]["oldest"] == "1780000000"
    updates = slack.sent("chat.update")
    assert [(u["channel"], u["ts"]) for u in updates] == [("C_BILLING", "111.1")]
    assert updates[0]["text"] == engine.render_recovered(ping, evt["data"]["object"], 1)
    assert updates[0]["text"].startswith(":white_check_mark: recovered: *paid <!date^1790000000^")


def test_paid_after_3ds_is_rewritten_even_without_a_failed_charge(client, slack, attempts):
    attempts["result"] = ("succeeded", [charge(None, status="succeeded")])
    ping = engine.render_ping(invoice(attempt_count=0, next_payment_attempt=None), engine.AWAITING_AUTH, NEEDS_YOU, CONFIG)
    slack.history = {"C_BILLING": [{"ts": "111.1", "text": ping}]}
    assert post_stripe(client, paid_event()).get_data(as_text=True) == "recovered"
    assert "failed attempt" not in slack.sent("chat.update")[0]["text"]


def test_already_recovered_ping_is_left_alone(client, slack, attempts):
    ping = engine.render_ping(invoice(), "do_not_honor", NEEDS_YOU, CONFIG)
    recovered = engine.render_recovered(ping, invoice(status="paid"), 1)
    slack.history = {"C_BILLING": [{"ts": "111.1", "text": recovered}]}
    assert post_stripe(client, paid_event()).status_code == 200
    assert slack.sent("chat.update") == []
    assert attempts["asked"] == []


def test_slack_rejects_bad_signature(client, responses):
    assert post_slack(client, "command=%2Fdigest", secret="wrong").status_code == 401


def test_unknown_command(client, responses):
    assert post_slack(client, "command=%2Fnope").get_data(as_text=True) == "Unknown command /nope"


def test_digest_acks_then_posts_to_response_url(client, responses, attempts, monkeypatch):
    monkeypatch.setattr(app, "run_in_background", lambda fn, *args: fn(*args))
    invoices = {
        "in_1": invoice(id="in_1", status="paid"),
        "in_2": invoice(id="in_2", amount_due=60000),
    }
    monkeypatch.setattr(app.reader, "failed_invoice_ids", lambda since: list(invoices))
    monkeypatch.setattr(app.reader, "invoice", invoices.__getitem__)
    attempts["result"] = ("requires_payment_method", [charge("do_not_honor")])

    resp = post_slack(client, "command=%2Fdigest&response_url=https%3A%2F%2Fhooks.slack.test%2Fr")
    assert resp.get_data(as_text=True) == "Working on /digest…"
    assert responses[0][0] == "https://hooks.slack.test/r"
    assert "self-recovered: 1" in responses[0][1]
    assert "needs you: 1" in responses[0][1]
    assert attempts["asked"] == ["in_2"]  # only open invoices need their attempts
