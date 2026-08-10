import type {
  BoolCondition,
  FieldCondition,
  FilterConfig,
  FilterOperator,
} from '../types';
import type { TFunction } from 'i18next';

// ---------------------------------------------------------------------------
// Filter tree guards & helpers — shared by FilterBuilder and its consumers.
// Lives outside FilterBuilder.tsx so that file only exports components
// (react-refresh/only-export-components).
// ---------------------------------------------------------------------------

const isBoolCondition = (node: unknown): node is BoolCondition => {
  return (
    typeof node === 'object' &&
    node !== null &&
    'combinator' in node &&
    'conditions' in node
  );
};

const isFieldCondition = (node: unknown): node is FieldCondition => {
  return (
    typeof node === 'object' &&
    node !== null &&
    'field' in node &&
    'operator' in node
  );
};

// A value-taking operator with an empty value is rejected by the backend at
// save time; blank strings count as empty too.
const isEmptyValue = (v: FieldCondition['value'] | null | undefined): boolean =>
  v === undefined ||
  v === null ||
  (typeof v === 'string' && v.trim() === '') ||
  (Array.isArray(v) && v.length === 0);

function emptyBool(): BoolCondition {
  return { combinator: 'and', conditions: [] };
}

export { isBoolCondition, isFieldCondition, isEmptyValue, emptyBool };

// Operators that take no value (is_empty / is_not_empty). Valid for every
// field type; the backend ignores ``value`` when one of these is selected.
const NO_VALUE_OPERATORS: FilterOperator[] = ['is_empty', 'is_not_empty'];
export const isNoValueOperator = (op: FilterOperator): boolean =>
  NO_VALUE_OPERATORS.includes(op);

export function normalizeFilter(v: FilterConfig | null | undefined): BoolCondition {
  if (isBoolCondition(v)) return v as BoolCondition;
  return emptyBool();
}

export function isFilterEmpty(v: FilterConfig | null | undefined): boolean {
  if (!v) return true;
  if (!isBoolCondition(v)) return true;
  if (!v.conditions || v.conditions.length === 0) return true;
  return false;
}

/**
 * Collapse an emptied-out tree (`{combinator, conditions: []}` — e.g. the
 * user just deleted the last condition) to `null` ("no filter", pass-all).
 * The backend rejects empty condition lists, so every save/preview payload
 * must pass through this.
 */
export function nullIfEmptyFilter(
  v: FilterConfig | null | undefined,
): BoolCondition | null {
  return isFilterEmpty(v) ? null : (v as BoolCondition);
}

/** Walk the tree and return every leaf field condition (depth-first). */
export function collectFieldConditions(
  config: BoolCondition | null | undefined,
): FieldCondition[] {
  if (!config || !isBoolCondition(config)) return [];
  const out: FieldCondition[] = [];
  const walk = (node: BoolCondition) => {
    for (const c of node.conditions || []) {
      if (isBoolCondition(c)) walk(c);
      else if (isFieldCondition(c)) out.push(c);
    }
  };
  walk(config);
  return out;
}

/**
 * Leaves whose operator takes a value but the value is empty — the backend
 * rejects these at save time (422), so callers check before submitting.
 * No-value operators (is_empty / is_not_empty) are never invalid.
 */
export function findInvalidConditions(
  config: BoolCondition | null | undefined,
): FieldCondition[] {
  return collectFieldConditions(config).filter(
    (c) => !isNoValueOperator(c.operator) && isEmptyValue(c.value),
  );
}

/** Human-readable one-liner for a leaf condition, e.g. "Resolution Equals 1080p". */
export function describeCondition(cond: FieldCondition, t: TFunction): string {
  const field = t(`filter.${cond.field}` as never, { defaultValue: cond.field });
  const op = t(`filter.${cond.operator}` as never, { defaultValue: cond.operator });
  if (isNoValueOperator(cond.operator)) return `${field} ${op}`;
  const v = cond.value;
  const text = Array.isArray(v)
    ? v.join(', ')
    : typeof v === 'boolean'
      ? t(v ? 'filter.true' : 'filter.false')
      : String(v ?? '');
  return `${field} ${op} ${text}`.trim();
}
