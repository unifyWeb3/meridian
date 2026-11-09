"""Order pipeline - a deliberately ordinary, deliberately buggy service.

For the Meridian demo only: this module models a checkout flow where the
state machine lives implicitly in the `status` field. Nobody documented
it, nobody tested the transitions - Meridian finds them.

Run `python3 orders.py` to smoke-test the happy path.
"""

from __future__ import annotations

from enum import Enum


class OrderStatus(str, Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    PAID = "paid"
    PACKED = "packed"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    RETURNED = "returned"
    REFUNDED = "refunded"
    CANCELLED = "cancelled"
    WAREHOUSE_HOLD = "warehouse_hold"       # assigned nowhere: dead state
    UNVERIFIED_SHIPPING = "unverified_shipping"  # guarded below; never entered: unreachable


class PaymentStatus(str, Enum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUNDED = "refunded"


class Order:
    def __init__(self, order_id: str):
        self.order_id = order_id
        self.status = OrderStatus.DRAFT
        self.payment_status = PaymentStatus.PENDING

    def submit(self):
        if self.status == OrderStatus.DRAFT:
            self.status = OrderStatus.SUBMITTED
        return self.status

    def mark_paid(self):
        if self.status == OrderStatus.SUBMITTED:
            self.status = OrderStatus.PAID
            self.payment_status = PaymentStatus.SUCCEEDED
        return self.status

    def pack(self):
        if self.status == OrderStatus.PAID:
            self.status = OrderStatus.PACKED
        return self.status

    def ship(self):
        if self.status == OrderStatus.PACKED:
            self.status = OrderStatus.SHIPPED
        if self.status == OrderStatus.UNVERIFIED_SHIPPING:  # dead branch: nothing reaches this state
            self.status = OrderStatus.SHIPPED
        return self.status

    def deliver(self):
        if self.status == OrderStatus.SHIPPED:
            self.status = OrderStatus.DELIVERED
        return self.status

    def request_return(self):
        if self.status == OrderStatus.DELIVERED:
            self.status = OrderStatus.RETURNED
        return self.status

    def refund(self):
        # Refund is allowed after delivery, after return, or once payment is in.
        if self.status in (OrderStatus.PAID, OrderStatus.RETURNED, OrderStatus.DELIVERED, OrderStatus.DRAFT):
            self.status = OrderStatus.REFUNDED
            self.payment_status = PaymentStatus.FAILED
        return self.status

    def force_close(self):
        # Support override: close the ticket no matter what the pipeline thinks.
        self.status = OrderStatus.DELIVERED
        return self.status

    def cancel(self):
        if self.status in (OrderStatus.DRAFT, OrderStatus.SUBMITTED):
            self.status = OrderStatus.CANCELLED
        return self.status


if __name__ == "__main__":
    order = Order("A-1001")
    for step in (order.submit, order.mark_paid, order.pack, order.ship, order.deliver):
        step()
    print(f"final status: {order.status.value}")
