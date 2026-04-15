import React, { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { AlertCircle, CheckCircle, ExternalLink, RefreshCw, X, XCircle } from 'lucide-react';

import BusinessContentShell from '../../components/business/Dashboard/BusinessContentShell';
import { getBusinessById, getMyBusiness } from '../../services/domain/businessApi';
import {
  createStripeConnectAccount,
  createStripeConnectAccountLink,
  createUserStripeConnectAccount,
  createUserStripeConnectAccountLink,
  getStripeConnectStatus,
  getUserStripeConnectStatus,
  listPaymentProviders,
} from '../../services/domain/paymentProvidersApi';

const primaryButtonClass = 'app-btn app-btn-primary rounded-full px-6 py-3';
const surfaceButtonClass = 'app-btn app-btn-surface rounded-full px-6 py-3';

const StatusBadge = ({ label, ok }) => (
  <span
    className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-sm font-medium ${
      ok
        ? 'bg-emerald-500/15 text-emerald-300 border border-emerald-400/30'
        : 'bg-amber-500/15 text-amber-300 border border-amber-400/30'
    }`}
  >
    {ok ? <CheckCircle className="h-3.5 w-3.5" /> : <XCircle className="h-3.5 w-3.5" />}
    {label}
  </span>
);

const useStripeProvider = () => {
  const [providerId, setProviderId] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    listPaymentProviders()
      .then((payload) => {
        const providers = Array.isArray(payload?.providers) ? payload.providers : [];
        const stripe = providers.find(
          (p) => p?.active && String(p.type || '').toLowerCase() === 'stripe',
        );
        if (!stripe) {
          setError('No active Stripe payment provider is configured for this account.');
        } else {
          setProviderId(stripe.id);
        }
      })
      .catch(() => setError('Failed to load payment providers.'));
  }, []);

  return { providerId, providerError: error };
};

const useBusinessId = (routeBusinessId) => {
  const [businessId, setBusinessId] = useState(null);
  const [businessError, setBusinessError] = useState(null);

  useEffect(() => {
    const id = routeBusinessId ? Number(routeBusinessId) : NaN;
    const fetcher = Number.isFinite(id) ? getBusinessById(id) : getMyBusiness();
    fetcher
      .then((biz) => setBusinessId(biz?.id ?? biz?.business_id ?? null))
      .catch(() => setBusinessError('Failed to load business.'));
  }, [routeBusinessId]);

  return { businessId, businessError };
};

export const StripeConnectPage = () => {
  const { businessId: routeBusinessId } = useParams();
  const navigate = useNavigate();

  const { providerId, providerError } = useStripeProvider();
  const { businessId, businessError } = useBusinessId(routeBusinessId);

  const [status, setStatus] = useState(null);
  const [loadingStatus, setLoadingStatus] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState(null);

  const businessBasePath = routeBusinessId
    ? `/dashboard/b/${routeBusinessId}`
    : null;

  const fetchStatus = useCallback(async () => {
    if (!providerId || !businessId) return;
    setLoadingStatus(true);
    try {
      const result = await getStripeConnectStatus(providerId, businessId);
      setStatus(result);
    } catch (err) {
      if (err?.status !== 404) {
        setError('Failed to load Stripe connection status.');
      }
      setStatus(null);
    } finally {
      setLoadingStatus(false);
    }
  }, [providerId, businessId]);

  useEffect(() => {
    fetchStatus();
  }, [fetchStatus]);

  const handleConnect = async () => {
    if (!providerId || !businessId) return;
    setConnecting(true);
    setError(null);
    try {
      await createStripeConnectAccount(providerId, businessId);
      const origin = window.location.origin;
      const base = `/dashboard/b/${routeBusinessId}/settings/payments/stripe`;
      const linkResult = await createStripeConnectAccountLink(providerId, businessId, {
        refreshUrl: `${origin}${base}/refresh`,
        returnUrl: `${origin}${base}/return`,
      });
      const url = linkResult?.url;
      if (!url) throw new Error('No onboarding URL returned.');
      window.location.href = url;
    } catch (err) {
      setError(err?.data?.detail?.message ?? err?.data?.detail ?? err?.message ?? 'Failed to start Stripe onboarding.');
      setConnecting(false);
    }
  };

  const isReady = providerId && businessId;
  const payoutsEnabled = status?.payouts_enabled ?? status?.status?.payouts_enabled ?? false;
  const chargesEnabled = status?.charges_enabled ?? status?.status?.charges_enabled ?? false;
  const detailsSubmitted = status?.details_submitted ?? status?.status?.details_submitted ?? false;
  const accountId = status?.account_id ?? status?.id;
  const fullyOnboarded = payoutsEnabled && chargesEnabled && detailsSubmitted;

  return (
    <BusinessContentShell>
      <div className="max-w-xl mx-auto px-4 py-8">
        <h1 className="text-white text-2xl font-bold font-['GeneralSansBold'] mb-2">
          Stripe
        </h1>
        <p className="text-[#959DB0] text-sm mb-8">
          Connect your Stripe account so Acme can fund cashback and keep your business payment
          setup active.
        </p>

        {(providerError || businessError) && (
          <div className="rounded-xl bg-red-500/10 border border-red-400/30 p-4 text-red-300 text-sm mb-6">
            {providerError || businessError}
          </div>
        )}

        {error && (
          <div className="rounded-xl bg-red-500/10 border border-red-400/30 p-4 text-red-300 text-sm mb-6">
            {error}
          </div>
        )}

        <div className="bg-[#0D182E] rounded-2xl p-6 mb-6">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-white text-lg font-semibold font-['GeneralSansBold']">
              Connection Status
            </h2>
            {isReady && (
              <button
                type="button"
                onClick={fetchStatus}
                disabled={loadingStatus}
                aria-label="Refresh status"
                className="text-[#959DB0] hover:text-white transition-colors disabled:opacity-50"
              >
                <RefreshCw className={`h-4 w-4 ${loadingStatus ? 'animate-spin' : ''}`} />
              </button>
            )}
          </div>

          {loadingStatus && !status && (
            <p className="text-[#959DB0] text-sm">Checking status…</p>
          )}

          {!loadingStatus && !status && !providerError && !businessError && (
            <p className="text-[#959DB0] text-sm">No Stripe account connected yet.</p>
          )}

          {status && (
            <div className="space-y-3">
              {accountId && (
                <div className="flex items-center justify-between">
                  <span className="text-[#959DB0] text-sm">Account ID</span>
                  <span className="text-white text-sm font-mono">{accountId}</span>
                </div>
              )}
              <div className="flex items-center justify-between">
                <span className="text-[#959DB0] text-sm">Details submitted</span>
                <StatusBadge label={detailsSubmitted ? 'Submitted' : 'Incomplete'} ok={detailsSubmitted} />
              </div>
              <div className="flex items-center justify-between">
                <span className="text-[#959DB0] text-sm">Charges enabled</span>
                <StatusBadge label={chargesEnabled ? 'Enabled' : 'Not enabled'} ok={chargesEnabled} />
              </div>
              <div className="flex items-center justify-between">
                <span className="text-[#959DB0] text-sm">Payouts enabled</span>
                <StatusBadge label={payoutsEnabled ? 'Enabled' : 'Not enabled'} ok={payoutsEnabled} />
              </div>
            </div>
          )}
        </div>

        {fullyOnboarded ? (
          <div className="rounded-xl bg-emerald-500/10 border border-emerald-400/30 p-4 text-emerald-300 text-sm text-center">
            Your Stripe account is fully connected and ready for cashback funding.
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            <button
              type="button"
              onClick={handleConnect}
              disabled={!isReady || connecting}
              className={`${primaryButtonClass} flex items-center justify-center gap-2 disabled:opacity-50`}
            >
              {connecting ? (
                <RefreshCw className="h-4 w-4 animate-spin" />
              ) : (
                <ExternalLink className="h-4 w-4" />
              )}
              {status ? 'Continue Stripe Onboarding' : 'Connect with Stripe'}
            </button>
            <p className="text-[#959DB0] text-xs text-center">
              You will be redirected to Stripe to complete your account setup.
            </p>
          </div>
        )}

        <div className="mt-6 text-center">
          <button
            type="button"
            className={surfaceButtonClass}
            onClick={() => navigate(businessBasePath ? `${businessBasePath}/settings` : '/dashboard')}
          >
            Back to Settings
          </button>
        </div>
      </div>
    </BusinessContentShell>
  );
};

export const StripeConnectReturnPage = () => {
  const { businessId: routeBusinessId } = useParams();
  const navigate = useNavigate();
  const businessBasePath = routeBusinessId ? `/dashboard/b/${routeBusinessId}` : null;

  return (
    <BusinessContentShell>
      <div className="max-w-xl mx-auto px-4 py-16 text-center">
        <CheckCircle className="h-16 w-16 text-emerald-400 mx-auto mb-6" />
        <h1 className="text-white text-2xl font-bold font-['GeneralSansBold'] mb-3">
          Stripe Setup Complete
        </h1>
        <p className="text-[#959DB0] text-sm mb-8">
          Your Stripe account details have been submitted. It may take a moment for your account to
          be fully activated.
        </p>
        <button
          type="button"
          className={primaryButtonClass}
          onClick={() =>
            navigate(businessBasePath ? `${businessBasePath}/settings/payments/stripe` : '/dashboard')
          }
        >
          View Connection Status
        </button>
      </div>
    </BusinessContentShell>
  );
};

export const StripeConnectRefreshPage = () => {
  const { businessId: routeBusinessId } = useParams();
  const navigate = useNavigate();

  const { providerId, providerError } = useStripeProvider();
  const { businessId, businessError } = useBusinessId(routeBusinessId);

  const [error, setError] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  const businessBasePath = routeBusinessId ? `/dashboard/b/${routeBusinessId}` : null;

  useEffect(() => {
    if (!providerId || !businessId) return;
    setRefreshing(true);
    const origin = window.location.origin;
    const base = `/dashboard/b/${routeBusinessId}/settings/payments/stripe`;
    createStripeConnectAccountLink(providerId, businessId, {
      refreshUrl: `${origin}${base}/refresh`,
      returnUrl: `${origin}${base}/return`,
    })
      .then((result) => {
        const url = result?.url;
        if (url) {
          window.location.href = url;
        } else {
          setError('Could not generate a new onboarding link.');
          setRefreshing(false);
        }
      })
      .catch(() => {
        setError('Failed to refresh the Stripe onboarding link.');
        setRefreshing(false);
      });
  }, [providerId, businessId, routeBusinessId]);

  return (
    <BusinessContentShell>
      <div className="max-w-xl mx-auto px-4 py-16 text-center">
        {refreshing && !error && !providerError && !businessError ? (
          <>
            <RefreshCw className="h-12 w-12 text-[#65D4B0] mx-auto mb-6 animate-spin" />
            <p className="text-[#959DB0] text-sm">Refreshing your Stripe onboarding link…</p>
          </>
        ) : (
          <>
            <XCircle className="h-12 w-12 text-red-400 mx-auto mb-6" />
            <h1 className="text-white text-xl font-bold font-['GeneralSansBold'] mb-3">
              Onboarding Link Expired
            </h1>
            <p className="text-red-300 text-sm mb-8">
              {error || providerError || businessError || 'The Stripe onboarding link has expired.'}
            </p>
            <button
              type="button"
              className={primaryButtonClass}
              onClick={() =>
                navigate(businessBasePath ? `${businessBasePath}/settings/payments/stripe` : '/dashboard')
              }
            >
              Back to Stripe Settings
            </button>
          </>
        )}
      </div>
    </BusinessContentShell>
  );
};

// ── User Stripe Connect ────────────────────────────────────────────────────────

const USER_STRIPE_BASE = '/dashboard/settings/payments/stripe';

export const UserStripeConnectPage = () => {
  const navigate = useNavigate();
  const { providerId, providerError } = useStripeProvider();

  const [status, setStatus] = useState(null);
  const [loadingStatus, setLoadingStatus] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState(null);
  const [showInfoModal, setShowInfoModal] = useState(false);

  const fetchStatus = useCallback(async () => {
    if (!providerId) return;
    setLoadingStatus(true);
    try {
      const result = await getUserStripeConnectStatus(providerId);
      setStatus(result?.connected ? result : null);
    } catch (err) {
      if (err?.status !== 404) {
        setError('Failed to load Stripe connection status.');
      }
      setStatus(null);
    } finally {
      setLoadingStatus(false);
    }
  }, [providerId]);

  useEffect(() => {
    fetchStatus();
  }, [fetchStatus]);

  const handleConnect = async () => {
    if (!providerId) return;
    setConnecting(true);
    setError(null);
    try {
      await createUserStripeConnectAccount(providerId);
      const origin = window.location.origin;
      const linkResult = await createUserStripeConnectAccountLink(providerId, {
        refreshUrl: `${origin}${USER_STRIPE_BASE}/refresh`,
        returnUrl: `${origin}${USER_STRIPE_BASE}/return`,
      });
      const url = linkResult?.url;
      if (!url) throw new Error('No onboarding URL returned.');
      window.location.href = url;
    } catch (err) {
      setError(err?.data?.detail?.message ?? err?.data?.detail ?? err?.message ?? 'Failed to start Stripe onboarding.');
      setConnecting(false);
    }
  };

  const payoutsEnabled = status?.payouts_enabled ?? status?.status?.payouts_enabled ?? false;
  const chargesEnabled = status?.charges_enabled ?? status?.status?.charges_enabled ?? false;
  const detailsSubmitted = status?.details_submitted ?? status?.status?.details_submitted ?? false;
  const accountId = status?.account_id ?? status?.id;
  const fullyOnboarded = payoutsEnabled && chargesEnabled && detailsSubmitted;

  return (
    <div className="max-w-xl mx-auto px-4 py-8">
      <h1 className="text-white text-2xl font-bold font-['GeneralSansBold'] mb-2">
        Stripe Payouts
      </h1>
      <p className="text-[#959DB0] text-sm mb-8">
        Connect your Stripe account so Acme can pay cashback directly to your bank account.
      </p>

      {(providerError || error) && (
        <div className="rounded-xl bg-red-500/10 border border-red-400/30 p-4 text-red-300 text-sm mb-6">
          {providerError || error}
        </div>
      )}

      <div className="bg-[#0D182E] rounded-2xl p-6 mb-6">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-white text-lg font-semibold font-['GeneralSansBold']">
            Connection Status
          </h2>
          {providerId && (
            <button
              type="button"
              onClick={fetchStatus}
              disabled={loadingStatus}
              aria-label="Refresh status"
              className="text-[#959DB0] hover:text-white transition-colors disabled:opacity-50"
            >
              <RefreshCw className={`h-4 w-4 ${loadingStatus ? 'animate-spin' : ''}`} />
            </button>
          )}
        </div>

        {loadingStatus && !status && (
          <p className="text-[#959DB0] text-sm">Checking status…</p>
        )}

        {!loadingStatus && !status && !providerError && (
          <p className="text-[#959DB0] text-sm">No Stripe account connected yet.</p>
        )}

        {status && (
          <div className="space-y-3">
            {accountId && (
              <div className="flex items-center justify-between">
                <span className="text-[#959DB0] text-sm">Account ID</span>
                <span className="text-white text-sm font-mono">{accountId}</span>
              </div>
            )}
            <div className="flex items-center justify-between">
              <span className="text-[#959DB0] text-sm">Details submitted</span>
              <StatusBadge label={detailsSubmitted ? 'Submitted' : 'Incomplete'} ok={detailsSubmitted} />
            </div>
            <div className="flex items-center justify-between">
              <span className="text-[#959DB0] text-sm">Charges enabled</span>
              <StatusBadge label={chargesEnabled ? 'Enabled' : 'Not enabled'} ok={chargesEnabled} />
            </div>
            <div className="flex items-center justify-between">
              <span className="text-[#959DB0] text-sm">Payouts enabled</span>
              <StatusBadge label={payoutsEnabled ? 'Enabled' : 'Not enabled'} ok={payoutsEnabled} />
            </div>
          </div>
        )}
      </div>

      {fullyOnboarded ? (
        <div className="rounded-xl bg-emerald-500/10 border border-emerald-400/30 p-4 text-emerald-300 text-sm text-center">
          Your Stripe account is fully connected and ready to receive payouts.
        </div>
      ) : (
        <div className="flex flex-col gap-3">
          <button
            type="button"
            onClick={() => setShowInfoModal(true)}
            disabled={!providerId || connecting}
            className={`${primaryButtonClass} flex items-center justify-center gap-2 disabled:opacity-50`}
          >
            {connecting ? (
              <RefreshCw className="h-4 w-4 animate-spin" />
            ) : (
              <ExternalLink className="h-4 w-4" />
            )}
            {status ? 'Continue Stripe Onboarding' : 'Connect with Stripe'}
          </button>
          <p className="text-[#959DB0] text-xs text-center">
            You will be redirected to Stripe to complete your account setup.
          </p>
        </div>
      )}

      {showInfoModal && (
        <div
          className="fixed inset-0 z-[200] flex items-center justify-center bg-black/70 px-4 py-4"
          onClick={() => setShowInfoModal(false)}
          role="presentation"
        >
          <div
            className="relative w-full max-w-lg rounded-2xl border border-white/10 bg-[#0B1629] p-6 shadow-[0_24px_80px_rgba(0,0,0,0.35)]"
            onClick={(e) => e.stopPropagation()}
            role="dialog"
            aria-modal="true"
            aria-labelledby="stripe-info-title"
          >
            <button
              type="button"
              className="absolute right-3 top-3 inline-flex h-9 w-9 items-center justify-center rounded-full border border-white/10 bg-white/5 text-white transition hover:bg-white/10"
              onClick={() => setShowInfoModal(false)}
              aria-label="Close"
            >
              <X className="h-4 w-4" />
            </button>

            <div className="flex items-start gap-3 mb-4">
              <AlertCircle className="h-6 w-6 text-amber-400 shrink-0 mt-0.5" />
              <h2 id="stripe-info-title" className="text-white text-lg font-semibold font-['GeneralSansBold']">
                Before you continue
              </h2>
            </div>

            <div className="space-y-4 text-[#C2D0DE] text-sm leading-relaxed">
              <p>
                Stripe is our payment partner and is required to collect your personal and banking details
                before they can send you cashback payments. This is a regulatory requirement for financial compliance.
              </p>
              <p>
                Stripe will ask you to provide:
              </p>
              <ul className="list-disc list-inside space-y-1 text-[#959DB0]">
                <li>Your personal details (name, date of birth, address)</li>
                <li>A valid form of identification</li>
                <li>Your bank account details (BSB &amp; account number) for payouts</li>
              </ul>
              <div className="rounded-xl bg-blue-500/10 border border-blue-400/20 p-3">
                <p className="text-blue-200 text-sm">
                  <strong>Important:</strong> Cashback payments can only be deposited into a{' '}
                  <strong>bank account</strong>. Credit and debit cards cannot be used to receive payouts.
                </p>
              </div>
              <div className="rounded-xl bg-amber-500/10 border border-amber-400/20 p-3">
                <p className="text-amber-200 text-sm">
                  <strong>Note:</strong> On the final page Stripe will show <strong>"Business Type"</strong> as{' '}
                  <strong>Individual / Sole trader</strong>. This is normal and required — it is how Stripe
                  categorises personal accounts that receive payments.
                </p>
              </div>
              <p>
                Some details from your Acme profile have been pre-filled, but Stripe may still ask
                you to confirm or re-enter them.
              </p>
            </div>

            <div className="flex gap-3 mt-6">
              <button
                type="button"
                className={`${surfaceButtonClass} flex-1`}
                onClick={() => setShowInfoModal(false)}
              >
                Cancel
              </button>
              <button
                type="button"
                className={`${primaryButtonClass} flex-1 flex items-center justify-center gap-2`}
                disabled={connecting}
                onClick={() => {
                  setShowInfoModal(false);
                  handleConnect();
                }}
              >
                {connecting ? (
                  <RefreshCw className="h-4 w-4 animate-spin" />
                ) : (
                  <ExternalLink className="h-4 w-4" />
                )}
                Continue to Stripe
              </button>
            </div>
          </div>
        </div>
      )}

      <div className="mt-6 text-center">
        <button
          type="button"
          className={surfaceButtonClass}
          onClick={() => navigate('/dashboard/settings')}
        >
          Back to Settings
        </button>
      </div>
    </div>
  );
};

export const UserStripeConnectReturnPage = () => {
  const navigate = useNavigate();
  return (
    <div className="max-w-xl mx-auto px-4 py-16 text-center">
      <CheckCircle className="h-16 w-16 text-emerald-400 mx-auto mb-6" />
      <h1 className="text-white text-2xl font-bold font-['GeneralSansBold'] mb-3">
        Stripe Setup Complete
      </h1>
      <p className="text-[#959DB0] text-sm mb-8">
        Your Stripe account details have been submitted. It may take a moment for your account to
        be fully activated.
      </p>
      <button
        type="button"
        className={primaryButtonClass}
        onClick={() => navigate(USER_STRIPE_BASE)}
      >
        View Connection Status
      </button>
    </div>
  );
};

export const UserStripeConnectRefreshPage = () => {
  const navigate = useNavigate();
  const { providerId, providerError } = useStripeProvider();

  const [error, setError] = useState(null);
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    if (!providerId) return;
    setRefreshing(true);
    const origin = window.location.origin;
    createUserStripeConnectAccountLink(providerId, {
      refreshUrl: `${origin}${USER_STRIPE_BASE}/refresh`,
      returnUrl: `${origin}${USER_STRIPE_BASE}/return`,
    })
      .then((result) => {
        const url = result?.url;
        if (url) {
          window.location.href = url;
        } else {
          setError('Could not generate a new onboarding link.');
          setRefreshing(false);
        }
      })
      .catch(() => {
        setError('Failed to refresh the Stripe onboarding link.');
        setRefreshing(false);
      });
  }, [providerId]);

  return (
    <div className="max-w-xl mx-auto px-4 py-16 text-center">
      {refreshing && !error && !providerError ? (
        <>
          <RefreshCw className="h-12 w-12 text-[#65D4B0] mx-auto mb-6 animate-spin" />
          <p className="text-[#959DB0] text-sm">Refreshing your Stripe onboarding link…</p>
        </>
      ) : (
        <>
          <XCircle className="h-12 w-12 text-red-400 mx-auto mb-6" />
          <h1 className="text-white text-xl font-bold font-['GeneralSansBold'] mb-3">
            Onboarding Link Expired
          </h1>
          <p className="text-red-300 text-sm mb-8">
            {error || providerError || 'The Stripe onboarding link has expired.'}
          </p>
          <button
            type="button"
            className={primaryButtonClass}
            onClick={() => navigate(USER_STRIPE_BASE)}
          >
            Back to Stripe Settings
          </button>
        </>
      )}
    </div>
  );
};
