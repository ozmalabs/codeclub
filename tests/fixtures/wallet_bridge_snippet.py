"""
Fixture: relevant snippet from backend/app/services/wallet_bridge.py
Source: issue #1519 — referral wallet transactions missing metadata at commit

This is the buggy version — commit_wallet_transaction does not forward
meta_data to provider.commit_inflight.
"""

from __future__ import annotations

from typing import Any

from app.services.wallet_helpers import wallet_provider_instance as _wallet_provider
from app.services.wallet_provider import WalletProviderError, WalletTransaction
from app.services.wallet_provider_names import normalize_wallet_provider_name


def resolve_transaction_provider(
    meta_data: dict[str, Any] | None,
    *,
    fallback_provider_name: str,
) -> str:
    if isinstance(meta_data, dict):
        for key in ("wallet_transaction_provider", "wallet_provider", "ledger_provider"):
            value = normalize_wallet_provider_name(meta_data.get(key))
            if value != "none":
                return value
    return normalize_wallet_provider_name(fallback_provider_name)


def apply_wallet_transaction_meta(
    meta_data: dict[str, Any] | None,
    *,
    source_provider_name: str,
    source_wallet_id: str,
    destination_provider_name: str,
    destination_wallet_id: str,
    transaction_provider_name: str,
) -> dict[str, Any]:
    payload = dict(meta_data or {})
    payload.update(
        {
            "wallet_transaction_provider": transaction_provider_name,
            "wallet_provider": transaction_provider_name,
            "source_wallet_provider": source_provider_name,
            "destination_wallet_provider": destination_provider_name,
            "source_wallet_id": source_wallet_id,
            "destination_wallet_id": destination_wallet_id,
        }
    )
    if source_provider_name != destination_provider_name:
        payload["cross_provider_payment"] = True
        payload["wallet_bridge"] = True
    return payload


def commit_wallet_transaction(
    transaction_id: str,
    *,
    meta_data: dict[str, Any] | None,
    fallback_provider_name: str,
) -> WalletTransaction:
    provider = _wallet_provider(
        resolve_transaction_provider(meta_data, fallback_provider_name=fallback_provider_name)
    )
    return provider.commit_inflight(transaction_id)


def void_wallet_transaction(
    transaction_id: str,
    *,
    meta_data: dict[str, Any] | None,
    fallback_provider_name: str,
) -> WalletTransaction:
    provider = _wallet_provider(
        resolve_transaction_provider(meta_data, fallback_provider_name=fallback_provider_name)
    )
    return provider.void_inflight(transaction_id)
