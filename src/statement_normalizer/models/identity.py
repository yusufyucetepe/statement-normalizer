from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable

from statement_normalizer.models.schemas import Transaction

#: Which payload shape produced a key. Hashed in as the *first* element, so a key
#: built from a description can never collide with one built from an external id,
#: and the two are never silently compared as equal.
#:
#: These are not a version sequence to migrate between: both shapes are live at
#: once, chosen per transaction by whether the institution published an id.
#: `v1` is frozen — every `dedupe_key` already in the database was built with it,
#: and changing its payload by so much as a separator silently invalidates all of
#: them, after which overlapping uploads double-count until every statement is
#: re-uploaded. `test_identity.py` pins it to a literal digest for that reason.
DESCRIPTION_FINGERPRINT = "v1"
EXTERNAL_ID_FINGERPRINT = "v2"

_SEPARATOR = "|"


def _fingerprint(txn: Transaction, account_ref: str) -> str:
    """The parts of a transaction that identify it across two exports.

    The last element is what the institution gives us to recognize the
    transaction by. When it publishes its own id we use that and drop the
    description entirely: a narrative is the part of a row an institution feels
    free to reword between exports, and a reworded row is stored twice today.

    The id replaces the description rather than joining it, but it does *not*
    replace the rest — date, direction, amount and currency stay in the payload.
    That is deliberate and load-bearing: an institution's id need not be unique
    per row. Wise gives a transfer and the fee charged for it the same
    `TransferWise ID`, and keying on the id alone would merge the fee into the
    transfer and lose it.

    Deliberately excludes `balance_after` and `raw_row`: a running balance
    differs between statements that start at different points in the ledger, and
    `raw_row` carries per-export noise like column order.
    """
    if txn.external_id:
        marker, identity = EXTERNAL_ID_FINGERPRINT, txn.external_id
    else:
        # `Transaction` already collapses whitespace on the way in.
        marker, identity = DESCRIPTION_FINGERPRINT, txn.description.lower()
    return _SEPARATOR.join(
        (
            marker,
            txn.source_institution,
            account_ref,
            txn.date.isoformat(),
            txn.direction.value,
            # Fixed exponent so Decimal("10.5") and Decimal("10.50") agree; 4dp
            # matches the NUMERIC(20, 4) the value is stored in.
            f"{txn.amount:.4f}",
            txn.currency,
            identity,
        )
    )


def dedupe_key(txn: Transaction, *, account_ref: str | None, occurrence: int) -> str | None:
    """Stable identity for one transaction, or None if it must not be deduplicated.

    `occurrence` distinguishes genuinely repeated transactions — two identical
    coffees on the same day are two real rows, and collapsing them would be data
    loss. See `assign_dedupe_keys`, which is what callers should use.

    Returns None when the statement carries no account reference. Without one,
    two different accounts at the same institution could share a fingerprint,
    and merging them would attribute someone else's money to this account. An
    external id does not lift that requirement: institutions promise their ids
    are unique within an account, not across every account they hold.
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
