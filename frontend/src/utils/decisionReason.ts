import type { TFunction } from 'i18next';

// ---------------------------------------------------------------------------
// PendingDecision reason text. The backend stores a human-readable reason
// generated in one language at creation time; rows with a versioned
// decision_scope also carry ``reason_code``/``reason_params`` so the UI can
// re-render the same sentence in the interface language. Rows predating
// versioned scopes (or unknown codes) keep the stored text.
// ---------------------------------------------------------------------------

export interface DecisionReasonShape {
  reason: string;
  reason_code?: string | null;
  reason_params?: {
    title?: string | null;
    season?: number | null;
    episode?: number | null;
  } | null;
}

export function decisionReasonText(d: DecisionReasonShape, t: TFunction): string {
  if (!d.reason_code) return d.reason;
  return t(`decisions.reasons.${d.reason_code}`, {
    title: d.reason_params?.title ?? '',
    season: d.reason_params?.season ?? '',
    episode: d.reason_params?.episode ?? '',
    defaultValue: d.reason,
  });
}
