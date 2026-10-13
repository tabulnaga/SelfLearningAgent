---
title: Kafka Consumer Operations Guide
doc_id: OPS-KAFKA-002
classification: internal
domain: payments
version: 2.0
---

# Kafka Consumer Operations Guide

## Consumers in the payments platform

| Consumer | Topic | Purpose |
|---|---|---|
| payment-events | payments.events | Applies PSP payment confirmations; moves payments from PENDING to COMPLETED |
| refund-events | payments.refunds | Applies refund confirmations |

## Checking consumer health

A consumer is healthy when its state is `running` and its lag stays below 500 messages. A consumer
whose state is `stalled`, or whose lag grows steadily while the topic receives traffic, is not
processing events.

## Restarting a consumer

1. Confirm the consumer is stalled or lagging (see above).
2. Restart the consumer. In production, open a standard change ticket first; in staging no ticket is
   needed.
3. Watch the lag for 5 minutes. It should fall steadily.
4. Confirm downstream effects (for payment-events: PENDING payments move to COMPLETED).

Restarting is safe because offsets are committed only after an event has been applied.

## When a restart does not help

If the consumer stalls again within 15 minutes of a restart, do not keep restarting it. Escalate to
the Payments Platform team with the consumer logs.
