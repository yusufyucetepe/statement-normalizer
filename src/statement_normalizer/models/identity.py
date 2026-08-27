from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable

from statement_normalizer.models.schemas import Transaction

#: Bumped when the fingerprint payload changes. It is hashed *into* the key, so
#: keys from two versions can never collide or silently compare equal.
FINGERPRINT_VERSION = "v1"

_SEPARATOR = "|"


def _fingerprint(txn: Transaction, account_ref: str) -> str:
    """The parts of a transaction that identify it across two exports.

    Deliberately excludes `balance_after` and `raw_row`: a running balance
    differs between statements that start at different points in the ledger, and
    `raw_row` carries per-export noise like column order.
    """
    return _SEPARATOR.join(
        (
            FINGERPRINT_VERSION,
            txn.source_institution,
            account_ref,
            txn.date.isoformat(),
            txn.direction.value,
            # Fixed exponent so Decimal("10.5") and Decimal("10.50") agree; 4dp
            # matches the NUMERIC(20, 4) the value is stored in.
            f"{txn.amount:.4f}",
            txn.currency,
            # `Transaction` already collapses whitespace on the way in.
            txn.description.lower(),
        )
    )


def dedupe_key(txn: Transaction, *, account_ref: str | None, occurrence: int) -> str | None:
    """Stable identity for one transaction, or None if it must not be deduplicated.

    `occurrence` distinguishes genuinely repeated transactions — two identical
    coffees on the same day are two real rows, and collapsing them would be data
    loss. See `assign_dedupe_keys`, which is what callers should use.

    Returns None when the statement carries no account reference. Without one,
    two different accounts at the same institution could share a fingerprint,
    and merging them would attribute someone else's money to this account.
    """
    if not account_ref:
        return None
    payload = f"{_fingerprint(txn, account_ref)}{_SEPARATOR}{occurrence}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def assign_dedupe_keys(
    transactions: Iterable[Transaction], account_ref: str | None
) -> list[str | None]:
    """Dedupe keys for one statement's transactions, in statement order.

    Repeats of the same fingerprint are numbered 0, 1, 2… within the statement.
    An overlapping statement containing the same repeats numbers them the same
    way and therefore matches all of them, while a statement that happens to
    contain one extra repeat matches the ones it shares and stores the extra.
    """
    if not account_ref:
        return [None for _ in transactions]

    seen: Counter[str] = Counter()
    keys: list[str | None] = []
    for txn in transactions:
        fingerprint = _fingerprint(txn, account_ref)
        payload = f"{fingerprint}{_SEPARATOR}{seen[fingerprint]}"
        seen[fingerprint] += 1
        keys.append(hashlib.sha256(payload.encode("utf-8")).hexdigest())
    return keys
