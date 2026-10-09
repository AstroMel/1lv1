"""Owner-controlled permission gate. Default: deny everything.

The agent owner holds a private signing key. The tool ships with only the matching
public key, so whoever *runs* the tool can verify permissions but can never create or
change them. Every allow/deny decision is written to a tamper-evident audit log.
"""
import argparse
import base64
import fnmatch
import getpass
import hashlib
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

HERE = Path(__file__).resolve().parent

# action -> (description, implemented in this version)
ACTIONS = {
    "read_workbook": ("Open and read an input Excel workbook", True),
    "write_report": ("Write a NEW ranked-list Excel file", True),
    "send_message": ("Send anything to anyone (email, text, chat)", False),
    "receive_message": ("Receive or read inbound messages", False),
    "edit_source_data": ("Change an existing dealer or purchase file", False),
}

DEFAULT_SCORING = {
    "weights": {"value": 0.40, "decline": 0.25, "lapse": 0.20, "visit_gap": 0.15},
    "visit_interval_days": 90,
    "default_buy_interval_days": 60,
    "top_n": 25,
}


class PermissionDenied(Exception):
    pass


def home_dir():
    return Path(os.environ.get("DEALER_PRIORITY_HOME", HERE))


def _canon(body):
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def fingerprint(pub):
    raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


def _scoring_problem(scoring):
    w = scoring.get("weights", {})
    if set(w) != set(DEFAULT_SCORING["weights"]):
        return "scoring weights must be exactly: " + ", ".join(DEFAULT_SCORING["weights"])
    if any(v < 0 for v in w.values()) or abs(sum(w.values()) - 1) > 1e-6:
        return "scoring weights must be non-negative and sum to 1"
    if scoring.get("top_n", 0) < 1 or scoring.get("visit_interval_days", 0) < 1:
        return "top_n and visit_interval_days must be positive"
    return None


def load_policy(home=None):
    """Return (body, fingerprint, problem). Any problem means nothing is allowed."""
    home = Path(home or home_dir())
    pub_path, pol_path = home / "owner.pub", home / "policy.json"
    if not pub_path.exists():
        return None, None, "No owner key installed (owner.pub missing)"
    pub = serialization.load_pem_public_key(pub_path.read_bytes())
    fp = fingerprint(pub)
    if not pol_path.exists():
        return None, fp, "No signed policy found (policy.json missing)"
    try:
        doc = json.loads(pol_path.read_text())
        pub.verify(base64.b64decode(doc["signature"]), _canon(doc["policy"]))
    except (InvalidSignature, KeyError, ValueError):
        return None, fp, "Policy signature invalid: file was changed or not signed by the owner"
    bad = _scoring_problem(doc["policy"].get("scoring", {}))
    if bad:
        return None, fp, "Policy rejected: " + bad
    return doc["policy"], fp, None


def _load_private(path):
    pw = os.environ.get("DEALER_OWNER_PASSPHRASE")
    return serialization.load_pem_private_key(
        Path(path).read_bytes(), password=pw.encode() if pw else None)


def _write_signed(home, body, priv):
    sig = base64.b64encode(priv.sign(_canon(body))).decode()
    tmp = Path(home) / "policy.json.tmp"
    tmp.write_text(json.dumps({"policy": body, "signature": sig}, indent=2))
    tmp.replace(Path(home) / "policy.json")


class Gate:
    """Every read, write, send, or edit must pass through require()."""

    def __init__(self, home=None, now=None):
        self.home = Path(home or home_dir())
        self.now = now or datetime.now(timezone.utc)
        self.policy, self.fingerprint, self.problem = load_policy(self.home)
        self.audit_path = self.home / "audit.log.jsonl"
        self.used = []

    def _decide(self, action, target):
        if self.problem:
            return False, self.problem
        if action not in ACTIONS:
            return False, "Unknown action"
        if not ACTIONS[action][1]:
            return False, "Action is not built into this tool and cannot be granted"
        name = os.path.basename(str(target))
        for g in self.policy["grants"]:
            if g["action"] == action and fnmatch.fnmatch(name, g["scope"]):
                if self.now.date() <= date.fromisoformat(g["expires"]):
                    return True, f"Granted by {self.policy['owner']} until {g['expires']}"
                return False, f"Grant expired on {g['expires']}"
        return False, "No grant from the owner covers this action and file"

    def require(self, action, target):
        ok, reason = self._decide(action, target)
        try:
            self._log(action, target, "allow" if ok else "deny", reason)
        except OSError as e:   # fail closed: an action that cannot be logged is not allowed
            ok, reason = False, f"Audit log unavailable ({e.__class__.__name__})"
        if not ok:
            raise PermissionDenied(f"DENIED {action} on {os.path.basename(str(target))}: {reason}")
        self.used.append((action, os.path.basename(str(target))))

    def _log(self, action, target, decision, reason):
        prev = "0" * 64
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        if self.audit_path.exists():
            lines = self.audit_path.read_text().strip().splitlines()
            if lines:
                prev = json.loads(lines[-1])["hash"]
        rec = {"ts": self.now.isoformat(), "actor": getpass.getuser(), "action": action,
               "target": os.path.basename(str(target)), "decision": decision,
               "reason": reason, "policy": self.fingerprint, "prev": prev}
        rec["hash"] = hashlib.sha256(_canon(rec)).hexdigest()
        with self.audit_path.open("a") as f:
            f.write(json.dumps(rec) + "\n")


def verify_audit(home=None):
    path = Path(home or home_dir()) / "audit.log.jsonl"
    prev, n = "0" * 64, 0
    if not path.exists():
        return True, 0
    for line in path.read_text().splitlines():
        rec = json.loads(line)
        h = rec.pop("hash")
        if rec["prev"] != prev or hashlib.sha256(_canon(rec)).hexdigest() != h:
            return False, n
        prev, n = h, n + 1
    return True, n


# ---------- owner commands (need the private key) ----------

def _owner_session(args):
    home = Path(args.home or home_dir())
    body, fp, problem = load_policy(home)
    if problem:
        sys.exit(problem)
    priv = _load_private(args.key)
    if fingerprint(priv.public_key()) != fp:
        sys.exit("This key is not the owner key installed for this tool.")
    return home, body, priv


def cmd_keygen(args):
    home = Path(args.home or home_dir())
    home.mkdir(parents=True, exist_ok=True)
    if (home / "owner.pub").exists():
        sys.exit("owner.pub already exists; refusing to replace the owner.")
    priv = Ed25519PrivateKey.generate()
    pw = os.environ.get("DEALER_OWNER_PASSPHRASE")
    enc = serialization.BestAvailableEncryption(pw.encode()) if pw else serialization.NoEncryption()
    key_path = Path(args.key_out)
    key_path.write_bytes(priv.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc))
    key_path.chmod(0o600)
    (home / "owner.pub").write_bytes(priv.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    body = {"version": 1, "owner": args.owner, "issued": date.today().isoformat(),
            "grants": [], "scoring": DEFAULT_SCORING}
    _write_signed(home, body, priv)
    print(f"Owner key written to {key_path}. Only the owner keeps this file. Never share or commit it.")
    print(f"Fingerprint {fingerprint(priv.public_key())}. Policy starts with ZERO grants.")


def cmd_grant(args):
    home, body, priv = _owner_session(args)
    if args.action not in ACTIONS or not ACTIONS[args.action][1]:
        sys.exit(f"'{args.action}' cannot be granted. Grantable: "
                 + ", ".join(a for a, v in ACTIONS.items() if v[1]))
    if not 1 <= args.days <= 365:
        sys.exit("--days must be between 1 and 365; permissions always expire.")
    expires = (datetime.now(timezone.utc).date() + timedelta(days=args.days)).isoformat()
    body["grants"] = [g for g in body["grants"]
                      if (g["action"], g["scope"]) != (args.action, args.scope)]
    body["grants"].append({"action": args.action, "scope": args.scope, "expires": expires,
                           "note": args.note or ""})
    _write_signed(home, body, priv)
    print(f"Granted {args.action} on '{args.scope}' until {expires}.")


def cmd_revoke(args):
    home, body, priv = _owner_session(args)
    before = len(body["grants"])
    body["grants"] = [g for g in body["grants"] if not (
        g["action"] == args.action and (args.scope is None or g["scope"] == args.scope))]
    _write_signed(home, body, priv)
    print(f"Revoked {before - len(body['grants'])} grant(s).")


def cmd_set_scoring(args):
    home, body, priv = _owner_session(args)
    s = json.loads(json.dumps(body["scoring"]))
    if args.weights:
        s["weights"] = {k: float(v) for k, v in (p.split("=") for p in args.weights.split(","))}
    for f in ("visit_interval_days", "default_buy_interval_days", "top_n"):
        if getattr(args, f) is not None:
            s[f] = getattr(args, f)
    bad = _scoring_problem(s)
    if bad:
        sys.exit(bad)
    body["scoring"] = s
    _write_signed(home, body, priv)
    print("Scoring rules updated and re-signed.")


def cmd_show(args):
    body, fp, problem = load_policy(args.home)
    print(f"Owner key fingerprint: {fp}")
    if problem:
        print(f"STATUS: LOCKED. {problem}. Nothing is permitted.")
        return
    print(f"Owner: {body['owner']}  (policy issued {body['issued']})")
    print("Permissions (everything not listed is denied):")
    for g in body["grants"] or []:
        print(f"  ALLOW {g['action']:<18} files '{g['scope']}'  until {g['expires']}  {g['note']}")
    if not body["grants"]:
        print("  (none)")
    print("Never available in this version: " + ", ".join(
        a for a, v in ACTIONS.items() if not v[1]))
    print("Scoring rules:", json.dumps(body["scoring"]))


def cmd_verify_audit(args):
    ok, n = verify_audit(args.home)
    print(f"Audit log intact ({n} entries)." if ok else f"AUDIT LOG TAMPERED near entry {n}.")
    sys.exit(0 if ok else 1)


def main(argv=None):
    p = argparse.ArgumentParser(description="Owner controls for the dealer prioritizer")
    p.add_argument("--home", help="folder with policy.json, owner.pub, audit.log.jsonl")
    sub = p.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen", help="OWNER, one time: create the owner key")
    k.add_argument("--key-out", required=True)
    k.add_argument("--owner", required=True, help="owner's name, shown on every report")
    k.set_defaults(fn=cmd_keygen)
    g = sub.add_parser("grant", help="OWNER: allow one action on matching files, with expiry")
    g.add_argument("--key", required=True)
    g.add_argument("--action", required=True)
    g.add_argument("--scope", required=True, help="file name or pattern, e.g. dealers_*.xlsx")
    g.add_argument("--days", type=int, required=True)
    g.add_argument("--note")
    g.set_defaults(fn=cmd_grant)
    r = sub.add_parser("revoke", help="OWNER: withdraw permission")
    r.add_argument("--key", required=True)
    r.add_argument("--action", required=True)
    r.add_argument("--scope")
    r.set_defaults(fn=cmd_revoke)
    s = sub.add_parser("set-scoring", help="OWNER: change the ranking rules for everyone")
    s.add_argument("--key", required=True)
    s.add_argument("--weights", help="value=.4,decline=.25,lapse=.2,visit_gap=.15")
    s.add_argument("--visit-interval-days", dest="visit_interval_days", type=int)
    s.add_argument("--default-buy-interval-days", dest="default_buy_interval_days", type=int)
    s.add_argument("--top-n", dest="top_n", type=int)
    s.set_defaults(fn=cmd_set_scoring)
    sub.add_parser("show", help="anyone: what is currently permitted").set_defaults(fn=cmd_show)
    sub.add_parser("verify-audit", help="anyone: check the log was not altered").set_defaults(
        fn=cmd_verify_audit)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
