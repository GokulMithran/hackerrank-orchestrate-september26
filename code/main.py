"""Buy or Wait? -- command line entry point.

    python code/main.py                      # audit the dataset (default)
    python code/main.py --mode audit
    python code/main.py --mode deterministic --out output.csv
    python code/main.py --mode assisted --out output.csv

Milestone status (see docs/IMPLEMENTATION_STATUS.md): M0 implements the
contract, loaders, dataset audit, split manifest and this CLI. The prediction
modes are wired but deliberately refuse to run until the financial engine lands
in M1 -- emitting placeholder rows and calling them a baseline would be a false
claim of implemented behaviour.

Exit codes: 0 success, 1 dataset audit errors, 2 not implemented yet.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # make `buy_or_wait` importable

from buy_or_wait import forecast as forecast_module  # noqa: E402
from buy_or_wait import planner, recurrence, state, validation  # noqa: E402
from buy_or_wait.audit import audit_dataset, errors  # noqa: E402
from buy_or_wait.data import DataError, load_dataset  # noqa: E402
from buy_or_wait.output import PublishError, publish, to_row  # noqa: E402
from buy_or_wait.schema import RequestContext  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = REPO_ROOT / "dataset"
DEFAULT_OUTPUT = REPO_ROOT / "output.csv"

NOT_IMPLEMENTED = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="buy-or-wait", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET,
                        help="dataset directory (default: <repo>/dataset)")
    parser.add_argument("--mode", choices=("audit", "deterministic", "assisted"), default="audit",
                        help="audit: validate the dataset only. deterministic: no model calls. "
                             "assisted: deterministic engine plus evidence extraction.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT,
                        help="prediction output path (default: <repo>/output.csv)")
    parser.add_argument("--limit", type=int, default=None,
                        help="process only the first N requests (smoke runs)")
    parser.add_argument("--quiet", action="store_true", help="print only the summary")
    return parser


def run_audit(dataset_dir: Path, *, quiet: bool = False) -> int:
    try:
        data = load_dataset(dataset_dir)
    except DataError as exc:
        print(f"DATASET ERROR\n{exc}", file=sys.stderr)
        return 1

    findings = audit_dataset(data)
    failures = errors(findings)

    if not quiet:
        print("=============== DATASET AUDIT ===============")
        print(f"dataset            : {data.dataset_dir}")
        print(f"evaluation requests: {len(data.requests)}")
        print(f"public samples     : {len(data.sample_requests)}")
        print(f"profiles           : {len(data.profiles)}")
        print(f"financial events   : {len(data.events)}  "
              f"(unknown amounts: {sum(1 for e in data.events if e.amount_is_unknown)})")
        print(f"payment options    : {len(data.payment_options)}")
        print(f"messages / images  : {len(data.messages)} / {len(data.images)}")
        print(f"exchange rates     : {len(data.exchange_rates)}")
        print("--- input file hashes (sha256, first 16) ---")
        for name, digest in sorted(data.manifest.items()):
            print(f"  {name:32} {digest[:16]}")
        print(f"--- media content hashes ({len(data.media_manifest)} files) ---")
        for name, digest in sorted(data.media_manifest.items()):
            print(f"  {name:32} {digest[:16]}")
        print("=============================================")
        for finding in findings:
            print(finding)

    print(f"\naudit: {len(findings)} finding(s), {len(failures)} error(s)")
    return 1 if failures else 0


def decide_one(context: RequestContext, rates) -> tuple[planner.Decision, list[validation.GateFailure]]:
    """Run the deterministic core for one request and gate the result.

    This is the core routing function: every request takes exactly this path.
    Cash state -> recurrence -> forecast -> candidates -> gate. A row that fails
    the gate is replaced by the conservative fallback rather than published.
    """
    request, profile = context.request, context.profile

    cash = state.reconstruct(context.events, profile, request.request_date, rates)
    series = recurrence.detect(context.events, profile, request.request_date, rates)
    forecast = forecast_module.build(cash, series, request.request_date)

    decision = planner.choose(request, profile, forecast, series, context.payment_options)
    failures = validation.check(decision, request, profile, forecast,
                                context.payment_options, context.events)
    if failures:
        decision = validation.conservative_fallback(
            request, "failed quality gate: " + "; ".join(str(f) for f in failures)
        )
        # The fallback must itself pass, or we have a bug rather than a bad row.
        residual = validation.check(decision, request, profile, forecast)
        if residual:
            raise RuntimeError(
                f"{request.request_id}: conservative fallback failed validation: {residual}"
            )
    return decision, failures


def run_predictions(dataset_dir: Path, out_path: Path, *, mode: str,
                    limit: int | None, quiet: bool) -> int:
    if mode == "assisted":
        print("--mode assisted needs the evidence layer (M2); not implemented yet.",
              file=sys.stderr)
        return NOT_IMPLEMENTED

    data = load_dataset(dataset_dir)
    requests = list(data.requests)[:limit] if limit else list(data.requests)

    rows: list[dict] = []
    gate_failures: list[str] = []
    degraded: list[str] = []
    crashed: list[str] = []
    methods: dict[str, int] = {}

    for request in requests:
        try:
            context = data.context_for(request.request_id)
            decision, failures = decide_one(context, data.rates_by_key)
            if failures:
                gate_failures.append(f"{request.request_id}: {failures[0]}")
            if decision.degraded:
                degraded.append(f"{request.request_id}: {decision.degradation_reason}")
            row = to_row(decision, context.profile.home_currency, request.requested_amount,
                         context.profile.minimum_balance_to_keep)
        except Exception as exc:  # noqa: BLE001 - one row must not end the run
            crashed.append(f"{request.request_id}: {type(exc).__name__}: {exc}")
            decision = validation.conservative_fallback(
                request, f"unhandled error: {type(exc).__name__}")
            profile = data.profiles_by_user[request.user_id]
            row = to_row(decision, profile.home_currency, request.requested_amount,
                         profile.minimum_balance_to_keep)
        methods[decision.recommended_payment_method] = (
            methods.get(decision.recommended_payment_method, 0) + 1)
        rows.append(row)
        if not quiet:
            print(f"  {request.request_id:14} {decision.affordability_status:22} "
                  f"{decision.recommended_payment_method}")

    publish(rows, out_path, [r.request_id for r in requests], dataset_dir=dataset_dir)

    print(f"\nwrote {len(rows)} rows to {out_path}")
    print("method distribution: " + ", ".join(f"{k}={v}" for k, v in sorted(methods.items())))
    print(f"degraded rows      : {len(degraded)}")
    print(f"gate failures      : {len(gate_failures)}")
    print(f"unhandled errors   : {len(crashed)}")
    for label, items in (("GATE FAILURE", gate_failures), ("UNHANDLED", crashed)):
        for item in items[:10]:
            print(f"  {label}: {item}", file=sys.stderr)

    # A systematic programming error must not be presentable as a clean run.
    return 1 if crashed else 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    status = run_audit(args.dataset, quiet=args.quiet)
    if args.mode == "audit":
        return status
    if status != 0:
        print("refusing to predict on a dataset that failed its audit", file=sys.stderr)
        return status

    try:
        return run_predictions(args.dataset, args.out, mode=args.mode,
                               limit=args.limit, quiet=args.quiet)
    except (DataError, PublishError) as exc:
        print(f"RUN FAILED\n{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
