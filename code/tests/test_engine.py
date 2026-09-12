"""M1 financial core: cash state, FX, recurrence, forecast, planning, gating.

Safety claims are proved two ways: against hand-computed expectations, and
against `tests/oracle.py`, an independent replay written from the contract
rather than from `forecast.py`.
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from buy_or_wait import config, planner, recurrence, state, validation  # noqa: E402
from buy_or_wait import forecast as F  # noqa: E402
from buy_or_wait.fx import RateUnavailable, convert  # noqa: E402
from buy_or_wait.money import ZERO  # noqa: E402
from buy_or_wait.schema import ExchangeRate, FinancialEvent, Profile, RequestInput  # noqa: E402
from tests import oracle  # noqa: E402

D = Decimal
TODAY = date(2024, 3, 1)


# ------------------------------------------------------------- builders -----


def event(event_id, *, amount="100", direction="debit", status="settled", on=TODAY,
          category="groceries", description="shop", currency="ZAR", settle=None,
          flexibility="fixed", floor=None, event_type=None, link=None) -> FinancialEvent:
    return FinancialEvent(
        event_id=event_id, user_id="user_01",
        event_type=event_type or ("income" if direction == "credit" else "expense"),
        description=description, category=category, direction=direction,
        amount=None if amount is None else D(amount), currency=currency,
        event_date=on, settlement_date=settle if settle is not None else on,
        status=status, linked_event_id=link, flexibility=flexibility,
        minimum_allowed_amount=None if floor is None else D(floor),
    )


def profile(balance="10000", minimum="2000", methods=("full_payment",), currency="ZAR") -> Profile:
    return Profile(
        user_id="user_01", home_currency=currency,
        current_available_balance=D(balance), minimum_balance_to_keep=D(minimum),
        financial_priorities=(), expense_categories_to_protect=frozenset({"rent"}),
        expense_categories_user_is_willing_to_reduce=frozenset({"dining"}),
        expense_categories_user_is_willing_to_stop=frozenset({"streaming"}),
        payment_methods_user_will_consider=frozenset(methods), max_installment_months=6,
    )


def request(amount="1000", *, deadline_days=30, partial=False) -> RequestInput:
    return RequestInput(
        request_id="request_01", user_id="user_01", request_date=TODAY,
        request_type="purchase", requested_amount=D(amount),
        desired_completion_date=TODAY + timedelta(days=deadline_days),
        allows_partial_payment=partial, request_text="",
    )


def rate_table(*rows) -> dict:
    return {(r.rate_date, r.from_currency, r.to_currency): r for r in rows}


def build(events, prof=None, rates=None, when=TODAY):
    prof = prof or profile()
    rates = rates if rates is not None else {}
    cash = state.reconstruct(events, prof, when, rates)
    rec = recurrence.detect(events, prof, when, rates)
    return cash, rec, F.build(cash, rec, when)


# ----------------------------------------------------------------- FX -------


class FxTests(unittest.TestCase):
    RATES = rate_table(ExchangeRate(date(2024, 3, 15), "EUR", "ZAR", D("20.12345")))

    def test_identity_conversion_needs_no_rate(self):
        got = convert(D("100"), from_currency="ZAR", to_currency="ZAR",
                      on_date=TODAY, rates={})
        self.assertEqual(got.amount, D("100"))
        self.assertIsNone(got.rate)

    def test_exact_date_uses_full_rate_precision(self):
        got = convert(D("100"), from_currency="EUR", to_currency="ZAR",
                      on_date=date(2024, 3, 15), rates=self.RATES)
        self.assertEqual(got.rate, D("20.12345"))     # rate NOT rounded
        self.assertEqual(got.amount, D("2012.35"))    # result IS rounded

    def test_an_earlier_rate_does_not_satisfy_a_later_date(self):
        with self.assertRaises(RateUnavailable):
            convert(D("100"), from_currency="EUR", to_currency="ZAR",
                    on_date=date(2024, 4, 15), rates=self.RATES)

    def test_reverse_direction_is_not_inferred(self):
        with self.assertRaises(RateUnavailable):
            convert(D("100"), from_currency="ZAR", to_currency="EUR",
                    on_date=date(2024, 3, 15), rates=self.RATES)


# ------------------------------------------------------------ cash state ----


class CashStateTests(unittest.TestCase):
    def test_settled_history_is_not_replayed_onto_the_balance(self):
        events = [event(f"e{i}", amount="500", on=TODAY - timedelta(days=i)) for i in range(1, 6)]
        cash, _, forecast = build(events)
        self.assertEqual(forecast.opening_balance, D("10000"))
        self.assertEqual([m.event_id for m in cash.confirmed], [])
        self.assertTrue(all(r.reason == state.ALREADY_IN_BALANCE for r in cash.excluded))

    def test_pending_debit_is_reserved_exactly_once(self):
        events = [event("e1", amount="500", status="pending", on=TODAY + timedelta(days=3))]
        cash, _, forecast = build(events)
        self.assertEqual(len(cash.confirmed), 1)
        self.assertEqual(forecast.minimum_over_window(), D("9500"))

    def test_a_pending_debit_already_past_is_reserved_immediately(self):
        events = [event("e1", amount="500", status="pending", on=TODAY - timedelta(days=5))]
        cash, _, _ = build(events)
        self.assertEqual(cash.confirmed[0].on_date, TODAY)

    def test_pending_credit_does_not_fund_anything(self):
        events = [event("e1", amount="5000", direction="credit", status="pending",
                        on=TODAY + timedelta(days=2), category="shopping")]
        cash, _, forecast = build(events)
        self.assertEqual(cash.confirmed, ())
        self.assertEqual(forecast.minimum_over_window(), D("10000"))
        self.assertEqual(cash.excluded[0].reason, state.PENDING_CREDIT)

    def test_cancelled_failed_and_unrealized_are_excluded(self):
        events = [
            event("cancelled", status="cancelled", on=TODAY + timedelta(days=1)),
            event("failed", status="failed", on=TODAY + timedelta(days=1)),
            event("unrealized", status="unrealized", direction="non_cash",
                  category="investment", on=TODAY + timedelta(days=1)),
        ]
        cash, _, _ = build(events)
        self.assertEqual(cash.confirmed, ())
        self.assertEqual({r.reason for r in cash.excluded},
                         {state.CANCELLED, state.FAILED, state.UNREALIZED})

    def test_unknown_debit_is_an_unfunded_obligation(self):
        events = [event("e1", amount=None, status="pending", on=TODAY + timedelta(days=2))]
        cash, _, forecast = build(events)
        self.assertEqual(len(cash.unfunded_obligations), 1)
        self.assertFalse(forecast.certifiable)

    def test_unknown_credit_is_not_an_unfunded_obligation(self):
        # Missing income only makes us more cautious, so it does not block.
        events = [event("e1", amount=None, direction="credit", status="scheduled",
                        category="salary", on=TODAY + timedelta(days=2))]
        cash, _, forecast = build(events)
        self.assertEqual(cash.unfunded_obligations, ())
        self.assertTrue(forecast.certifiable)

    def test_foreign_event_without_a_rate_becomes_an_unfunded_obligation(self):
        events = [event("e1", amount="100", currency="EUR", status="pending",
                        on=TODAY + timedelta(days=2))]
        cash, _, forecast = build(events, rates={})
        self.assertEqual(cash.excluded[0].reason, state.RATE_UNAVAILABLE)
        self.assertFalse(forecast.certifiable)


# ------------------------------------------------------------ recurrence ----


class RecurrenceTests(unittest.TestCase):
    def _monthly(self, n, *, amount="500", direction="debit", category="rent",
                 description="rent", last_offset=5, status="settled"):
        return [
            event(f"{description}{i}", amount=amount, direction=direction, category=category,
                  description=description, status=status,
                  on=TODAY - timedelta(days=last_offset + 30 * i))
            for i in range(n)
        ]

    def test_constant_monthly_debit_becomes_a_fixed_series(self):
        rec = build(self._monthly(4))[1]
        self.assertEqual(len(rec.fixed), 1)
        self.assertEqual(rec.fixed[0].period_days, 30)
        self.assertEqual(rec.fixed[0].amount_home, D("500"))

    def test_two_occurrences_are_not_a_debit_cadence(self):
        rec = build(self._monthly(2))[1]
        self.assertEqual([s for s in rec.fixed if s.direction == "debit"], [])

    def test_varying_amounts_feed_the_category_rate_not_a_fixed_series(self):
        events = [event(f"g{i}", amount=str(100 + i * 7), category="groceries",
                        description="shop", on=TODAY - timedelta(days=5 + 30 * i))
                  for i in range(4)]
        rec = build(events)[1]
        self.assertEqual([s for s in rec.fixed if s.direction == "debit"], [])
        self.assertEqual([r.category for r in rec.rates], ["groceries"])

    def test_every_settled_debit_is_counted_exactly_once(self):
        fixed = self._monthly(4, amount="500", category="rent", description="rent")
        variable = [event(f"g{i}", amount=str(90 + i), category="groceries",
                          description=f"shop {i}", on=TODAY - timedelta(days=3 + 7 * i))
                    for i in range(6)]
        rec = build(fixed + variable)[1]
        in_series = {eid for s in rec.fixed for eid in s.event_ids}
        in_rates = {eid for r in rec.rates for eid in r.event_ids}
        self.assertEqual(in_series & in_rates, set(), "an event fed both a series and a rate")
        self.assertEqual(in_series | in_rates, {e.event_id for e in fixed + variable})

    def test_income_needs_only_two_occurrences(self):
        rec = build(self._monthly(2, direction="credit", category="salary",
                                  description="pay", amount="4000"))[1]
        self.assertEqual([s.direction for s in rec.fixed], ["credit"])

    def test_lapsed_income_is_not_projected(self):
        # Last salary 70 days ago on a 30-day cadence: it stopped.
        rec = build(self._monthly(3, direction="credit", category="salary",
                                  description="pay", amount="4000", last_offset=70))[1]
        self.assertEqual([s for s in rec.fixed if s.direction == "credit"], [])

    def test_income_under_two_different_descriptions_is_one_cadence(self):
        events = [
            event("s1", amount="4000", direction="credit", category="salary",
                  description="Prorated first salary", on=TODAY - timedelta(days=35)),
            event("s2", amount="5000", direction="credit", category="salary",
                  description="Next confirmed salary", status="scheduled",
                  on=TODAY - timedelta(days=5)),
        ]
        rec = build(events)[1]
        credits = [s for s in rec.fixed if s.direction == "credit"]
        self.assertEqual(len(credits), 1)
        self.assertEqual(credits[0].amount_home, D("5000"))  # latest pay rate

    def test_investment_contributions_are_not_reserved_as_essential(self):
        events = [event(f"i{i}", amount="300", category="investment", description=f"buy {i}",
                        on=TODAY - timedelta(days=5 + 30 * i)) for i in range(4)]
        rec = build(events)[1]
        self.assertNotIn("investment", [r.category for r in rec.rates])


# -------------------------------------------------------------- forecast ----


class ForecastTests(unittest.TestCase):
    def test_agrees_with_the_independent_oracle(self):
        events = [
            event("d1", amount="900", status="scheduled", on=TODAY + timedelta(days=10)),
            event("d2", amount="400", status="pending", on=TODAY + timedelta(days=20)),
            event("c1", amount="2500", direction="credit", status="scheduled",
                  category="salary", on=TODAY + timedelta(days=15)),
        ]
        _, _, forecast = build(events)
        for payment in ([], [(TODAY, D("500"))], [(TODAY + timedelta(days=40), D("3000"))]):
            with self.subTest(payment=payment):
                self.assertEqual(
                    forecast.minimum_over_window(payment),
                    oracle.lowest_balance(D("10000"), forecast.movements, payment),
                )
                self.assertEqual(
                    forecast.is_safe(payment),
                    oracle.is_safe(D("10000"), D("2000"), forecast.movements, payment),
                )

    def test_hand_computed_balance(self):
        # 10000 - 900 (day 10) + 2500 (day 15) - 400 (day 20) = 11200, trough 9100.
        events = [
            event("d1", amount="900", status="scheduled", on=TODAY + timedelta(days=10)),
            event("c1", amount="2500", direction="credit", status="scheduled",
                  category="salary", on=TODAY + timedelta(days=15)),
            event("d2", amount="400", status="pending", on=TODAY + timedelta(days=20)),
        ]
        _, _, forecast = build(events)
        self.assertEqual(forecast.minimum_over_window(), D("9100"))
        self.assertEqual(forecast.headroom(), D("7100"))

    def test_landing_exactly_on_the_minimum_is_safe(self):
        _, _, forecast = build([])
        self.assertTrue(forecast.is_safe([(TODAY, D("8000"))]))      # 10000-8000 == minimum
        self.assertFalse(forecast.is_safe([(TODAY, D("8000.01"))]))

    def test_debits_are_applied_before_credits_on_the_same_date(self):
        when = TODAY + timedelta(days=5)
        events = [
            event("d1", amount="9000", status="scheduled", on=when),
            event("c1", amount="9000", direction="credit", status="scheduled",
                  category="salary", on=when),
        ]
        _, _, forecast = build(events)
        # Net zero by end of day, but the intraday low is 1000 -> below the minimum.
        self.assertEqual(forecast.minimum_over_window(), D("1000"))
        self.assertFalse(forecast.is_safe())

    def test_a_later_bill_blocks_a_payment_that_looks_affordable_today(self):
        events = [event("rent", amount="7000", status="scheduled", on=TODAY + timedelta(days=45))]
        _, _, forecast = build(events)
        self.assertFalse(forecast.is_safe([(TODAY, D("2000"))]))
        self.assertEqual(forecast.breach_date([(TODAY, D("2000"))]), TODAY + timedelta(days=45))

    def test_window_boundaries(self):
        _, _, forecast = build([])
        self.assertEqual(forecast.horizon, TODAY + timedelta(days=config.FORECAST_DAYS))
        inside = event("d", amount="9000", status="scheduled", on=forecast.horizon)
        outside = event("d", amount="9000", status="scheduled",
                        on=forecast.horizon + timedelta(days=1))
        self.assertFalse(build([inside])[2].is_safe())
        self.assertTrue(build([outside])[2].is_safe())

    def test_metamorphic_an_extra_debit_cannot_increase_capacity(self):
        base = build([])[2]
        extra = build([event("x", amount="750", status="scheduled",
                             on=TODAY + timedelta(days=30))])[2]
        self.assertLessEqual(extra.headroom(), base.headroom())
        self.assertLessEqual(extra.amount_safe_to_pay(D("99999")),
                             base.amount_safe_to_pay(D("99999")))

    def test_unquantified_debit_forces_zero_safe_amount(self):
        events = [event("u", amount=None, status="pending", on=TODAY + timedelta(days=2))]
        _, _, forecast = build(events)
        self.assertFalse(forecast.certifiable)
        self.assertEqual(forecast.amount_safe_to_pay(D("100")), ZERO)
        self.assertIsNone(forecast.earliest_full_payment_date(D("100")))

    def test_earliest_date_is_the_first_date_that_survives_the_whole_window(self):
        events = [event("c1", amount="5000", direction="credit", status="scheduled",
                        category="salary", on=TODAY + timedelta(days=20))]
        _, _, forecast = build(events)
        earliest = forecast.earliest_full_payment_date(D("12000"))
        self.assertEqual(earliest, TODAY + timedelta(days=20))
        self.assertTrue(forecast.is_safe([(earliest, D("12000"))]))
        self.assertFalse(forecast.is_safe([(earliest - timedelta(days=1), D("12000"))]))


# ------------------------------------------------------ planner and gate ----


class PlannerTests(unittest.TestCase):
    def test_affordable_now(self):
        _, _, forecast = build([])
        decision = planner.choose(request("1000"), profile(), forecast)
        self.assertEqual(decision.affordability_status, "affordable_now")
        self.assertEqual(decision.recommended_payment_method, "full_payment")
        self.assertEqual(decision.earliest_date_for_full_payment, TODAY)
        self.assertEqual(decision.amount_safe_to_pay, D("1000"))

    def test_wait_when_capacity_arrives_before_the_deadline(self):
        events = [event("c1", amount="6000", direction="credit", status="scheduled",
                        category="salary", on=TODAY + timedelta(days=10))]
        _, _, forecast = build(events)
        decision = planner.choose(request("12000", deadline_days=30), profile(), forecast)
        self.assertEqual(decision.recommended_payment_method, "wait")
        self.assertEqual(decision.affordability_status, "affordable_later")
        self.assertEqual(decision.payments[0].on_date, TODAY + timedelta(days=10))

    def test_capacity_after_the_deadline_is_reported_but_not_recommended(self):
        events = [event("c1", amount="6000", direction="credit", status="scheduled",
                        category="salary", on=TODAY + timedelta(days=40))]
        _, _, forecast = build(events)
        decision = planner.choose(request("12000", deadline_days=20), profile(), forecast)
        self.assertEqual(decision.recommended_payment_method, "not_recommended")
        # Capacity is independent of eligibility (problem_statement.md:163).
        self.assertEqual(decision.earliest_date_for_full_payment, TODAY + timedelta(days=40))

    def test_a_method_the_user_rejects_is_never_recommended(self):
        _, _, forecast = build([])
        decision = planner.choose(request("1000"), profile(methods=("installments",)), forecast)
        self.assertEqual(decision.recommended_payment_method, "not_recommended")
        self.assertTrue(any("not in payment_methods" in r for r in decision.rejected))

    def test_unquantified_obligation_blocks_every_recommendation(self):
        events = [event("u", amount=None, status="pending", on=TODAY + timedelta(days=2))]
        _, _, forecast = build(events)
        decision = planner.choose(request("100"), profile(), forecast)
        self.assertEqual(decision.recommended_payment_method, "not_recommended")
        self.assertTrue(decision.degraded)
        self.assertEqual(decision.amount_safe_to_pay, ZERO)

    def test_amount_safe_to_pay_is_capped_at_the_requested_amount(self):
        _, _, forecast = build([])
        decision = planner.choose(request("50"), profile(), forecast)
        self.assertEqual(decision.amount_safe_to_pay, D("50"))


class GateTests(unittest.TestCase):
    def check(self, decision, req=None, prof=None, forecast=None):
        req = req or request("1000")
        prof = prof or profile()
        forecast = forecast or build([])[2]
        return [f.rule for f in validation.check(decision, req, prof, forecast)]

    def good(self):
        return planner.choose(request("1000"), profile(), build([])[2])

    def test_a_clean_decision_passes(self):
        self.assertEqual(self.check(self.good()), [])

    def test_c1_amount_outside_bounds(self):
        bad = self.good()._replace() if hasattr(self.good(), "_replace") else None
        decision = planner.Decision(**{**self.good().__dict__, "amount_safe_to_pay": D("9999")})
        self.assertIn("C1", self.check(decision))

    def test_c3_affordable_now_requires_earliest_equals_request_date(self):
        decision = planner.Decision(**{**self.good().__dict__,
                                       "earliest_date_for_full_payment": TODAY + timedelta(days=5)})
        self.assertIn("C3", self.check(decision))

    def test_c11_method_not_accepted(self):
        self.assertIn("C11", self.check(self.good(), prof=profile(methods=("installments",))))

    def test_c14_status_and_method_must_agree(self):
        decision = planner.Decision(**{**self.good().__dict__,
                                       "affordability_status": "not_affordable"})
        self.assertIn("C14", self.check(decision))

    def test_c16_plan_after_the_deadline(self):
        late = request("1000", deadline_days=0)
        decision = planner.Decision(**{**self.good().__dict__,
                                       "payments": (planner.Payment(TODAY + timedelta(days=5), D("1000")),)})
        self.assertIn("C16", self.check(decision, req=late))

    def test_p1_replay_catches_a_schedule_that_breaches(self):
        forecast = build([event("rent", amount="9500", status="scheduled",
                                on=TODAY + timedelta(days=10))])[2]
        decision = planner.Decision(**{**self.good().__dict__,
                                       "payments": (planner.Payment(TODAY, D("1000")),)})
        self.assertIn("P1", self.check(decision, forecast=forecast))

    def test_p2_degraded_row_cannot_recommend_a_payment(self):
        decision = planner.Decision(**{**self.good().__dict__, "degraded": True})
        failures = self.check(decision)
        self.assertIn("P2", failures)

    def test_conservative_fallback_is_itself_valid(self):
        fallback = validation.conservative_fallback(request("1000"), "test")
        self.assertEqual(self.check(fallback), [])
        self.assertEqual(fallback.amount_safe_to_pay, ZERO)
        self.assertEqual(fallback.recommended_payment_method, "not_recommended")


if __name__ == "__main__":
    unittest.main()
