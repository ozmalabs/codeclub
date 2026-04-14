from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import stripe
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import is_test_wallet_environment, settings
from app.db import SessionLocal
from app.models import PendingWalletTransfer
from app.services.stripe_connect import StripeServiceError, _attr, create_connected_account
from app.services.wallet_provider import (
    WalletBalance,
    WalletProvider,
    WalletProviderError,
    WalletTransaction,
)


class StripeWalletProvider(WalletProvider):
    """Stripe wallet adapter.

    Stripe transfers in this adapter are intentionally platform-funded:
    ``source`` is preserved for ReviewPay bookkeeping, but Stripe only moves
    funds from the platform balance to ``destination``. User withdrawals use
    :meth:`create_payout`, which executes against the connected account itself.
    """

    @staticmethod
    def _request_options(stripe_account: str | None = None) -> dict[str, Any]:
        options: dict[str, Any] = {"api_key": settings.stripe_secret_key}
        if stripe_account:
            options["stripe_account"] = stripe_account
        return options

    @classmethod
    def _json_safe(cls, value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if hasattr(value, "to_dict_recursive"):
            return cls._json_safe(value.to_dict_recursive())
        if hasattr(value, "to_dict"):
            return cls._json_safe(value.to_dict())
        if isinstance(value, dict):
            return {str(key): cls._json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [cls._json_safe(item) for item in value]
        return str(value)

    @classmethod
    def _serialize(cls, payload: Any) -> dict[str, Any]:
        serialized = cls._json_safe(payload)
        if isinstance(serialized, dict):
            return serialized
        return {"value": serialized}

    @staticmethod
    def _wallet_error(exc: Exception) -> WalletProviderError:
        if isinstance(exc, StripeServiceError):
            return WalletProviderError(str(exc), payload=exc.payload)
        if isinstance(exc, stripe.error.StripeError):
            return WalletProviderError(
                str(exc.user_message or exc),
                payload={"type": exc.__class__.__name__, "message": str(exc)},
            )
        return WalletProviderError(str(exc))

    @staticmethod
    def _created_at(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.astimezone(UTC).isoformat()
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=UTC).isoformat()
        return str(value)

    @staticmethod
    def _currency_candidates(payload: dict[str, Any]) -> list[str]:
        currencies: list[str] = []
        for section_name in ("available", "pending"):
            for row in payload.get(section_name) or []:
                currency = str(row.get("currency") or "").upper()
                if currency and currency not in currencies:
                    currencies.append(currency)
        return currencies

    @staticmethod
    def _pick_currency(payload: dict[str, Any]) -> str:
        currencies = StripeWalletProvider._currency_candidates(payload)
        if "AUD" in currencies:
            return "AUD"
        return currencies[0] if currencies else "AUD"

    @staticmethod
    def _sum_for_currency(rows: list[dict[str, Any]], currency: str) -> int:
        total = 0
        for row in rows:
            if str(row.get("currency") or "").upper() == currency.upper():
                total += int(row.get("amount") or 0)
        return total

    @classmethod
    def _balance_components(cls, payload: dict[str, Any], currency: str) -> tuple[int, int, int, int]:
        available_balance = cls._sum_for_currency(payload.get("available") or [], currency)
        instant_available_balance = cls._sum_for_currency(payload.get("instant_available") or [], currency)
        pending_rows = [
            row
            for row in (payload.get("pending") or [])
            if str(row.get("currency") or "").upper() == currency
        ]
        pending_in = sum(max(int(row.get("amount") or 0), 0) for row in pending_rows)
        pending_out = sum(abs(min(int(row.get("amount") or 0), 0)) for row in pending_rows)
        if is_test_wallet_environment(settings.environment, settings.stripe_secret_key) and instant_available_balance > available_balance:
            uplift = instant_available_balance - available_balance
            available_balance = instant_available_balance
            pending_in = max(0, pending_in - uplift)
        return available_balance, instant_available_balance, pending_in, pending_out

    @staticmethod
    def _pending_destination_id(meta_data: dict[str, Any] | None) -> int:
        if not isinstance(meta_data, dict):
            return 0
        for key in ("destination_id", "business_id", "user_id", "recipient_id"):
            value = meta_data.get(key)
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return 0

    @staticmethod
    def _stripe_idempotency_key(*parts: Any) -> str | None:
        values = [str(part).strip() for part in parts if str(part or "").strip()]
        if not values:
            return None
        return ":".join(values)

    @staticmethod
    def _transaction_status(payload: dict[str, Any], *, default: str = "pending") -> str:
        status = str(payload.get("status") or "").strip().lower()
        if status:
            if status in {"available", "paid", "succeeded", "success"}:
                return "committed"
            if status in {"canceled", "cancelled", "voided", "reversed"}:
                return "voided"
            return status

        object_type = str(payload.get("object") or "").strip().lower()
        if object_type == "transfer":
            return "committed"
        return default

    @classmethod
    def _map_transaction(
        cls,
        payload: Any,
        *,
        default_status: str = "pending",
        source: str = "",
        destination: str = "",
    ) -> WalletTransaction:
        raw = cls._serialize(payload)
        meta_data = raw.get("metadata")
        if not isinstance(meta_data, dict):
            meta_data = raw.get("meta_data")
        if not isinstance(meta_data, dict):
            meta_data = {}

        transaction_source = str(
            raw.get("source")
            or raw.get("source_transaction")
            or source
            or ""
        )
        transaction_destination = str(raw.get("destination") or destination or "")

        return WalletTransaction(
            transaction_id=str(raw.get("id") or raw.get("transaction_id") or ""),
            amount=int(raw.get("amount") or 0),
            currency=str(raw.get("currency") or "AUD").upper(),
            status=cls._transaction_status(raw, default=default_status),
            source=transaction_source,
            destination=transaction_destination,
            meta_data=dict(meta_data),
            created_at=cls._created_at(raw.get("created")),
            raw=raw,
        )

    @classmethod
    def _map_pending_transfer(cls, row: PendingWalletTransfer) -> WalletTransaction:
        return WalletTransaction(
            transaction_id=str(row.id),
            amount=int(row.amount),
            currency=str(row.currency or "AUD").upper(),
            status=str(row.status or "pending"),
            source=str(row.source_id or ""),
            destination=str(row.destination_stripe_account or ""),
            meta_data=dict(row.meta_data or {}),
            created_at=cls._created_at(row.created_at),
            raw={
                "id": row.id,
                "pending_transfer_id": row.id,
                "reference_type": row.reference_type,
                "reference_id": row.reference_id,
                "stripe_transfer_id": row.stripe_transfer_id,
            },
        )

    @staticmethod
    def _sort_transactions(
        transactions: list[WalletTransaction], *, limit: int
    ) -> list[WalletTransaction]:
        return sorted(
            transactions,
            key=lambda row: row.created_at or "",
            reverse=True,
        )[:limit]

    @staticmethod
    def _list_rows(payload: Any) -> list[Any]:
        if isinstance(payload, dict):
            rows = payload.get("data")
        else:
            rows = getattr(payload, "data", None)
        return list(rows or [])

    @staticmethod
    def _pending_transfers(wallet_id: str, *, status: str = "pending") -> list[PendingWalletTransfer]:
        db = SessionLocal()
        try:
            return (
                db.query(PendingWalletTransfer)
                .filter(
                    PendingWalletTransfer.destination_type == "stripe_account",
                    PendingWalletTransfer.status == status,
                    or_(
                        PendingWalletTransfer.source_id == wallet_id,
                        PendingWalletTransfer.destination_stripe_account == wallet_id,
                    ),
                )
                .order_by(PendingWalletTransfer.created_at.desc())
                .all()
            )
        finally:
            db.close()

    @staticmethod
    def _committed_inbound_transfers(wallet_id: str) -> list[PendingWalletTransfer]:
        db = SessionLocal()
        try:
            return (
                db.query(PendingWalletTransfer)
                .filter(
                    PendingWalletTransfer.destination_type == "stripe_account",
                    PendingWalletTransfer.status == "committed",
                    PendingWalletTransfer.destination_stripe_account == wallet_id,
                    PendingWalletTransfer.stripe_transfer_id.isnot(None),
                )
                .order_by(
                    PendingWalletTransfer.committed_at.desc(),
                    PendingWalletTransfer.id.desc(),
                )
                .all()
            )
        finally:
            db.close()

    def _collect_platform_fee_via_transfer_reversals(
        self,
        *,
        wallet_id: str,
        amount: int,
        currency: str,
        payout_meta: dict[str, Any],
        base_idempotency_key: str | None,
    ) -> tuple[list[dict[str, Any]], int]:
        remaining_fee = amount
        reversals: list[dict[str, Any]] = []
        candidates = self._committed_inbound_transfers(wallet_id)

        for pending in candidates:
            if remaining_fee <= 0:
                break
            transfer_id = str(pending.stripe_transfer_id or "").strip()
            if not transfer_id:
                continue
            try:
                transfer = stripe.Transfer.retrieve(transfer_id, **self._request_options())
            except stripe.error.StripeError as exc:
                raise self._wallet_error(exc) from exc

            transfer_amount = int(transfer.get("amount") or 0)
            amount_reversed = int(transfer.get("amount_reversed") or 0)
            unreversed_amount = max(0, transfer_amount - amount_reversed)
            if unreversed_amount <= 0:
                continue
            if str(transfer.get("currency") or "").upper() != currency.upper():
                continue

            reversal_amount = min(remaining_fee, unreversed_amount)
            reversal_meta = {
                **payout_meta,
                "flow": "customer_withdrawal_platform_fee",
                "reviewpay_transaction_type": "wallet_payout_fee_reversal",
                "reviewpay_platform_fee_wallet_id": wallet_id,
                "reviewpay_platform_fee_source_transfer_id": transfer_id,
                "reviewpay_platform_fee_pending_transfer_id": pending.id,
            }
            reversal_idempotency_key = self._stripe_idempotency_key(
                base_idempotency_key,
                "platform-fee-reversal",
                transfer_id,
            )
            reversal_params: dict[str, Any] = {
                "amount": reversal_amount,
                "description": payout_meta.get("reviewpay_platform_fee_description") or "ReviewPay payout fee",
                "metadata": reversal_meta,
                **self._request_options(),
            }
            if reversal_idempotency_key:
                reversal_params["idempotency_key"] = reversal_idempotency_key
            try:
                reversal = stripe.Transfer.create_reversal(transfer_id, **reversal_params)
            except stripe.error.StripeError as exc:
                raise self._wallet_error(exc) from exc

            reversal_row = self._serialize(reversal)
            reversals.append(
                {
                    "transfer_id": transfer_id,
                    "reversal_id": str(reversal_row.get("id") or ""),
                    "amount": reversal_amount,
                    "pending_transfer_id": pending.id,
                }
            )
            remaining_fee -= reversal_amount

        if remaining_fee > 0:
            raise WalletProviderError(
                "Unable to collect the ReviewPay fee from prior Stripe wallet funding transfers",
                payload={
                    "wallet_id": wallet_id,
                    "currency": currency.upper(),
                    "required_fee_amount": amount,
                    "collected_fee_amount": amount - remaining_fee,
                },
            )

        return reversals, amount

    def _restore_platform_fee_reversals(
        self,
        *,
        wallet_id: str,
        currency: str,
        payout_meta: dict[str, Any],
        reversals: list[dict[str, Any]],
        base_idempotency_key: str | None,
    ) -> list[dict[str, Any]]:
        restored: list[dict[str, Any]] = []
        for reversal in reversals:
            reversal_id = str(reversal.get("reversal_id") or "").strip()
            reversal_amount = int(reversal.get("amount") or 0)
            if not reversal_id or reversal_amount <= 0:
                continue
            restore_meta = {
                **payout_meta,
                "flow": "customer_withdrawal_platform_fee_restore",
                "reviewpay_transaction_type": "wallet_payout_fee_restore",
                "reviewpay_platform_fee_reversal_id": reversal_id,
                "reviewpay_platform_fee_wallet_id": wallet_id,
            }
            restore_idempotency_key = self._stripe_idempotency_key(
                base_idempotency_key,
                "platform-fee-restore",
                reversal_id,
            )
            restore_params: dict[str, Any] = {
                "amount": reversal_amount,
                "currency": currency.lower(),
                "destination": wallet_id,
                "description": "Restore ReviewPay payout fee after payout failure",
                "metadata": restore_meta,
                **self._request_options(),
            }
            if restore_idempotency_key:
                restore_params["idempotency_key"] = restore_idempotency_key
            try:
                transfer = stripe.Transfer.create(**restore_params)
            except stripe.error.StripeError as exc:
                raise self._wallet_error(exc) from exc
            transfer_row = self._serialize(transfer)
            restored.append(
                {
                    "reversal_id": reversal_id,
                    "restoration_transfer_id": str(transfer_row.get("id") or ""),
                    "amount": reversal_amount,
                }
            )
        return restored

    def collect_platform_fee(
        self,
        *,
        wallet_id: str,
        amount: int,
        currency: str = "AUD",
        meta_data: dict[str, Any] | None = None,
        reference: str = "",
        description: str = "",
    ) -> dict[str, Any]:
        payout_meta = dict(meta_data or {})
        if description and "reviewpay_platform_fee_description" not in payout_meta:
            payout_meta["reviewpay_platform_fee_description"] = description
        base_idempotency_key = self._stripe_idempotency_key(
            payout_meta.get("idempotency_key"),
            reference,
            payout_meta.get("provider_txn_id"),
        )
        reversals, collected_amount = self._collect_platform_fee_via_transfer_reversals(
            wallet_id=wallet_id,
            amount=amount,
            currency=currency,
            payout_meta=payout_meta,
            base_idempotency_key=base_idempotency_key,
        )
        return {
            "provider_name": "stripe",
            "amount_cents": collected_amount,
            "currency": currency.upper(),
            "reversals": reversals,
        }

    def restore_platform_fee(
        self,
        *,
        wallet_id: str,
        amount: int,
        currency: str = "AUD",
        meta_data: dict[str, Any] | None = None,
        reference: str = "",
        description: str = "",
        collected_fee: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payout_meta = dict(meta_data or {})
        if description and "reviewpay_platform_fee_description" not in payout_meta:
            payout_meta["reviewpay_platform_fee_description"] = description
        base_idempotency_key = self._stripe_idempotency_key(
            payout_meta.get("idempotency_key"),
            reference,
            payout_meta.get("provider_txn_id"),
        )
        restorations = self._restore_platform_fee_reversals(
            wallet_id=wallet_id,
            currency=currency,
            payout_meta=payout_meta,
            reversals=list((collected_fee or {}).get("reversals") or []),
            base_idempotency_key=base_idempotency_key,
        )
        return {
            "provider_name": "stripe",
            "amount_cents": amount,
            "currency": currency.upper(),
            "restorations": restorations,
        }

    def is_configured(self) -> bool:
        return bool((settings.stripe_secret_key or "").strip())

    def provision_user_wallet(
        self, user_id: int, email: str, name: str, *, currency: str = "AUD"
    ) -> str:
        try:
            account = create_connected_account(
                email=email,
                business_name=name,
                business_type="individual",
                metadata={
                    "reviewpay_user_id": str(user_id),
                    "reviewpay_role": "user",
                },
            )
        except (StripeServiceError, stripe.error.StripeError) as exc:
            raise self._wallet_error(exc) from exc

        account_id = str(_attr(account, "id") or "")
        if not account_id:
            raise WalletProviderError("Stripe did not return an account ID")
        return account_id

    def provision_business_wallet(
        self,
        business_id: int,
        business_name: str,
        owner_email: str,
        *,
        currency: str = "AUD",
    ) -> str:
        try:
            account = create_connected_account(
                email=owner_email,
                business_name=business_name,
                metadata={"business_id": str(business_id), "role": "business_wallet"},
            )
        except (StripeServiceError, stripe.error.StripeError) as exc:
            raise self._wallet_error(exc) from exc

        account_id = str(_attr(account, "id") or "")
        if not account_id:
            raise WalletProviderError("Stripe did not return an account ID")
        return account_id

    def get_balance(self, wallet_id: str, *, with_queued: bool = False) -> WalletBalance:
        try:
            balance = stripe.Balance.retrieve(
                **self._request_options(stripe_account=wallet_id)
            )
        except stripe.error.StripeError as exc:
            raise self._wallet_error(exc) from exc

        raw = self._serialize(balance)
        currency = self._pick_currency(raw)
        available_balance, _instant_available_balance, pending_in, pending_out = self._balance_components(
            raw,
            currency,
        )
        for transfer in self._pending_transfers(wallet_id):
            transfer_currency = str(transfer.currency or "AUD").upper()
            if transfer_currency != currency.upper():
                continue
            transfer_amount = int(transfer.amount)
            if str(transfer.destination_stripe_account or "") == wallet_id:
                pending_in += transfer_amount
            if str(transfer.source_id or "") == wallet_id:
                pending_out += transfer_amount
        pending_total = pending_in - pending_out

        return WalletBalance(
            wallet_id=wallet_id,
            currency=currency,
            balance=available_balance + pending_total,
            available_balance=available_balance,
            pending_in=pending_in,
            pending_out=pending_out,
            raw=raw,
        )

    def get_transactions(
        self, wallet_id: str, *, limit: int = 50
    ) -> list[WalletTransaction]:
        try:
            transactions = stripe.BalanceTransaction.list(
                limit=limit,
                **self._request_options(stripe_account=wallet_id),
            )
        except stripe.error.StripeError as exc:
            raise self._wallet_error(exc) from exc

        rows = [self._map_transaction(row, source=wallet_id) for row in self._list_rows(transactions)]
        pending = [self._map_pending_transfer(row) for row in self._pending_transfers(wallet_id)[:limit]]
        return self._sort_transactions([*pending, *rows], limit=limit)

    def get_transaction(self, transaction_id: str) -> WalletTransaction:
        try:
            pending_transfer_id = int(transaction_id)
        except (TypeError, ValueError):
            pending_transfer_id = None
        if pending_transfer_id is not None:
            db = SessionLocal()
            try:
                pending = db.get(PendingWalletTransfer, pending_transfer_id)
                if (
                    pending is not None
                    and pending.destination_type == "stripe_account"
                ):
                    return self._map_pending_transfer(pending)
            finally:
                db.close()
        try:
            transaction = stripe.BalanceTransaction.retrieve(
                transaction_id,
                **self._request_options(),
            )
        except stripe.error.StripeError as exc:
            raise self._wallet_error(exc) from exc
        return self._map_transaction(transaction)

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
        db: Session | None = None,
    ) -> WalletTransaction:
        session = db or SessionLocal()
        owns_session = db is None
        try:
            payload = dict(meta_data or {})
            if description and "description" not in payload:
                payload["description"] = description

            pending = PendingWalletTransfer(
                status="pending",
                source_type="wallet",
                source_id=source,
                destination_type="stripe_account",
                destination_id=self._pending_destination_id(payload),
                destination_stripe_account=destination,
                amount=Decimal(amount),
                currency=currency.upper(),
                reference_type="transfer_group" if reference else None,
                reference_id=reference or None,
                meta_data=payload,
            )
            session.add(pending)
            if owns_session:
                session.commit()
            else:
                session.flush()
            session.refresh(pending)

            return WalletTransaction(
                transaction_id=str(pending.id),
                amount=amount,
                currency=pending.currency,
                status="pending",
                source=source,
                destination=destination,
                meta_data=dict(payload),
                created_at=self._created_at(pending.created_at),
                raw={"pending_transfer_id": pending.id, "status": pending.status},
            )
        finally:
            if owns_session:
                session.close()

    def create_payout(
        self,
        *,
        wallet_id: str,
        amount: int,
        currency: str = "AUD",
        description: str = "",
        meta_data: dict[str, Any] | None = None,
    ) -> WalletTransaction:
        payout_meta = dict(meta_data or {})
        base_idempotency_key = self._stripe_idempotency_key(
            payout_meta.get("idempotency_key"),
            payout_meta.get("reference"),
            payout_meta.get("provider_txn_id"),
        )
        fee_amount = int(payout_meta.get("reviewpay_platform_fee_amount_cents") or 0)
        payout_amount = amount - fee_amount
        if fee_amount < 0 or payout_amount <= 0:
            raise WalletProviderError(
                "Invalid Stripe payout fee breakdown",
                payload={
                    "amount": amount,
                    "fee_amount": fee_amount,
                    "wallet_id": wallet_id,
                },
            )

        fee_reversals: list[dict[str, Any]] = []
        if fee_amount > 0:
            fee_reversals, _collected_amount = self._collect_platform_fee_via_transfer_reversals(
                wallet_id=wallet_id,
                amount=fee_amount,
                currency=currency,
                payout_meta=payout_meta,
                base_idempotency_key=base_idempotency_key,
            )
            payout_meta["reviewpay_platform_fee_reversals"] = fee_reversals

        payout_idempotency_key = self._stripe_idempotency_key(base_idempotency_key, "payout")
        payout_params: dict[str, Any] = {
            "amount": payout_amount,
            "currency": currency.lower(),
            "description": description or "Wallet payout",
            "metadata": payout_meta,
            "stripe_account": wallet_id,
            **self._request_options(),
        }
        if payout_idempotency_key:
            payout_params["idempotency_key"] = payout_idempotency_key
        if is_test_wallet_environment(settings.environment, settings.stripe_secret_key):
            try:
                balance = stripe.Balance.retrieve(**self._request_options(stripe_account=wallet_id))
                raw_balance = self._serialize(balance)
                available_balance, instant_available_balance, _pending_in, _pending_out = self._balance_components(
                    raw_balance,
                    currency.upper(),
                )
                if available_balance >= amount and instant_available_balance >= amount:
                    payout_params["method"] = "instant"
            except stripe.error.StripeError:
                pass
        try:
            payout = stripe.Payout.create(**payout_params)
        except stripe.error.StripeError as exc:
            if fee_reversals:
                try:
                    restorations = self._restore_platform_fee_reversals(
                        wallet_id=wallet_id,
                        currency=currency,
                        payout_meta=payout_meta,
                        reversals=fee_reversals,
                        base_idempotency_key=base_idempotency_key,
                    )
                except WalletProviderError as restore_exc:
                    raise WalletProviderError(
                        "Stripe payout fee transfer reversals succeeded but payout failed and automatic fee restoration also failed",
                        payload={
                            "payout_error": {
                                "type": exc.__class__.__name__,
                                "message": str(exc),
                            },
                            "restore_error": restore_exc.payload or restore_exc.message,
                            "platform_fee_reversals": fee_reversals,
                        },
                    ) from exc
                raise WalletProviderError(
                    str(exc.user_message or exc),
                    payload={
                        "type": exc.__class__.__name__,
                        "message": str(exc),
                        "platform_fee_reversals": fee_reversals,
                        "platform_fee_restorations": restorations,
                    },
                ) from exc
            raise self._wallet_error(exc) from exc
        raw_payout = self._serialize(payout)
        if fee_reversals:
            raw_payout["platform_fee_reversals"] = fee_reversals
        return WalletTransaction(
            transaction_id=str(payout.get("id", "")),
            amount=amount,
            currency=currency.upper(),
            status=str(payout.get("status", "pending")),
            source=wallet_id,
            destination="bank_account",
            meta_data=payout_meta,
            created_at=datetime.now(UTC).isoformat(),
            raw=raw_payout,
        )

    def commit_inflight(self, transaction_id: str) -> WalletTransaction:
        db = SessionLocal()
        try:
            pending = db.get(PendingWalletTransfer, int(transaction_id))
            if pending is None:
                raise WalletProviderError(f"Pending wallet transfer {transaction_id} was not found")
            if pending.status == "committed" and pending.stripe_transfer_id:
                return WalletTransaction(
                    transaction_id=str(pending.id),
                    amount=int(pending.amount),
                    currency=pending.currency,
                    status="committed",
                    source=str(pending.source_id or ""),
                    destination=str(pending.destination_stripe_account or ""),
                    meta_data=dict(pending.meta_data or {}),
                    created_at=self._created_at(pending.committed_at),
                    raw={
                        "pending_transfer_id": pending.id,
                        "stripe_transfer_id": pending.stripe_transfer_id,
                    },
                )
            if pending.status != "pending":
                raise WalletProviderError(
                    f"Pending wallet transfer {transaction_id} is not pending"
                )
            if not pending.destination_stripe_account:
                raise WalletProviderError(
                    f"Pending wallet transfer {transaction_id} has no destination account"
                )

            transfer_idempotency_key = self._stripe_idempotency_key(
                "pending-wallet-transfer",
                pending.id,
                "commit",
            )
            transfer_meta = dict(pending.meta_data or {})
            transfer_meta["stripe_transfer_idempotency_key"] = transfer_idempotency_key

            transfer_params: dict[str, Any] = {
                "amount": int(pending.amount),
                "currency": pending.currency.lower(),
                "destination": pending.destination_stripe_account,
                "metadata": transfer_meta,
                "transfer_group": pending.reference_id or None,
                **self._request_options(),
            }
            if transfer_idempotency_key:
                transfer_params["idempotency_key"] = transfer_idempotency_key
            try:
                transfer = stripe.Transfer.create(**transfer_params)
            except stripe.error.StripeError as exc:
                raise self._wallet_error(exc) from exc

            transfer_id = str(transfer.get("id") or "")
            pending.status = "committed"
            pending.stripe_transfer_id = transfer_id
            pending.committed_at = datetime.now(UTC)
            pending.meta_data = {
                **dict(pending.meta_data or {}),
                "stripe_transfer_idempotency_key": transfer_idempotency_key,
                "stripe_transfer_id": transfer_id,
            }
            try:
                db.commit()
            except Exception as exc:
                db.rollback()
                raise WalletProviderError(
                    "Stripe transfer succeeded but ReviewPay could not persist the result; retry the commit safely with the same pending transfer",
                    payload={
                        "pending_transfer_id": pending.id,
                        "stripe_transfer_id": transfer_id or None,
                        "stripe_transfer_idempotency_key": transfer_idempotency_key,
                        "error": str(exc),
                    },
                ) from exc
            db.refresh(pending)

            return WalletTransaction(
                transaction_id=str(pending.id),
                amount=int(pending.amount),
                currency=pending.currency,
                status="committed",
                source=str(pending.source_id or ""),
                destination=str(pending.destination_stripe_account or ""),
                meta_data=dict(pending.meta_data or {}),
                created_at=self._created_at(pending.committed_at),
                raw={
                    "pending_transfer_id": pending.id,
                    "stripe_transfer_id": pending.stripe_transfer_id,
                    "stripe_transfer_idempotency_key": transfer_idempotency_key,
                    "transfer": self._serialize(transfer),
                },
            )
        finally:
            db.close()

    def void_inflight(self, transaction_id: str) -> WalletTransaction:
        db = SessionLocal()
        try:
            pending = db.get(PendingWalletTransfer, int(transaction_id))
            if pending is None:
                raise WalletProviderError(f"Pending wallet transfer {transaction_id} was not found")
            if pending.status == "voided":
                return WalletTransaction(
                    transaction_id=str(pending.id),
                    amount=int(pending.amount),
                    currency=pending.currency,
                    status="voided",
                    source=str(pending.source_id or ""),
                    destination=str(pending.destination_stripe_account or ""),
                    meta_data=dict(pending.meta_data or {}),
                    created_at=self._created_at(pending.voided_at),
                    raw={"pending_transfer_id": pending.id, "status": pending.status},
                )
            if pending.status != "pending":
                raise WalletProviderError("Cannot void non-pending transfer")

            pending.status = "voided"
            pending.voided_at = datetime.now(UTC)
            db.commit()
            db.refresh(pending)

            return WalletTransaction(
                transaction_id=str(pending.id),
                amount=int(pending.amount),
                currency=pending.currency,
                status="voided",
                source=str(pending.source_id or ""),
                destination=str(pending.destination_stripe_account or ""),
                meta_data=dict(pending.meta_data or {}),
                created_at=self._created_at(pending.voided_at),
                raw={"pending_transfer_id": pending.id, "status": pending.status},
            )
        finally:
            db.close()

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
        transfer_meta = dict(meta_data or {})
        transfer_idempotency_key = self._stripe_idempotency_key(
            "wallet-transfer",
            reference,
            transfer_meta.get("provider_txn_id"),
            transfer_meta.get("reviewpay_review_id"),
            transfer_meta.get("reviewpay_referral_payment_id"),
        )
        if transfer_idempotency_key:
            transfer_meta["stripe_transfer_idempotency_key"] = transfer_idempotency_key
        transfer_params: dict[str, Any] = {
            "amount": amount,
            "currency": currency.lower(),
            "destination": destination,
            "metadata": transfer_meta,
            "transfer_group": reference or None,
            "description": description or None,
            **self._request_options(),
        }
        if transfer_idempotency_key:
            transfer_params["idempotency_key"] = transfer_idempotency_key
        try:
            transfer = stripe.Transfer.create(**transfer_params)
        except stripe.error.StripeError as exc:
            raise self._wallet_error(exc) from exc

        return self._map_transaction(
            transfer,
            default_status="committed",
            source=source,
            destination=destination,
        )
