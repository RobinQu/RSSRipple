// ---------------------------------------------------------------------------
// ResourceEditWizard — pure step-validation and save-error routing logic.
// Extracted from the component so the per-step navigation gates and the
// save-time last-line checks share one implementation (and so the Node .mjs
// tests can exercise them without a DOM).
// ---------------------------------------------------------------------------

export function workKeyOf(workType: string, workId: string): string {
  return `${workType}:${workId}`;
}

export interface WizardWorkLike {
  workType: string;
  workId: string;
}

export interface WizardPlacementLike {
  workType: string;
  workId: string;
  season: number | null;
}

export interface WizardStepState {
  isBatch: boolean;
  audioLinked: boolean;
  works: WizardWorkLike[];
  placements: Record<string, WizardPlacementLike>;
}

export type WizardStepErrorCode = 'tv_season_required' | 'works_unassigned';

export interface WizardStepError {
  code: WizardStepErrorCode;
  /** Work keys (`type:id`) without any file assignment (works_unassigned). */
  workKeys?: string[];
}

/**
 * Gate for leaving wizard ``step`` (and the save-time final defense).
 * Returns null when the step's state is valid.
 *
 * Step 0 (association) has no client-side check — its invariants (works
 * existence, collection membership, non-batch single-work cap) are
 * server-enforced and routed back via ``meta.step``.
 * Step 1 (file mapping) mirrors the checks that used to live only in
 * handleSave: batch TV placements need a season, and every work of a
 * multi-work pack needs at least one file assignment.
 */
export function validateStep(
  step: number,
  state: WizardStepState,
): WizardStepError | null {
  if (step !== 1) return null;
  if (!state.isBatch || state.audioLinked) return null;
  const placements = Object.values(state.placements);
  if (placements.some((p) => p.workType === 'series' && p.season == null)) {
    return { code: 'tv_season_required' };
  }
  if (state.works.length > 1) {
    const assigned = new Set(
      placements.map((p) => workKeyOf(p.workType, p.workId)),
    );
    const workKeys = state.works
      .map((w) => workKeyOf(w.workType, w.workId))
      .filter((key) => !assigned.has(key));
    if (workKeys.length > 0) return { code: 'works_unassigned', workKeys };
  }
  return null;
}

/** Machine-readable wizard step hints carried by the associations 422. */
export type AssociationErrorStep = 'collection' | 'assignments';

/**
 * Wizard step index for a failed associations save. The server's 422 carries
 * a machine-readable ``meta.step``; the message-text regex is only a
 * fallback for responses without it (older server, non-validation errors).
 */
export function stepFromSaveError(metaStep: unknown, message: string): number {
  if (metaStep === 'collection') return 0;
  if (metaStep === 'assignments') return 1;
  if (/合集|作品集|collection/i.test(message)) return 0;
  if (/文件|指派|覆盖|区间|季/.test(message)) return 1;
  return 3;
}
