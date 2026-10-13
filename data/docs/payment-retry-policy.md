---
title: Payment Retry Policy
doc_id: POL-PAY-004
classification: internal
domain: payments
version: 1.4
---

# Payment Retry Policy

## Policy

1. A payment in **FAILED** status may be retried up to 3 times, at least 2 minutes apart.
2. A payment in **PENDING** status must **not** be manually retried. The original request may still
   complete, and a manual retry can charge the customer twice.
3. Retries must use the original idempotency key so the PSP can reject duplicates.

## Rationale

Most PENDING payments are not failed payments: the PSP has accepted them and the confirmation has
not yet been applied on our side. Retrying them treats a delay as a failure.

## Exceptions

Exceptions require written approval from the Payments Operations Lead.
