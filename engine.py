"""Triage engine: (decline code x attempt count) -> state, plus amount/risk levels and Slack text.

Pure functions over plain dicts, shared by the webhook triage and /digest.
"""

from collections import defaultdict

WAIT, WATCH, NEEDS_YOU, DEAD = "wait", "watch", "needs_you", "dead"
RECOVERED, LOST = "recovered", "lost"  # closed invoices, digest only
RANK = {WAIT: 0, WATCH: 1, NEEDS_YOU: 2, DEAD: 3}
LABELS = {
    RECOVERED: ":white_check_mark: self-recovered",
    WAIT: ":large_green_circle: wait",
    WATCH: ":large_yellow_circle: watch",
    NEEDS_YOU: ":red_circle: needs you",
    DEAD: ":black_circle: dead",
    LOST: ":x: lost",
}
MEANING = {
    WAIT: "auto-retry will likely collect",
    WATCH: "soft decline keeps failing",
    NEEDS_YOU: "automation won't save this, write to the customer",
    DEAD: "card is gone, stop burning retries",
}

# https://docs.stripe.com/currencies#zero-decimal
ZERO_DECIMAL = {"bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga", "pyg", "rwf", "ugx", "vnd", "vuv", "xaf", "xof", "xpf"}


def triageable(invoice):
    """Only automatic collection has a retry schedule; a $0 invoice (trial) can't really fail."""
    return invoice.get("collection_method") == "charge_automatically" and invoice["amount_due"] > 0


def decline_code(charge):
    """The bank's decline code: `outcome.reason`; `failure_code` is mostly a generic card_declined."""
    return (charge.get("outcome") or {}).get("reason") or charge.get("failure_code")


# A PaymentIntent waiting on 3DS/OTP has no failed charge; Stripe neither counts nor retries it.
AWAITING_AUTH = "authentication_required"
AWAITING_AUTH_TEXT = "waiting for the customer to confirm the payment (3DS/OTP)"


def attempt_code(pi_status, charges):
    """Decline code of the latest attempt, given the PaymentIntent status and its charges newest first."""
    if pi_status == "requires_action":
        return AWAITING_AUTH
    return decline_code(charges[0]) if charges else None


def classify(code, attempt_count, next_payment_attempt, config):
    """The decline code beats the counter; unlisted codes are soft. After a hard decline Stripe keeps
    scheduling retries but runs them only on a new card, so a hard code alone means needs_you."""
    codes = config["decline_codes"]
    if code in codes["dead"]:
        return DEAD
    if code in codes["hard"] or next_payment_attempt is None:
        return NEEDS_YOU
    if attempt_count >= config["watch_from_attempt"]:
        return WATCH
    return WAIT


def triage(invoice, charges, config, pi_status=None):
    """(state, decline code, entered) for a failed invoice, given its charges newest first.
    entered = worse than the previous attempt left it, rebuilt from the previous charge, so nothing is stored."""
    attempts = invoice["attempt_count"]
    code = attempt_code(pi_status, charges)
    state = classify(code, attempts, invoice["next_payment_attempt"], config)
    # A 3DS/OTP attempt made no charge, so every charge on record is an earlier attempt.
    earlier = charges if code == AWAITING_AUTH else charges[1:]
    previous = next((c for c in earlier if c["status"] == "failed"), None)
    if previous is None:
        return state, code, True
    # A later attempt exists, so the previous one still had a retry scheduled.
    previous_state = classify(decline_code(previous), attempts - 1, 0, config)
    return state, code, RANK[state] > RANK[previous_state]


def current_state(invoice, code, config):
    """Where an invoice stands now, for the digest."""
    if invoice["status"] == "paid":
        return RECOVERED
    if invoice["status"] in ("void", "uncollectible"):
        return LOST
    return classify(code, invoice["attempt_count"], invoice["next_payment_attempt"], config)


def level(levels, value):
    """First level from the top whose `min` the value reaches."""
    return next((lvl for lvl in levels if value >= lvl["min"]), None)


def major_units(amount, currency):
    return amount if currency.lower() in ZERO_DECIMAL else amount / 100


def channels_for(state, invoice, config):
    amount = major_units(invoice["amount_due"], invoice["currency"])
    channels = []
    for route in config["routes"]:
        if state in route["states"] and amount >= route.get("min_amount", 0):
            channels += [c for c in route["channels"] if c not in channels]
    return channels


def render_ping(invoice, code, state, config):
    amount = major_units(invoice["amount_due"], invoice["currency"])
    flag = level(config.get("amount_levels", []), amount)
    if state in (NEEDS_YOU, DEAD) and config.get("risk_levels"):
        risk = config["risk_levels"][0]  # automation won't recover these, whatever the attempt
    else:
        risk = level(config.get("risk_levels", []), invoice["attempt_count"])

    facts = [_amount(invoice)]
    if risk:
        facts.append(f"churn risk *{risk['label']}*")
    if code == AWAITING_AUTH:
        facts.append(AWAITING_AUTH_TEXT)
    else:
        if invoice["attempt_count"]:  # 0 = Stripe didn't count it (a 3DS/OTP attempt)
            facts.append(f"attempt {invoice['attempt_count']}")
        if code:
            facts.append(f"decline `{code}`")
        if invoice["next_payment_attempt"]:
            facts.append(f"next retry <!date^{invoice['next_payment_attempt']}^{{date_short}}|scheduled>")
        else:
            facts.append("Stripe stopped retrying")

    # mark_recovered finds the ping by this invoice id.
    links = f"{_customer_link(invoice)} · <{_dashboard(invoice)}invoices/{invoice['id']}|{invoice['id']}>"
    if invoice.get("hosted_invoice_url"):
        links += f" · <{invoice['hosted_invoice_url']}|pay link>"
    return "\n".join([
        f"{LABELS[state]}: *{MEANING[state]}*",
        (f"{flag['emoji']} " if flag else "") + " · ".join(facts),
        links,
    ])


RECOVERED_PREFIX = ":white_check_mark: recovered"
STALE_FACTS = ("next retry", "Stripe stopped retrying", AWAITING_AUTH_TEXT)


def render_recovered(old_text, invoice, failed_attempts):
    """The ping once its invoice is paid: a recovered headline, the old one struck through, stale facts dropped."""
    headline, *rest = old_text.split("\n")
    paid_at = (invoice.get("status_transitions") or {}).get("paid_at")
    paid = f"paid <!date^{paid_at}^{{date_short}}|recently>" if paid_at else "paid"
    if failed_attempts:
        paid += f" after {failed_attempts} failed attempt{'' if failed_attempts == 1 else 's'}"
    kept = [
        " · ".join(s for s in line.split(" · ") if not s.startswith(STALE_FACTS) and not s.endswith("|pay link>"))
        for line in rest
    ]
    return "\n".join([f"{RECOVERED_PREFIX}: *{paid}*", f"~{headline}~", *kept])


def render_digest(items, days):
    """items: [{"invoice", "code", "state"}] for every invoice that failed in the window."""
    by_state = defaultdict(list)
    for item in items:
        by_state[item["state"]].append(item["invoice"])

    lines = [f"*Dunning digest, last {days} days* · {len(items)} invoices failed"]
    for state in (RECOVERED, WAIT, WATCH, NEEDS_YOU, DEAD, LOST):
        if by_state[state]:
            lines.append(f"{LABELS[state]}: {len(by_state[state])} · {_money(by_state[state])}")
    open_invoices = [inv for state in (WAIT, WATCH, NEEDS_YOU, DEAD) for inv in by_state[state]]
    if open_invoices:
        lines.append(f"*Still open, at risk:* {_money(open_invoices)}")

    urgent = sorted((i for i in items if i["state"] in (NEEDS_YOU, DEAD)), key=lambda i: -i["invoice"]["amount_due"])
    if urgent:
        lines.append("\n*Needs you:*")
        for item in urgent:
            inv = item["invoice"]
            parts = [f"• {_customer_link(inv)}", _amount(inv)]
            if item["code"] == AWAITING_AUTH:
                parts.append("awaiting customer 3DS/OTP")
            else:
                if inv["attempt_count"]:
                    parts.append(f"attempt {inv['attempt_count']}")
                if item["code"]:
                    parts.append(f"`{item['code']}`")
            lines.append(" · ".join(parts))
    return "\n".join(lines)


def _amount(invoice):
    return f"{major_units(invoice['amount_due'], invoice['currency']):,.2f} {invoice['currency'].upper()}"


def _money(invoices):
    totals = defaultdict(float)
    for inv in invoices:
        totals[inv["currency"]] += major_units(inv["amount_due"], inv["currency"])
    return " + ".join(f"{total:,.2f} {currency.upper()}" for currency, total in totals.items())


def _dashboard(invoice):
    return "https://dashboard.stripe.com/" + ("" if invoice["livemode"] else "test/")


def _customer_link(invoice):
    name, email = invoice.get("customer_name"), invoice.get("customer_email")
    label = f"{name} ({email})" if name and email else name or email or invoice["customer"]
    label = label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"<{_dashboard(invoice)}customers/{invoice['customer']}|{label}>"
