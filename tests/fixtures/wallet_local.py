"""Local test wallet provider — stores all state in the database.

This provider is ONLY available in non-production environments (dev,
development, local, test).  It uses the ``pending_wallet_transfers``
table for transactions and a simple counter-based wallet ID scheme so
that every operation round-trips through the real ORM models without
requiring any external service (no Stripe, no BLNK).

Balances are computed on the fly from committed transfers.
"""

from __future__ import annotations

import logging
import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from app.config import is_test_wallet_environment, settings
from app.services.wallet_provider import (
    WalletBalance,
    WalletProvider,
    WalletProviderError,
    WalletTransaction,
)

logger = logging.getLogger(__name__)

_PROVIDER_NAME = "local"
_SRC_TYPE = "local_wallet"
_DST_TYPE = "local_wallet"
_REF_TYPE = "local"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_non_production() -> None:
    if not is_test_wallet_environment(settings.environment, settings.stripe_secret_key):
        raise WalletProviderError(
            "The local test wallet is not available in "
            "production environments",
            status_code=403,
        )


def _get_db():
    from app.db import SessionLocal

    return SessionLocal()


def _wallet_id_to_int(wallet_id: str) -> int:
    """Extract a stable numeric ID for destination_id from a wallet string."""
    if wallet_id.startswith("local_user_"):
        try:
            return int(wallet_id.rsplit("_", 1)[1])
        except ValueError:
            pass
    if wallet_id.startswith("local_biz_"):
        try:
            return 1_000_000_000 + int(wallet_id.rsplit("_", 1)[1])
        except ValueError:
            pass
    if wallet_id == "local_system_payin":
        return 1_900_000_001
    if wallet_id == "local_system_payout":
        return 1_900_000_002
    digest = hashlib.sha1(wallet_id.encode("utf-8")).digest()
    return 1_500_000_000 + int.from_bytes(digest[:4], "big") % 400_000_000


def _txn_to_wallet_transaction(row) -> WalletTransaction:
    return WalletTransaction(
        transaction_id=str(row.id),
        amount=int(row.amount),
        currency=row.currency or "AUD",
        status=row.status,
        source=row.source_id or "",
        destination=str(row.destination_id),
        meta_data=row.meta_data or {},
        created_at=(
            row.created_at.isoformat() if row.created_at else None
        ),
        raw={
            "id": row.id,
            "reference_type": row.reference_type,
            "reference_id": row.reference_id,
        },
    )


class LocalWalletProvider(WalletProvider):
    """In-database wallet provider for local testing only."""

    def is_configured(self) -> bool:
        return is_test_wallet_environment(settings.environment, settings.stripe_secret_key)

    # ── provisioning ────────────────────────────────────────

    def provision_user_wallet(
        self,
        user_id: int,
        email: str,
        name: str,
        *,
        currency: str = "AUD",
    ) -> str:
        _require_non_production()
        return f"local_user_{user_id}"

    def provision_business_wallet(
        self,
        business_id: int,
        business_name: str,
        owner_email: str,
        *,
        currency: str = "AUD",
    ) -> str:
        _require_non_production()
        return f"local_biz_{business_id}"

    # ── balance ─────────────────────────────────────────────

    def get_balance(
        self, wallet_id: str, *, with_queued: bool = False,
    ) -> WalletBalance:
        _require_non_production()
        from sqlalchemy import or_

        from app.models import PendingWalletTransfer

        db = _get_db()
        try:
            wid_int = _wallet_id_to_int(wallet_id)
            committed_outgoing = (
                db.query(PendingWalletTransfer)
                .filter(
                    PendingWalletTransfer.reference_type
                    == _REF_TYPE,
                    PendingWalletTransfer.status == "committed",
                    or_(
                        PendingWalletTransfer.source_id
                        == wallet_id,
                        PendingWalletTransfer.source_id
                        == str(wid_int),
                    ),
                )
                .all()
            )
            committed_incoming = (
                db.query(PendingWalletTransfer)
                .filter(
                    PendingWalletTransfer.reference_type
                    == _REF_TYPE,
                    PendingWalletTransfer.status == "committed",
                    PendingWalletTransfer.destination_id == wid_int,
                )
                .all()
            )

            balance = sum(int(t.amount) for t in committed_incoming) - sum(
                int(t.amount) for t in committed_outgoing
            )

            pending_in = 0
            pending_out = 0
            if with_queued:
                pending_outgoing = (
                    db.query(PendingWalletTransfer)
                    .filter(
                        PendingWalletTransfer.reference_type
                        == _REF_TYPE,
                        PendingWalletTransfer.status == "pending",
                        or_(
                            PendingWalletTransfer.source_id == wallet_id,
                            PendingWalletTransfer.source_id == str(wid_int),
                        ),
                    )
                    .all()
                )
                pending_incoming = (
                    db.query(PendingWalletTransfer)
                    .filter(
                        PendingWalletTransfer.reference_type
                        == _REF_TYPE,
                        PendingWalletTransfer.status == "pending",
                        PendingWalletTransfer.destination_id == wid_int,
                    )
                    .all()
                )
                pending_in = sum(int(t.amount) for t in pending_incoming)
                pending_out = sum(int(t.amount) for t in pending_outgoing)

            return WalletBalance(
                wallet_id=wallet_id,
                currency="AUD",
                balance=balance,
                available_balance=balance,
                pending_in=pending_in,
                pending_out=pending_out,
            )
        finally:
            db.close()

    # ── transactions ────────────────────────────────────────

    def get_transactions(
        self, wallet_id: str, *, limit: int = 50,
    ) -> list[WalletTransaction]:
        _require_non_production()
        from sqlalchemy import or_

        from app.models import PendingWalletTransfer

        wid_int = _wallet_id_to_int(wallet_id)
        db = _get_db()
        try:
            rows = (
                db.query(PendingWalletTransfer)
                .filter(
                    PendingWalletTransfer.reference_type
                    == _REF_TYPE,
                    or_(
                        PendingWalletTransfer.source_id
                        == wallet_id,
                        PendingWalletTransfer.destination_id
                        == wid_int,
                    ),
                )
                .order_by(
                    PendingWalletTransfer.created_at.desc(),
                )
                .limit(limit)
                .all()
            )
            return [_txn_to_wallet_transaction(r) for r in rows]
        finally:
            db.close()

    def get_transaction(
        self, transaction_id: str,
    ) -> WalletTransaction:
        _require_non_production()
        from app.models import PendingWalletTransfer

        db = _get_db()
        try:
            row = db.get(
                PendingWalletTransfer, int(transaction_id),
            )
            if row is None or row.reference_type != _REF_TYPE:
                raise WalletProviderError(
                    f"Transaction {transaction_id} not found"
                )
            return _txn_to_wallet_transaction(row)
        finally:
            db.close()

    # ── inflight lifecycle ──────────────────────────────────

    def create_inflight(
        self,
        *,
        source: str,
        destination: str,
        amount: int,
        currency: str = "AUD",
        meta_data: dict[str, Any] | None = None,
        reference: str = "",
        description: str = "",
    ) -> WalletTransaction:
        _require_non_production()
        from app.models import PendingWalletTransfer

        db = _get_db()
        try:
            row = PendingWalletTransfer(
                source_type=_SRC_TYPE,
                source_id=source,
                destination_type=_DST_TYPE,
                destination_id=_wallet_id_to_int(destination),
                amount=amount,
                currency=currency,
                status="pending",
                reference_type=_REF_TYPE,
                reference_id=reference or str(uuid.uuid4()),
                meta_data=meta_data or {},
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return _txn_to_wallet_transaction(row)
        finally:
            db.close()

    def create_payout(
        self,
        *,
        wallet_id: str,
        amount: int,
        currency: str = "AUD",
        description: str = "",
        meta_data: dict[str, Any] | None = None,
    ) -> WalletTransaction:
        _require_non_production()
        return self.create_inflight(
            source=wallet_id,
            destination="local_payout_sink",
            amount=amount,
            currency=currency,
            description=description or "Local test payout",
            meta_data=meta_data,
            reference=f"payout_{wallet_id}_{uuid.uuid4().hex[:8]}",
        )

    def commit_inflight(
        self, transaction_id: str,
    ) -> WalletTransaction:
        _require_non_production()
        from app.models import PendingWalletTransfer

        db = _get_db()
        try:
            row = db.get(
                PendingWalletTransfer, int(transaction_id),
            )
            if row is None or row.reference_type != _REF_TYPE:
                raise WalletProviderError(
                    f"Transaction {transaction_id} not found"
                )
            if row.status != "pending":
                raise WalletProviderError(
                    f"Cannot commit {row.status} transaction"
                )
            row.status = "committed"
            row.committed_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(row)
            return _txn_to_wallet_transaction(row)
        finally:
            db.close()

    def void_inflight(
        self, transaction_id: str,
    ) -> WalletTransaction:
        _require_non_production()
        from app.models import PendingWalletTransfer

        db = _get_db()
        try:
            row = db.get(
                PendingWalletTransfer, int(transaction_id),
            )
            if row is None or row.reference_type != _REF_TYPE:
                raise WalletProviderError(
                    f"Transaction {transaction_id} not found"
                )
            if row.status != "pending":
                raise WalletProviderError(
                    f"Cannot void {row.status} transaction"
                )
            row.status = "voided"
            row.voided_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(row)
            return _txn_to_wallet_transaction(row)
        finally:
            db.close()

    # ── immediate transfer ──────────────────────────────────

    def create_transfer(
        self,
        *,
        source: str,
        destination: str,
        amount: int,
        currency: str = "AUD",
        meta_data: dict[str, Any] | None = None,
        reference: str = "",
        description: str = "",
    ) -> WalletTransaction:
        _require_non_production()
        from app.models import PendingWalletTransfer

        db = _get_db()
        try:
            row = PendingWalletTransfer(
                source_type=_SRC_TYPE,
                source_id=source,
                destination_type=_DST_TYPE,
                destination_id=_wallet_id_to_int(destination),
                amount=amount,
                currency=currency,
                status="committed",
                reference_type=_REF_TYPE,
                reference_id=reference or str(uuid.uuid4()),
                meta_data=meta_data or {},
                committed_at=datetime.now(timezone.utc),
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return _txn_to_wallet_transaction(row)
        finally:
            db.close()
