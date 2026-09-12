"""The quality gate every row passes before it may be written.

Raw engine output does not become a CSV row. It is checked here first, and a row
that fails is downgraded to the conservative fallback rather than published --
with the failure recorded, never swallowed.

Two kinds of check, deliberately distinct:

* **Contract checks** (`C*`) -- the invariants stated in `problem_statement.md`.
  These are structural: enum membership, bounds, plan shape, date rules.
* **Replay** (`P1`) -- the plan is re-walked against the forecast. This is the
  only check that proves a schedule is affordable; matching an expected shape
  does not.

M1 implements the rules reachable from `full_payment` / `wait` /
`not_recommended`. Rules that only apply to partial payments, installments and
spending changes are declared here and marked M3 so the gap is visible rather
than implied.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .forecast import Forecast
from .money import ZERO
from .planner import Decision
from .schema import (
    AFFORDABILITY_STATUSES,
    PAYMENT_METHODS,
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
        if decision.recommended_payment_method in ("full_payment", "wait") and total != requested:
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
    if decision.payments:
        extra = [(p.on_date, p.amount) for p in decision.payments]
        if not forecast.is_safe(extra):
            fail("P1", f"replay breaches the minimum balance on {forecast.breach_date(extra)}")

    # --- P2: a degraded row must not recommend spending money --------------
    if decision.degraded and decision.payments:
        fail("P2", "a degraded row (unquantified obligations) must not recommend a payment")
    if decision.degraded and decision.amount_safe_to_pay > ZERO:
        fail("P2", "a degraded row cannot certify a positive safe amount")

    return failures


#: Declared but not reachable until M3 adds their candidate generators. Listed so
#: the coverage gap is explicit rather than implied by absence.
M3_RULES = (
    "C5-C8  partial_payment: status, permission, bounds, exact two-payment schedule",
    "C9-C10 installments: exact match to a supplied option, max_installment_months",
    "E1-E14 spending changes: flexibility, category permission, protection, floor, limit",
)


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
