---
title: Payment Pending Troubleshooting Runbook
doc_id: RB-PAY-001
classification: internal
domain: payments
version: 3.2
---

# Payment Pending Troubleshooting Runbook

This runbook is the official procedure for incidents where customer payments remain in **PENDING**
status longer than expected. A payment normally moves from PENDING to COMPLETED within 60 seconds.

## Scope and severity

Use this runbook when one or more payments have been PENDING for more than 5 minutes. If more than
100 payments are affected, or any payment has been PENDING for more than 30 minutes, follow the
Payments Escalation Policy in parallel.

## Investigation order

Follow these steps in order. Record the result of each step in the incident ticket.

1. **Check the payment service provider (PSP) status.** Confirm the external PSP is reachable and
   not reporting an outage. A PSP outage is outside our control; escalate to the PSP and inform
   customers.
2. **Check the status of an affected payment** using the payment status API. Confirm it is still
   PENDING and note how many attempts have been recorded.
3. **Check the payment event pipeline.** Payments are completed asynchronously: the PSP callback is
   published as a payment event and the payment-events consumer applies it. Check the consumer
   state and its lag on the payment-events topic.
4. **Check the payments database** for lock contention or failed writes.

## Remediation

- If the payment-events consumer is stalled or its lag is growing, restart the consumer following
  the Kafka Consumer Operations Guide. Restarting the consumer is safe: events are committed only
  after they are applied, so no event is lost or applied twice.
- Do **not** manually retry a PENDING payment. See the Payment Retry Policy.
- After remediation, confirm that affected payments move to COMPLETED before closing the incident.

## Closing the incident

Record the root cause, the remediation applied, and the number of affected payments.
