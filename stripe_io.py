"""Read-only Stripe calls. Stripe is the source of truth; nothing is stored here."""

import stripe


class StripeReader:
    def __init__(self, api_key):
        self.client = stripe.StripeClient(api_key)

    def payment_attempts(self, invoice_id):
        """(PaymentIntent status, its charges newest first). Retries reuse the PaymentIntent."""
        payments = self.client.v1.invoice_payments.list(
            {"invoice": invoice_id, "limit": 1, "expand": ["data.payment.payment_intent"]}
        )
        if not payments.data or not payments.data[0].payment.payment_intent:
            return None, []
        payment_intent = payments.data[0].payment.payment_intent
        charges = self.client.v1.charges.list({"payment_intent": payment_intent.id, "limit": 20})
        return payment_intent.status, sorted((c.to_dict() for c in charges.data), key=lambda c: c["created"], reverse=True)

    def invoice(self, invoice_id):
        return self.client.v1.invoices.retrieve(invoice_id).to_dict()

    def failed_invoice_ids(self, since):
        """Invoices with a failed payment since `since` (unix ts). The Events API keeps 30 days."""
        events = self.client.v1.events.list({"type": "invoice.payment_failed", "created": {"gte": since}, "limit": 100})
        return list(dict.fromkeys(e.to_dict()["data"]["object"]["id"] for e in events.auto_paging_iter()))
