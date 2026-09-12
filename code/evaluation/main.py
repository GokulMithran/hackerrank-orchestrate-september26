"""Buy or Wait? -- evaluation entry point.

    python code/evaluation/main.py --show-split
    python code/evaluation/main.py --split dev    --mode deterministic
    python code/evaluation/main.py --split report --compare-baseline

Every run prints an audit header first: which script ran, which dataset and
split manifest (with its fingerprint), how many rows, which subset, the
disjointness result, and the system under test. A reader must be able to tell
what was measured without opening the code.

Milestone status: M0 owns the frozen split manifest and this header. Scoring
arrives with the financial engine (M1) -- until then the scoring modes exit 2
rather than printing a metric that no implementation produced.

Exit codes: 0 success, 1 dataset/split error, 2 not implemented yet.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_DIR))  # make `buy_or_wait` and `evaluation` importable

from buy_or_wait.data import DataError, file_sha256, load_dataset  # noqa: E402
from buy_or_wait.output import to_row  # noqa: E402
from evaluation import metrics  # noqa: E402
from evaluation.labels import load_labels, sample_exposure_note  # noqa: E402
from main import decide_one  # noqa: E402
from evaluation.splits import (  # noqa: E402
    MANIFEST_PATH,
    SplitError,
    ensure_split,
    manifest_fingerprint,
)

REPO_ROOT = CODE_DIR.parent
DEFAULT_DATASET = REPO_ROOT / "dataset"
NOT_IMPLEMENTED = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="buy-or-wait-eval", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--split", choices=("dev", "report"), default=None,
                        help="which frozen subset to score")
    parser.add_argument("--mode", choices=("deterministic", "assisted"), default="deterministic",
                        help="system under test")
    parser.add_argument("--compare-baseline", action="store_true",
                        help="score the deterministic baseline alongside the selected mode")
    parser.add_argument("--show-split", action="store_true",
                        help="print the frozen split manifest and exit")
    parser.add_argument("--verbose", action="store_true",
                        help="show why each mismatched candidate was rejected")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        data = load_dataset(args.dataset)
    except DataError as exc:
        print(f"DATASET ERROR\n{exc}", file=sys.stderr)
        return 1

    sample_path = args.dataset / "sample_requests.csv"
    if not sample_path.exists():
        print("sample_requests.csv is required for evaluation", file=sys.stderr)
        return 1

    try:
        split = ensure_split(
            [r.request_id for r in data.sample_requests],
            file_sha256(sample_path),
        )
        split.assert_disjoint()
    except SplitError as exc:
        print(f"SPLIT ERROR\n{exc}", file=sys.stderr)
        return 1

    labels = load_labels(sample_path)
    missing = sorted(set(split.dev + split.report) - set(labels))
    if missing:
        print(f"SPLIT ERROR\nlabels missing for {missing}", file=sys.stderr)
        return 1

    subset = args.split
    rows = split.subset(subset) if subset else ()

    print("============== EVALUATION AUDIT ==============")
    print(f"script            : code/evaluation/main.py")
    print(f"dataset           : {args.dataset}")
    print(f"labelled source   : sample_requests.csv ({len(data.sample_requests)} rows)")
    print(f"split manifest    : {MANIFEST_PATH}")
    print(f"split fingerprint : {manifest_fingerprint(split)}  (v{split.version}, {split.algorithm})")
    print(f"development subset: {len(split.dev)} rows  -- tuning only, excluded from reported metrics")
    print(f"reporting subset  : {len(split.report)} rows")
    print(f"disjoint check    : PASS")
    print(f"selected subset   : {subset or '(none -- manifest display only)'}"
          f"{f' [{len(rows)} rows]' if subset else ''}")
    print(f"system under test : {args.mode}")
    print(f"baseline compare  : {'yes' if args.compare_baseline else 'no'}")
    print(f"exposure          : {sample_exposure_note()}")
    print("==============================================")

    if args.show_split or subset is None:
        print(f"\ndevelopment ({len(split.dev)}): {', '.join(split.dev)}")
        print(f"reporting   ({len(split.report)}): {', '.join(split.report)}")
        if subset is None and not args.show_split:
            print("\nPass --split dev or --split report to score a subset.")
        return 0

    if args.mode == "assisted":
        print("--mode assisted needs the evidence layer (M2); not implemented yet.",
              file=sys.stderr)
        return NOT_IMPLEMENTED

    return score(data, rows, labels, verbose=args.verbose)


def score(data, request_ids, labels, *, verbose: bool = False) -> int:
    """Score the deterministic engine against the labels for `request_ids`."""
    statuses: list[tuple[str, str]] = []
    methods: list[tuple[str, str]] = []
    amounts: list[tuple[str, str, str]] = []
    plans: list[tuple[str, str]] = []
    dates: list[tuple[str, str]] = []
    degraded = 0
    mismatches: list[str] = []

    for request_id in request_ids:
        request = data.requests_by_id[request_id]
        context = data.context_for(request_id)
        decision, failures = decide_one(context, data.rates_by_key)
        row = to_row(decision, context.profile.home_currency, request.requested_amount,
                     context.profile.minimum_balance_to_keep)
        gold = labels[request_id]

        statuses.append((row["affordability_status"], gold.affordability_status))
        methods.append((row["recommended_payment_method"], gold.recommended_payment_method))
        amounts.append((context.profile.home_currency, row["amount_safe_to_pay"],
                        gold.amount_safe_to_pay))
        plans.append((row["payment_plan"], gold.payment_plan))
        dates.append((row["earliest_date_for_full_payment"], gold.earliest_date_for_full_payment))
        if decision.degraded:
            degraded += 1
        if row["recommended_payment_method"] != gold.recommended_payment_method:
            mismatches.append(
                f"  {request_id:12} pred={row['recommended_payment_method']:16} "
                f"gold={gold.recommended_payment_method:16} "
                f"safe={row['amount_safe_to_pay']} gold_safe={gold.amount_safe_to_pay}"
            )
            if verbose and decision.rejected:
                mismatches.append(f"               rejected: {decision.rejected[0]}")

    correct_status, total = metrics.accuracy(statuses)
    correct_method, _ = metrics.accuracy(methods)
    plan_exact, _ = metrics.exact_match(plans)
    date_exact, _ = metrics.exact_match(dates)
    mean_days, compared, both_empty, emptiness_mismatch = metrics.date_day_error(dates)

    print("\n------------------ RESULTS ------------------")
    print(f"affordability_status      : {correct_status}/{total}  "
          f"macro-F1 {metrics.macro_f1(statuses):.3f}")
    print(f"recommended_payment_method: {correct_method}/{total}  "
          f"macro-F1 {metrics.macro_f1(methods):.3f}")
    print(f"payment_plan (exact)      : {plan_exact}/{total}")
    print(f"earliest_date (exact)     : {date_exact}/{total}  "
          f"(mean |days| {mean_days:.1f} over {compared}; both-empty {both_empty}; "
          f"emptiness disagreement {emptiness_mismatch})")
    print(f"degraded rows             : {degraded}/{total}  (counted as wrong, never dropped)")

    print("\namount_safe_to_pay by currency (never pooled):")
    for currency, stats in metrics.amount_error_by_currency(amounts).items():
        print(f"  {currency}  n={stats['n']:<3} exact={stats['exact']}/{stats['n']} "
              f"MAE={stats['mae']:.2f}  normalized MAE={stats['normalized_mae']:.3f}")

    print(f"\nstatus confusion (gold->pred): {dict(metrics.confusion(statuses))}")
    print(f"method confusion (gold->pred): {dict(metrics.confusion(methods))}")
    if mismatches:
        print("\n---------------- METHOD MISMATCHES ----------------")
        for line in mismatches:
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
