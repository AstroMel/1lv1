# Dealer Visit Priority

Ranks dealers for visits from **auction purchases** and **visit history** in an Excel workbook.
One fixed method, so every person gets the same answer. Nothing runs without the owner's permission.

## Nothing happens without permission

```
 AGENT OWNER (not the operator)              OPERATOR (runs the tool)
 holds the private key  owner.key            has only owner.pub
        │                                            │
        │ signs                                      │ asks to read / write
        ▼                                            ▼
  policy.json  ──────────────►  GATE  ◄──── every file access, no exceptions
  (grants + scoring rules)       │
                          allow / deny ─► audit.log.jsonl (tamper-evident)
```

- **Default is deny.** A new install has zero grants and cannot even open a file.
- **Only the owner can change permissions or the ranking rules.** The policy is signed; the
  operator holds only the public key, so an edited policy is rejected and everything locks.
- **Every grant names one action, a file pattern, and an expiry date** (max 365 days).
- **Sending, receiving, and editing source files do not exist in this version.** They cannot be
  granted. Enabling them needs a new owner-approved version of the tool.
- **Every allow and deny is logged.** If the log cannot be written, the action is denied.
- The input workbook is never modified, and never overwritten by the output.

## Owner (one time, then as needed)

```bash
python permissions.py keygen --key-out ~/owner.key --owner "Owner Name"   # keep owner.key private
python permissions.py grant --key ~/owner.key --action read_workbook --scope "dealers_*.xlsx" --days 30
python permissions.py grant --key ~/owner.key --action write_report  --scope "priority_*.xlsx" --days 30
python permissions.py revoke --key ~/owner.key --action read_workbook
python permissions.py set-scoring --key ~/owner.key --weights value=.4,decline=.25,lapse=.2,visit_gap=.15 --top-n 25
```
Set `DEALER_OWNER_PASSPHRASE` to encrypt the key file.

## Operator

```bash
python run.py status                                   # what is currently permitted
python run.py run dealers_2026.xlsx --out-dir out      # --as-of YYYY-MM-DD to pin the date
python permissions.py verify-audit                     # prove the log was not altered
```

## Workbook format

| Sheet | Columns (names are matched flexibly) |
|---|---|
| `Dealers` | dealer_id, dealer_name, region, last_visit |
| `Purchases` | dealer_id, purchase_date, amount |

Bad rows (unknown dealer, bad date, zero or negative amount, duplicates) are **listed on the
"Data checks" sheet and excluded**, never silently dropped.

## The score (0-100, higher = visit sooner)

| Part | Weight | Meaning |
|---|---|---|
| Value | 40% | Rank among all dealers by auction spend, last 24 months |
| Decline | 25% | Last 90 days vs the dealer's own usual 90-day pace |
| Lapse | 20% | Days since last purchase vs the dealer's own buying rhythm |
| Visit gap | 15% | Days since last visit vs a 90-day target |

Plain arithmetic, no AI judgment, no randomness. Same workbook + same date + same signed
rules = same ranking. Each row carries a plain-English "Why". The output file records the input
file's SHA-256, the owner, and the permissions used.

## Keep it private

`1lv1.com` is published from this repo. **Never commit real dealer data or `owner.key`**
(`.gitignore` blocks them here). Run this from a private repo or a private folder.
`sample/` holds synthetic data only. Try it with `python make_sample.py`.

Tests: `python -m unittest -q`. Requires `pandas`, `openpyxl`, `cryptography`.
