"""
Fixture: relevant snippet from backend/app/services/wallet_provider.py
Source: issue #1519 — referral wallet transactions missing metadata at commit

Includes WalletProvider abstract class and NullWalletProvider stub — the
two classes that need commit_inflight signatures updated to accept meta_data.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.services.wallet_provider_names import normalize_wallet_provider_name


class WalletProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        payload: Any | None = None,
        status_code: int | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.payload = payload
        self.status_code = status_code


@dataclass(slots=True)
class WalletBalance:
    wallet_id: str = ""
    currency: str = "AUD"
    balance: int = 0
    available_balance: int = 0
    pending_in: int = 0
    pending_out: int = 0
    raw: dict[str, Any] | None = None


@dataclass(slots=True)
class WalletTransaction:
    transaction_id: str = ""
    amount: int = 0
    currency: str = "AUD"
    status: str = "pending"
    source: str = ""
    destination: str = ""
    meta_data: dict[str, Any] = field(default_factory=dict)
    created_at: str | None = None
    raw: dict[str, Any] | None = None


class WalletProvider(abc.ABC):
    @abc.abstractmethod
    def is_configured(self) -> bool:
        """Return whether the provider has enough runtime configuration to operate."""

    @abc.abstractmethod
    def provision_user_wallet(
        self, user_id: int, email: str, name: str, *, currency: str = "AUD"
    ) -> str:
        """Create or fetch a wallet for a user and return its provider wallet identifier."""

    @abc.abstractmethod
    def provision_business_wallet(
        self,
        business_id: int,
        business_name: str,
        owner_email: str,
        *,
        currency: str = "AUD",
    ) -> str:
        """Create or fetch a wallet for a business and return its provider wallet identifier."""

    @abc.abstractmethod
    def get_balance(self, wallet_id: str, *, with_queued: bool = False) -> WalletBalance:
        """Fetch the current wallet balance state for the given wallet identifier."""

    @abc.abstractmethod
    def get_transactions(
        self, wallet_id: str, *, limit: int = 50
    ) -> list[WalletTransaction]:
        """List wallet transactions for the given wallet identifier."""

    @abc.abstractmethod
    def commit_inflight(self, transaction_id: str) -> WalletTransaction:
        """Commit a previously created inflight transaction."""

    @abc.abstractmethod
    def void_inflight(self, transaction_id: str) -> WalletTransaction:
        """Void a previously created inflight transaction."""

    @abc.abstractmethod
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
        """Create an immediate committed transfer between two wallets."""


class NullWalletProvider(WalletProvider):
    """No-op provider when wallet features are disabled."""

    @staticmethod
    def _raise_unconfigured() -> None:
        raise WalletProviderError("Wallet provider is not configured")

    def is_configured(self) -> bool:
        return False

    def provision_user_wallet(
        self, user_id: int, email: str, name: str, *, currency: str = "AUD"
    ) -> str:
        self._raise_unconfigured()

    def provision_business_wallet(
        self,
        business_id: int,
        business_name: str,
        owner_email: str,
        *,
        currency: str = "AUD",
    ) -> str:
        self._raise_unconfigured()

    def get_balance(self, wallet_id: str, *, with_queued: bool = False) -> WalletBalance:
        self._raise_unconfigured()

    def get_transactions(
        self, wallet_id: str, *, limit: int = 50
    ) -> list[WalletTransaction]:
        self._raise_unconfigured()

    def commit_inflight(self, transaction_id: str) -> WalletTransaction:
        self._raise_unconfigured()

    def void_inflight(self, transaction_id: str) -> WalletTransaction:
        self._raise_unconfigured()

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
        self._raise_unconfigured()
