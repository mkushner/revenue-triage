import engine
from engine import DEAD, LOST, NEEDS_YOU, RECOVERED, WAIT, WATCH

CONFIG = {
    "channels": {"billing": "C_BILLING", "big": "C_BIG"},
    "routes": [
        {"states": [NEEDS_YOU, DEAD], "channels": ["billing"]},
        {"states": [WATCH, NEEDS_YOU, DEAD], "min_amount": 500, "channels": ["big"]},
    ],
    "watch_from_attempt": 4,
    "decline_codes": {"dead": ["lost_card", "stolen_card"], "hard": ["expired_card", "do_not_honor"]},
    "risk_levels": [{"min": 7, "label": "HIGH"}, {"min": 4, "label": "MEDIUM"}, {"min": 1, "label": "LOW"}],
    "amount_levels": [{"min": 500, "emoji": ":triangular_flag_on_post:"}, {"min": 100, "emoji": ":warning:"}],
}


def invoice(**overrides):
    return {
        "id": "in_1",
        "customer": "cus_1",
        "customer_name": "Acme",
        "customer_email": "billing@acme.test",
        "amount_due": 21600,
        "currency": "usd",
        "attempt_count": 1,
        "next_payment_attempt": 1_790_000_000,
        "status": "open",
        "collection_method": "charge_automatically",
        "livemode": True,
        "hosted_invoice_url": "https://invoice.stripe.com/i/1",
        **overrides,
    }


def charge(reason, status="failed"):
    return {"status": status, "failure_code": "card_declined", "outcome": {"reason": reason}}


def test_triageable_skips_manual_and_zero_invoices():
    assert engine.triageable(invoice())
    assert not engine.triageable(invoice(collection_method="send_invoice"))
    assert not engine.triageable(invoice(amount_due=0))


def test_decline_code_prefers_outcome_reason():
    assert engine.decline_code(charge("insufficient_funds")) == "insufficient_funds"
    assert engine.decline_code({"failure_code": "expired_card", "outcome": None}) == "expired_card"


def test_classify_code_beats_counter():
    assert engine.classify("stolen_card", 1, 123, CONFIG) == DEAD
    assert engine.classify("expired_card", 1, 123, CONFIG) == NEEDS_YOU
    assert engine.classify("insufficient_funds", 3, 123, CONFIG) == WAIT
    assert engine.classify("insufficient_funds", 4, 123, CONFIG) == WATCH
    assert engine.classify("insufficient_funds", 2, None, CONFIG) == NEEDS_YOU  # Stripe gave up
    assert engine.classify(None, 1, 123, CONFIG) == WAIT


def test_triage_first_failure_is_a_transition():
    assert engine.triage(invoice(), [charge("insufficient_funds")], CONFIG) == (WAIT, "insufficient_funds", True)


def test_triage_soft_crossing_watch_threshold_pings_once():
    soft = [charge("insufficient_funds"), charge("insufficient_funds")]
    assert engine.triage(invoice(attempt_count=4), soft, CONFIG)[::2] == (WATCH, True)
    assert engine.triage(invoice(attempt_count=5), soft, CONFIG)[::2] == (WATCH, False)


def test_triage_hard_after_soft_enters_needs_you_but_repeat_does_not():
    assert engine.triage(invoice(attempt_count=2), [charge("do_not_honor"), charge("insufficient_funds")], CONFIG)[::2] == (NEEDS_YOU, True)
    assert engine.triage(invoice(attempt_count=3), [charge("do_not_honor"), charge("do_not_honor")], CONFIG)[::2] == (NEEDS_YOU, False)


def test_triage_stripe_giving_up_is_a_transition():
    charges = [charge("insufficient_funds"), charge("insufficient_funds")]
    assert engine.triage(invoice(attempt_count=8, next_payment_attempt=None), charges, CONFIG)[::2] == (NEEDS_YOU, True)


def test_current_state_for_closed_invoices():
    assert engine.current_state(invoice(status="paid"), None, CONFIG) == RECOVERED
    assert engine.current_state(invoice(status="uncollectible"), None, CONFIG) == LOST
    assert engine.current_state(invoice(attempt_count=5), "insufficient_funds", CONFIG) == WATCH


def test_channels_for_respects_min_amount():
    assert engine.channels_for(NEEDS_YOU, invoice(amount_due=1800), CONFIG) == ["billing"]
    assert engine.channels_for(NEEDS_YOU, invoice(amount_due=60000), CONFIG) == ["billing", "big"]
    assert engine.channels_for(WATCH, invoice(amount_due=1800), CONFIG) == []
    assert engine.channels_for(WAIT, invoice(amount_due=60000), CONFIG) == []


def test_level_and_zero_decimal():
    assert engine.level(CONFIG["risk_levels"], 5)["label"] == "MEDIUM"
    assert engine.level(CONFIG["amount_levels"], 50) is None
    assert engine.major_units(5000, "jpy") == 5000
    assert engine.major_units(5000, "usd") == 50


def test_render_ping():
    text = engine.render_ping(invoice(amount_due=60000, attempt_count=5), "do_not_honor", NEEDS_YOU, CONFIG)
    assert text.splitlines()[0] == ":red_circle: needs you: *automation won't save this, write to the customer*"
    assert ":triangular_flag_on_post: 600.00 USD · churn risk *HIGH* · attempt 5 · decline `do_not_honor` · next retry" in text
    assert "<https://dashboard.stripe.com/customers/cus_1|Acme (billing@acme.test)>" in text
    assert "<https://dashboard.stripe.com/invoices/in_1|in_1> · <https://invoice.stripe.com/i/1|pay link>" in text


def test_risk_follows_attempts_only_for_soft_states():
    assert "churn risk *MEDIUM*" in engine.render_ping(invoice(attempt_count=5), "insufficient_funds", WATCH, CONFIG)
    assert "churn risk *HIGH*" in engine.render_ping(invoice(attempt_count=1), "do_not_honor", NEEDS_YOU, CONFIG)


def test_uncounted_attempt_is_not_shown_as_attempt_0():
    text = engine.render_ping(invoice(attempt_count=0, next_payment_attempt=None), "generic_decline", NEEDS_YOU, CONFIG)
    assert "attempt 0" not in text
    assert "decline `generic_decline`" in text


def test_awaiting_customer_authentication():
    awaiting = invoice(attempt_count=0, next_payment_attempt=None)
    assert engine.triage(awaiting, [], CONFIG, "requires_action") == (NEEDS_YOU, engine.AWAITING_AUTH, True)
    # Already needs_you from an earlier hard decline on record: no new ping.
    assert engine.triage(awaiting, [charge("do_not_honor")], CONFIG, "requires_action")[2] is False
    text = engine.render_ping(awaiting, engine.AWAITING_AUTH, NEEDS_YOU, CONFIG)
    assert engine.AWAITING_AUTH_TEXT in text
    assert "attempt" not in text and "Stripe stopped retrying" not in text


def test_render_recovered_says_when_and_drops_stale_facts():
    old = engine.render_ping(invoice(attempt_count=3), "do_not_honor", NEEDS_YOU, CONFIG)
    paid = invoice(status="paid", status_transitions={"paid_at": 1_790_000_000})
    new = engine.render_recovered(old, paid, 3).splitlines()
    assert new[0] == ":white_check_mark: recovered: *paid <!date^1790000000^{date_short}|recently> after 3 failed attempts*"
    assert new[1] == f"~{old.splitlines()[0]}~"
    assert new[2] == ":warning: 216.00 USD · churn risk *HIGH* · attempt 3 · decline `do_not_honor`"
    assert new[3] == "<https://dashboard.stripe.com/customers/cus_1|Acme (billing@acme.test)> · <https://dashboard.stripe.com/invoices/in_1|in_1>"


def test_render_recovered_after_3ds_has_no_failure_count():
    old = engine.render_ping(invoice(attempt_count=0, next_payment_attempt=None), engine.AWAITING_AUTH, NEEDS_YOU, CONFIG)
    new = engine.render_recovered(old, invoice(status="paid"), 0).splitlines()
    assert new[0] == ":white_check_mark: recovered: *paid*"
    assert engine.AWAITING_AUTH_TEXT not in new[2]


def test_render_ping_escapes_customer_name_and_marks_test_mode():
    text = engine.render_ping(invoice(customer_name="A<b>", customer_email=None, livemode=False, next_payment_attempt=None), None, NEEDS_YOU, CONFIG)
    assert "<https://dashboard.stripe.com/test/customers/cus_1|A&lt;b&gt;>" in text
    assert "Stripe stopped retrying" in text


def test_render_digest():
    items = [
        {"invoice": invoice(id="in_1", status="paid"), "code": None, "state": RECOVERED},
        {"invoice": invoice(id="in_2", amount_due=1800), "code": "insufficient_funds", "state": WAIT},
        {"invoice": invoice(id="in_3", amount_due=60000), "code": "do_not_honor", "state": NEEDS_YOU},
        {"invoice": invoice(id="in_4", amount_due=1200, attempt_count=0), "code": engine.AWAITING_AUTH, "state": NEEDS_YOU},
    ]
    text = engine.render_digest(items, 7)
    assert "4 invoices failed" in text
    assert ":white_check_mark: self-recovered: 1 · 216.00 USD" in text
    assert "*Still open, at risk:* 630.00 USD" in text
    assert "600.00 USD · attempt 1 · `do_not_honor`" in text
    assert "12.00 USD · awaiting customer 3DS/OTP" in text
