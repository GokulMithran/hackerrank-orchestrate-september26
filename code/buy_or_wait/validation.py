"""The quality gate every row passes before it may be written.

Raw engine output does not become a CSV row. It is checked here first, and a row
that fails is downgraded to the conservative fallback rather than published --
with the failure recorded, never swallowed.

Two kinds of check, deliberately distinct:

* **Contract checks** (`C*`) -- the invariants stated in `problem_statement.md`.
  These are structural: enum membership, bounds, plan shape, date rules.
* **Replay** (`P1`) -- the plan is re-walked against the forecast (with any
  spending changes independently reconstructed and applied). This is the only
  check that proves a schedule is affordable; matching an expected shape does
  not.

`C5`-`C10` and `E1`-`E7` are M3's additions, covering `partial_payment`,
`installments`, and `spending_changes_needed`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Optional, Sequence

from . import spending
from .forecast import Forecast
from .money import ZERO
from .planner import Decision
from .schema import (
    AFFORDABILITY_STATUSES,
    PAYMENT_METHODS,
    REDUCIBLE_FLEXIBILITIES,
    STOPPABLE_FLEXIBILITIES,
    FinancialEvent,
    PaymentOption,
    Profile,
    RequestInput,
)


@dataclass(frozen=True)
class GateFailure:
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.rule}: {self.detail}"


def check(
    decision: Decision,
    request: RequestInput,
    profile: Profile,
    forecast: Forecast,
    payment_options: Sequence[PaymentOption] = (),
    events: Sequence[FinancialEvent] = (),
) -> list[GateFailure]:
    """Return every violated rule. Empty means the row may be published."""
    failures: list[GateFailure] = []
    requested = request.requested_amount

    def fail(rule: str, detail: str) -> None:
        failures.append(GateFailure(rule, detail))

    # --- C0: enum membership ------------------------------------------------
    if decision.affordability_status not in AFFORDABILITY_STATUSES:
        fail("C0", f"unknown affordability_status {decision.affordability_status!r}")
    if decision.recommended_payment_method not in PAYMENT_METHODS:
        fail("C0", f"unknown recommended_payment_method {decision.recommended_payment_method!r}")

    # --- C1: 0 <= amount_safe_to_pay <= requested_amount --------------------
    if not (ZERO <= decision.amount_safe_to_pay <= requested):
        fail("C1", f"amount_safe_to_pay {decision.amount_safe_to_pay} outside [0, {requested}]")

    # --- C2-C4: affordable_now --------------------------------------------
    if decision.affordability_status == "affordable_now":
        if decision.recommended_payment_method != "full_payment":
            fail("C2", "affordable_now must pair with full_payment")
        if decision.earliest_date_for_full_payment != request.request_date:
            fail("C3", "affordable_now requires earliest_date_for_full_payment == request_date")
        if decision.amount_safe_to_pay != requested:
            fail("C4", "affordable_now requires amount_safe_to_pay == requested_amount")

    # --- C5-C8: partial_payment ----------------------------------------------
    if decision.recommended_payment_method == "partial_payment":
        if decision.affordability_status != "affordable_with_plan":
            fail("C5", "partial_payment must pair with affordable_with_plan")
        if not request.allows_partial_payment:
            fail("C6", "partial_payment recommended but the request does not allow it")
        if not (ZERO < decision.amount_safe_to_pay < requested):
            fail("C7", "partial_payment requires 0 < amount_safe_to_pay < requested_amount")
        if len(decision.payments) != 2:
            fail("C8", "partial_payment must schedule exactly two payments")
        else:
            first, second = decision.payments
            if first.on_date != request.request_date or first.amount != decision.amount_safe_to_pay:
                fail("C8", "the first partial payment must be amount_safe_to_pay on request_date")
            if second.amount != requested - decision.amount_safe_to_pay:
                fail("C8", "the second partial payment must be the remainder of requested_amount")
            if (decision.earliest_date_for_full_payment is None
                    or second.on_date != decision.earliest_date_for_full_payment):
                fail("C8", "the second partial payment must land on earliest_date_for_full_payment")
            if second.on_date > request.desired_completion_date:
                fail("C8", "the second partial payment is after desired_completion_date")

    # --- C9-C10: installments --------------------------------------------------
    if decision.recommended_payment_method == "installments":
        if decision.affordability_status != "affordable_with_plan":
            fail("C9", "installments must pair with affordable_with_plan")
        option = next(
            (o for o in payment_options if o.payment_option_id == decision.chosen_payment_option_id),
            None,
        )
        if option is None:
            fail("C9", f"installments does not reference a supplied payment option "
                       f"({decision.chosen_payment_option_id!r})")
        else:
            expected_dates = [
                option.first_payment_date + timedelta(days=(option.payment_frequency_days or 0) * i)
                for i in range(option.number_of_payments)
            ]
            expected = [(d, option.payment_amount) for d in expected_dates]
            actual = [(p.on_date, p.amount) for p in decision.payments]
            if actual != expected:
                fail("C9", "installment schedule does not exactly match the supplied payment option")
            if profile.max_installment_months is None:
                fail("C10", "installments recommended but the user's max_installment_months is blank")
            elif expected_dates:
                term_months = Decimal((expected_dates[-1] - expected_dates[0]).days) / Decimal(30)
                if term_months > Decimal(profile.max_installment_months):
                    fail("C10", f"installment term ~{term_months} months exceeds "
                               f"max_installment_months {profile.max_installment_months}")

    # --- E1-E7: spending_changes_needed ---------------------------------------
    if decision.spending_changes:
        if decision.affordability_status != "affordable_with_plan":
            fail("E1", "spending changes require affordable_with_plan")
        if len(decision.spending_changes) > 3:
            fail("E2", "at most three spending-change actions are allowed")
        events_by_id = {e.event_id: e for e in events}
        seen: set[str] = set()
        for item in decision.spending_changes:
            parsed = spending.from_literal(item)
            if parsed is None:
                fail("E3", f"unparseable spending change {item!r}")
                continue
            action, event_id, new_amount = parsed
            if event_id in seen:
                fail("E4", f"event {event_id} is referenced by more than one spending change")
            seen.add(event_id)
            event = events_by_id.get(event_id)
            if event is None:
                fail("E5", f"spending change references unknown event_id {event_id!r}")
                continue
            if event.category in profile.expense_categories_to_protect:
                fail("E6", f"{event_id}: category {event.category} is protected")
            if action == "stop":
                if event.flexibility not in STOPPABLE_FLEXIBILITIES:
                    fail("E7", f"{event_id}: flexibility {event.flexibility} does not allow stopping")
                if event.category not in profile.expense_categories_user_is_willing_to_stop:
                    fail("E7", f"{event_id}: category {event.category} is not in willing-to-stop")
            else:
                if event.flexibility not in REDUCIBLE_FLEXIBILITIES:
                    fail("E7", f"{event_id}: flexibility {event.flexibility} does not allow reducing")
                if event.category not in profile.expense_categories_user_is_willing_to_reduce:
                    fail("E7", f"{event_id}: category {event.category} is not in willing-to-reduce")
                if event.minimum_allowed_amount is not None and new_amount < event.minimum_allowed_amount:
                    fail("E7", f"{event_id}: reduce_to {new_amount} is below "
                               f"minimum_allowed_amount {event.minimum_allowed_amount}")
                if event.amount is not None and new_amount >= event.amount:
                    fail("E7", f"{event_id}: reduce_to {new_amount} is not a reduction")

    # --- C11: the method must be one the user accepts -----------------------
    if decision.recommended_payment_method in ("full_payment", "partial_payment", "installments"):
        if decision.recommended_payment_method not in profile.payment_methods_user_will_consider:
            fail("C11", f"{decision.recommended_payment_method} is not accepted by this user")

    # --- C12: wait ----------------------------------------------------------
    if decision.recommended_payment_method == "wait":
        if "full_payment" not in profile.payment_methods_user_will_consider:
            fail("C12", "wait requires the user to accept full_payment")
        if decision.earliest_date_for_full_payment is None:
            fail("C12", "wait requires a known earliest_date_for_full_payment")
        elif [(p.on_date, p.amount) for p in decision.payments] != [
            (decision.earliest_date_for_full_payment, requested)
        ]:
            fail("C12", "wait must schedule exactly the full amount on the earliest safe date")

    # --- C13: not_recommended ----------------------------------------------
    # NOTE: this deliberately does NOT require an empty earliest date. Capacity
    # is independent of method preference (problem_statement.md:163): a user can
    # have the cash today and still accept no safe method.
    if decision.recommended_payment_method == "not_recommended":
        if decision.payments:
            fail("C13", "not_recommended must carry no payment plan")
        if decision.spending_changes:
            fail("C13", "not_recommended must carry no spending changes")

    # --- C14: status and method agree --------------------------------------
    if (decision.affordability_status == "not_affordable") != (
        decision.recommended_payment_method == "not_recommended"
    ):
        fail("C14", "not_affordable and not_recommended must occur together")

    # --- C15: plan is chronological, positive, and completes the request ----
    dates = [p.on_date for p in decision.payments]
    if dates != sorted(dates):
        fail("C15", "payment_plan is not chronological")
    if any(p.amount <= ZERO for p in decision.payments):
        fail("C15", "payment_plan contains a non-positive amount")
    if decision.payments:
        total = sum((p.amount for p in decision.payments), ZERO)
        if (decision.recommended_payment_method in ("full_payment", "wait", "partial_payment")
                and total != requested):
            fail("C15", f"plan total {total} != requested_amount {requested}")

    # --- C16: a recommended plan must finish by the deadline ---------------
    if decision.payments and decision.payments[-1].on_date > request.desired_completion_date:
        fail("C16", f"plan completes {decision.payments[-1].on_date}, after "
                    f"{request.desired_completion_date}")

    # --- C17: earliest date must sit inside the forecast window ------------
    earliest = decision.earliest_date_for_full_payment
    if earliest is not None and not (request.request_date <= earliest <= forecast.horizon):
        fail("C17", f"earliest_date_for_full_payment {earliest} outside the forecast window")

    # --- P1: independent replay -- the only real proof of affordability ----
    # Spending changes are reconstructed from the *published strings*, not
    # trusted from whatever object produced the decision, so this replay is a
    # genuine check of what the row says rather than of the planner's memory.
    if decision.payments:
        events_by_id = {e.event_id: e for e in events}
        replay_forecast = (
            spending.apply_literals(forecast, decision.spending_changes, events_by_id)
            if decision.spending_changes else forecast
        )
        extra = [(p.on_date, p.amount) for p in decision.payments]
        if not replay_forecast.is_safe(extra):
            fail("P1", f"replay breaches the minimum balance on {replay_forecast.breach_date(extra)}")

    # --- P2: a degraded row must not recommend spending money --------------
    if decision.degraded and decision.payments:
        fail("P2", "a degraded row (unquantified obligations) must not recommend a payment")
    if decision.degraded and decision.amount_safe_to_pay > ZERO:
        fail("P2", "a degraded row cannot certify a positive safe amount")

    return failures


def conservative_fallback(
    request: RequestInput,
    reason: str,
    *,
    earliest: Optional[object] = None,
) -> Decision:
    """The safe row used when a decision cannot be certified or fails the gate.

    Uses only values in the fixed eight-column contract -- there is no
    `insufficient_evidence` enum in September, and inventing one would produce an
    invalid CSV. The reason is carried in the audit trace, not the CSV.
    """
    return Decision(
        request_id=request.request_id,
        amount_safe_to_pay=ZERO,
        affordability_status="not_affordable",
        recommended_payment_method="not_recommended",
        payments=(),
        earliest_date_for_full_payment=None,
        spending_changes=(),
        degraded=True,
        degradation_reason=reason,
        rejected=(),
        chosen_payment_option_id=None,
        facts=(),
    )
