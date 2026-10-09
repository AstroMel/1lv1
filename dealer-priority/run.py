"""Operator entry point. Every file access is gated by the owner's signed permissions."""
import argparse
import sys
from datetime import date
from pathlib import Path

import pandas as pd

import report
import scoring
from permissions import Gate, PermissionDenied


def run(workbook, out_dir, as_of=None, home=None):
    gate = Gate(home)
    workbook, out_dir = Path(workbook), Path(out_dir)
    as_of = pd.Timestamp(as_of or date.today()).normalize()
    out_path = out_dir / f"priority_{as_of.date().isoformat()}.xlsx"
    if out_path.resolve() == workbook.resolve():
        raise PermissionDenied("Refusing to overwrite the input workbook")
    gate.require("read_workbook", workbook)
    gate.require("write_report", out_path)   # check both before doing any work
    sheets = pd.read_excel(workbook, sheet_name=None, engine="openpyxl")
    ranked, issues = scoring.compute(sheets, as_of, gate.policy["scoring"])
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {"as_of": as_of.date().isoformat(), "input_name": workbook.name,
            "input_sha256": report.sha256(workbook), "owner": gate.policy["owner"],
            "fingerprint": gate.fingerprint, "used": gate.used, "scoring": gate.policy["scoring"]}
    report.write(out_path, ranked, issues, meta)
    return out_path, ranked, issues


def main(argv=None):
    p = argparse.ArgumentParser(description="Rank dealers for visits from auction purchases")
    p.add_argument("--home")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("workbook")
    r.add_argument("--out-dir", default="out")
    r.add_argument("--as-of", help="YYYY-MM-DD (default: today)")
    sub.add_parser("status")
    args = p.parse_args(argv)
    if args.cmd == "status":
        import permissions
        permissions.cmd_show(argparse.Namespace(home=args.home))
        return 0
    try:
        out, ranked, issues = run(args.workbook, args.out_dir, args.as_of, args.home)
    except PermissionDenied as e:
        print(e, file=sys.stderr)
        return 2
    except scoring.InputError as e:
        print(f"Input problem: {e}", file=sys.stderr)
        return 3
    print(f"Wrote {out}  ({len(ranked)} dealers ranked, {len(issues)} data problems flagged)")
    print(ranked.head(10)[["rank", "dealer_id", "dealer_name", "priority", "why"]]
          .to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
