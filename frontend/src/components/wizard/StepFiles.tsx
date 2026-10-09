import {
  Alert,
  Button,
  Checkbox,
  Divider,
  Empty,
  InputNumber,
  Select,
  Spin,
  Tag,
  Typography,
} from 'antd';
import { ChevronDown, ChevronRight, Plus, RefreshCw, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { seasonLabel } from '../../utils/season';
import { workKeyOf } from '../wizardValidation';
import SeasonInput from '../SeasonInput';
import { LabeledRow, PoolFileRow } from './PoolFileRow';
import type { ResourceEditWizardState } from './useResourceEditWizard';

const { Text } = Typography;

/** Step ② — file mapping (left: selectable work list; right: candidate files
 * grouped by work_title_hint cluster — a cluster-level work pick expands to
 * every member file, expanding a cluster exposes per-file tweaks and the
 * shift-range/drag multi-select join into the selected work — the season
 * comes from the work's own season_number, S/E prefilled from the
 * deterministic name parses). */
export default function StepFiles({
  wiz,
  alert,
  compact,
}: {
  wiz: ResourceEditWizardState;
  alert: React.ReactNode;
  compact: boolean;
}) {
  const { t } = useTranslation();
  const {
    audioLinked,
    isBatch,
    works,
    epSeason,
    setEpSeason,
    epEpisode,
    setEpEpisode,
    epAbsolute,
    setEpAbsolute,
    files,
    filesSource,
    detail,
    filesRetrying,
    retryFetchFiles,
    analyzing,
    analysisStatus,
    analysisOutput,
    analyze,
    workTitles,
    workSeasons,
    setWorkSeasons,
    selectedWorkKey,
    setSelectedWorkKey,
    sortedEntriesFor,
    checkedAssign,
    setCheckedAssign,
    batchEpStart,
    setBatchEpStart,
    applyBatchEpisodeIncrement,
    setPlacementField,
    unassignPaths,
    removeWork,
    setPickerOpen,
    joinSeason,
    setJoinSeason,
    joinChecked,
    checkedFiles,
    suggestion,
    poolFiles,
    poolRows,
    expandedClusters,
    toggleCluster,
    clusterBinding,
    clusterRangePreview,
    pickClusterWork,
    placements,
    detParses,
    clusterGroups,
    beginPointerSelect,
    extendTouchSelect,
    extendDragSelect,
    toggleFileChecked,
  } = wiz;

  return (
    <>
      {alert}
      {audioLinked ? (
        <Alert type="info" showIcon message={t('resource.audioLinkHint')} />
      ) : !isBatch ? (
        <>
          {(!works.length || works[0]?.work_type === 'series') ? (
            <>
              <LabeledRow label={t('resource.seasonLabel')}>
                <SeasonInput value={epSeason} onChange={setEpSeason} style={{ width: '100%' }} />
              </LabeledRow>
              <LabeledRow label={t('resource.episodePerSeasonLabel')}>
                <InputNumber min={0} value={epEpisode} onChange={(v) => setEpEpisode(typeof v === 'number' ? v : null)} style={{ width: '100%' }} />
              </LabeledRow>
              <LabeledRow label={t('resource.absoluteEpisodePlaceholder')}>
                <InputNumber min={0} value={epAbsolute} onChange={(v) => setEpAbsolute(typeof v === 'number' ? v : null)} style={{ width: '100%' }} />
              </LabeledRow>
            </>
          ) : (
            <Text type="secondary" style={{ fontSize: 12 }}>
              {t('resource.noEpisodeFields')}
            </Text>
          )}
        </>
      ) : files.length === 0 ? (
        filesSource === 'none' && detail?.torrent_url?.startsWith('http') ? (
          <Alert
            type="warning"
            showIcon
            message={t('resource.torrentFetchFailed')}
            action={
              <Button size="small" loading={filesRetrying} onClick={retryFetchFiles}>
                {t('common.retry')}
              </Button>
            }
          />
        ) : (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('resource.noListingHint')} />
        )
      ) : analyzing ? (
        <div style={{ minHeight: 420, display: 'flex', flexDirection: 'column', justifyContent: 'center', padding: 24 }}>
          <Spin size="large" />
          <Text strong style={{ textAlign: 'center', marginTop: 16 }}>{analysisStatus}</Text>
          <pre style={{ marginTop: 16, maxHeight: 360, overflow: 'auto', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', padding: 16, borderRadius: 8, background: 'var(--rr-surface-card)', fontSize: 12 }}>
            {analysisOutput || t('resource.analysisWaiting')}
          </pre>
        </div>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: compact ? '1fr' : 'minmax(380px, 42%) minmax(0, 1fr)', gap: 12, height: '100%', minHeight: 0, overflow: compact ? 'auto' : 'hidden' }}>
          {/* Left: works */}
          <div style={{ border: '1px solid var(--rr-border-soft)', borderRadius: 8, padding: 8 }}>
            <Text strong style={{ fontSize: 12 }}>{t('resource.stepWorkType')}</Text>
            <div style={{ marginTop: 6, display: 'flex', flexDirection: 'column', gap: 4 }}>
              {works.map((w) => {
                const key = workKeyOf(w.work_type, w.work_id);
                const entries = sortedEntriesFor(key);
                const checkedSet = new Set(checkedAssign);
                const checkedHere = entries.filter(([path]) => checkedSet.has(path));
                const allChecked = entries.length > 0 && checkedHere.length === entries.length;
                const count = entries.length;
                const active = selectedWorkKey === key;
                return (
                  <div key={key}>
                    <div
                      role="button"
                      tabIndex={0}
                      aria-pressed={active}
                      onClick={() => {
                        setSelectedWorkKey(active ? null : key);
                        if (!active) setCheckedAssign([]);
                      }}
                      onKeyDown={(e) => {
                        // Only when the row itself is focused — nested inputs
                        // (season field, remove button) handle their own keys.
                        if (e.target !== e.currentTarget) return;
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault();
                          setSelectedWorkKey(active ? null : key);
                          if (!active) setCheckedAssign([]);
                        }
                      }}
                      style={{
                        display: 'flex',
                        alignItems: 'flex-start',
                        gap: 6,
                        padding: '5px 7px',
                        borderRadius: 6,
                        cursor: 'pointer',
                        border: `1px solid ${active ? 'var(--rr-primary)' : 'var(--rr-border-soft)'}`,
                        background: active ? 'var(--rr-primary-soft)' : 'transparent',
                      }}
                    >
                      <span style={{ flex: 1, minWidth: 0 }}>
                        <span style={{ fontSize: 12, display: 'block', overflowWrap: 'anywhere', wordBreak: 'break-all', lineHeight: 1.4 }}>
                          {workTitles[key] || w.work_id}
                        </span>
                        <Text type="secondary" style={{ fontSize: 11 }}>
                          {w.work_type === 'series' ? t('works.tv') : t('works.movie')} · {count}
                        </Text>
                      </span>
                      {/* Per-season works: the season comes from the work
                          itself; only unknown seasons (candidates, legacy
                          rows) get a per-work input. */}
                      {w.work_type === 'series' &&
                        (workSeasons[key] != null ? (
                          <Tag style={{ fontSize: 11, margin: 0, flexShrink: 0 }}>
                            {seasonLabel(t, workSeasons[key])}
                          </Tag>
                        ) : (
                          <SeasonInput
                            size="small"
                            value={workSeasons[key] ?? null}
                            onChange={(v) =>
                              setWorkSeasons((prev) => ({ ...prev, [key]: v }))
                            }
                            placeholder={t('resource.workSeasonUnknown')}
                            style={{ width: 96, flexShrink: 0 }}
                          />
                        ))}
                      <Button
                        size="small"
                        type="text"
                        icon={<X size={12} />}
                        aria-label={t('common.remove')}
                        onClick={(e) => {
                          e.stopPropagation();
                          removeWork(key);
                        }}
                      />
                    </div>
                    {active && w.work_type === 'series' && entries.length > 0 && (
                      <div style={{ padding: '4px 2px 2px 10px' }}>
                        <div
                          style={{
                            display: 'flex',
                            flexWrap: 'wrap',
                            gap: 6,
                            alignItems: 'center',
                            padding: '2px 0 6px',
                            borderBottom: '1px dashed var(--rr-border-soft)',
                            marginBottom: 4,
                          }}
                        >
                          <Text type="secondary" style={{ fontSize: 11 }}>{t('resource.batchToolbarHint')}</Text>
                          <InputNumber
                            size="small"
                            min={0}
                            value={batchEpStart}
                            onChange={(v) => setBatchEpStart(typeof v === 'number' ? v : null)}
                            style={{ width: 72 }}
                            controls={false}
                            placeholder={t('resource.epStartCol')}
                          />
                          <Button
                            size="small"
                            disabled={batchEpStart == null || checkedHere.length === 0}
                            onClick={applyBatchEpisodeIncrement}
                          >
                            {t('resource.fillEpisodesIncrement', { count: checkedHere.length })}
                          </Button>
                        </div>
                        <div
                          style={{
                            display: 'flex',
                            gap: 4,
                            alignItems: 'center',
                            borderBottom: '1px solid var(--rr-border)',
                            paddingBottom: 2,
                            marginBottom: 2,
                          }}
                        >
                          <Checkbox
                            checked={allChecked}
                            indeterminate={checkedHere.length > 0 && !allChecked}
                            onChange={(e) =>
                              setCheckedAssign(e.target.checked ? entries.map(([path]) => path) : [])
                            }
                          />
                          <Text type="secondary" style={{ fontSize: 10, flex: 1 }}>{t('resource.fileColName')}</Text>
                          <Text type="secondary" style={{ fontSize: 10, width: 52, flexShrink: 0, textAlign: 'center' }}>{t('resource.seasonLabel')}</Text>
                          <Text type="secondary" style={{ fontSize: 10, width: 52, flexShrink: 0, textAlign: 'center' }}>{t('resource.epStartCol')}</Text>
                          <Text type="secondary" style={{ fontSize: 10, width: 52, flexShrink: 0, textAlign: 'center' }}>{t('resource.epEndCol')}</Text>
                          <span style={{ width: 22, flexShrink: 0 }} />
                        </div>
                        <div style={{ maxHeight: compact ? 220 : 300, overflowY: 'auto' }}>
                          {entries.map(([path, p]) => (
                            <div key={path} style={{ display: 'flex', gap: 4, alignItems: 'flex-start', padding: '1px 0' }}>
                              <Checkbox
                                style={{ marginTop: 4 }}
                                checked={checkedSet.has(path)}
                                onChange={(e) =>
                                  setCheckedAssign((prev) =>
                                    e.target.checked
                                      ? [...prev, path]
                                      : prev.filter((x) => x !== path),
                                  )
                                }
                              />
                              <span
                                title={path}
                                style={{ fontSize: 11, flex: 1, minWidth: 0, overflowWrap: 'anywhere', wordBreak: 'break-all', lineHeight: 1.4, marginTop: 3 }}
                              >
                                {path.split('/').pop()}
                              </span>
                              {/* Season is fixed by the target work
                                  (per-season works) — read-only here. */}
                              <span style={{ width: 52, flexShrink: 0, textAlign: 'center', fontSize: 11, marginTop: 3 }}>
                                {p.season != null ? `S${p.season}` : '—'}
                              </span>
                              <InputNumber size="small" min={0} value={p.epStart} onChange={(v) => setPlacementField(path, { epStart: typeof v === 'number' ? v : null })} style={{ width: 52, flexShrink: 0 }} controls={false} />
                              <InputNumber size="small" min={0} value={p.epEnd} onChange={(v) => setPlacementField(path, { epEnd: typeof v === 'number' ? v : null })} style={{ width: 52, flexShrink: 0 }} controls={false} />
                              <Button size="small" type="text" icon={<X size={11} />} aria-label={t('resource.removeMapping')} onClick={() => unassignPaths([path])} />
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
              <Button
                size="small"
                icon={<Plus size={13} />}
                onClick={() => setPickerOpen(true)}
              >
                {t('resource.addWork')}
              </Button>
            </div>
          </div>

          {/* Right: unassigned candidate files only — assigned files live
              under the selected work on the left. */}
          <div style={{ border: '1px solid var(--rr-border-soft)', borderRadius: 8, padding: 8, minWidth: 0, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center', marginBottom: 6 }}>
              <Button
                size="small"
                icon={<RefreshCw size={12} />}
                loading={analyzing}
                onClick={() => void analyze(true)}
              >
                {t('resource.reanalyze')}
              </Button>
              <Divider type="vertical" />
              {/* Season join fallback — only when the selected season
                  work's season_number is unknown (candidate/legacy);
                  otherwise the work itself carries the season. */}
              {selectedWorkKey?.startsWith('series:') &&
                workSeasons[selectedWorkKey] == null && (
                  <SeasonInput
                    size="small"
                    value={joinSeason}
                    onChange={setJoinSeason}
                    addonBefore={t('resource.seasonLabel')}
                    style={{ width: 130 }}
                  />
                )}
              <Button
                size="small"
                type="primary"
                disabled={!selectedWorkKey || checkedFiles.length === 0}
                onClick={joinChecked}
              >
                {t('resource.joinToWork')}（{checkedFiles.length}）
              </Button>
            </div>
            {suggestion && (
              <div style={{ marginBottom: 4 }}>
                <Text type="success" style={{ fontSize: 11 }}>
                  {t('resource.analyzeSummary', {
                    parsed: suggestion.deterministic.files.filter((f) => f.episode != null).length,
                    total: suggestion.deterministic.files.length,
                  })}
                  {suggestion.works.length > 0 ? ` · ${t('resource.llmSuggestionReady')}` : ''}
                </Text>
              </div>
            )}
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', padding: '2px 4px', borderBottom: '1px solid var(--rr-border)' }}>
              <Text type="secondary" style={{ fontSize: 11, flex: 1 }}>{t('resource.fileColName')}</Text>
              <Text type="secondary" style={{ fontSize: 11, width: 70, flexShrink: 0 }}>{t('resource.parsedSE')}</Text>
              <Text type="secondary" style={{ fontSize: 11, width: 64, flexShrink: 0, textAlign: 'right' }}>{t('resource.fileColSize')}</Text>
            </div>
            <div
              style={{ flex: compact ? undefined : 1, minHeight: 0, maxHeight: compact ? 360 : undefined, overflowY: 'auto', userSelect: 'none', touchAction: 'none' }}
              onPointerMove={extendTouchSelect}
            >
              {poolFiles.length === 0 ? (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t('resource.allMappedHint')} style={{ margin: 12 }} />
              ) : (
                // Cluster-level rows: one header per work_title_hint group
                // (files without a hint fall into the trailing "other"
                // group); expanding a cluster reveals its per-file rows.
                poolRows.map((row) => {
                  if (row.kind === 'header') {
                    const g = row.group;
                    const expanded = expandedClusters.has(g.key);
                    const binding = clusterBinding(g.allPaths);
                    const range = clusterRangePreview(g.allPaths);
                    return (
                      <div
                        key={`h-${g.key}`}
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          gap: 6,
                          padding: '4px 4px',
                          borderBottom: '1px solid var(--rr-border-soft)',
                          background: 'var(--rr-surface-card)',
                        }}
                      >
                        <Button
                          size="small"
                          type="text"
                          icon={expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                          aria-label={expanded ? t('common.collapse') : t('common.expand')}
                          onClick={() => toggleCluster(g.key)}
                        />
                        <span style={{ fontSize: 12, fontWeight: 600, overflowWrap: 'anywhere', wordBreak: 'break-all', lineHeight: 1.4 }}>
                          {g.title || t('resource.otherFilesGroup')}
                        </span>
                        <Text type="secondary" style={{ fontSize: 11, flexShrink: 0 }}>
                          {t('resource.clusterFileCount', { count: g.allPaths.length })}
                          {range ? ` · ${range}` : ''}
                        </Text>
                        <span style={{ flex: 1 }} />
                        {binding === 'mixed' ? (
                          <Tag color="orange" style={{ fontSize: 10, margin: 0 }}>{t('resource.clusterMixed')}</Tag>
                        ) : binding ? (
                          <Tag color="blue" style={{ fontSize: 10, margin: 0 }}>{workTitles[binding] || binding}</Tag>
                        ) : g.title ? (
                          <Tag style={{ fontSize: 10, margin: 0 }}>{t('resource.clusterUnbound')}</Tag>
                        ) : null}
                        {g.title && (
                          <Select
                            size="small"
                            style={{ width: 180, flexShrink: 0 }}
                            placeholder={t('resource.clusterApplyWork')}
                            value={binding && binding !== 'mixed' ? binding : undefined}
                            onChange={(v) => pickClusterWork(g, v)}
                            options={[
                              ...works.map((w) => {
                                const key = workKeyOf(w.work_type, w.work_id);
                                const sLabel =
                                  w.work_type === 'series' ? ` · ${seasonLabel(t, workSeasons[key])}` : '';
                                return {
                                  value: key,
                                  label: `${workTitles[key] || w.work_id}${sLabel}`,
                                };
                              }),
                              { value: '__new__', label: t('resource.pickNewWork') },
                            ]}
                          />
                        )}
                      </div>
                    );
                  }
                  if (row.kind === 'assigned') {
                    // Assigned cluster members surface inside the expanded
                    // cluster too, so single-file tweaks (unassign back to
                    // the pool) stay in the cluster context.
                    const p = placements[row.path];
                    const key = workKeyOf(p.workType, p.workId);
                    return (
                      <div
                        key={`a-${row.path}`}
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          gap: 8,
                          padding: '3px 4px 3px 26px',
                          borderBottom: '1px solid var(--rr-border-soft)',
                        }}
                      >
                        <span style={{ flex: 1, minWidth: 0, fontSize: 12, overflowWrap: 'anywhere', wordBreak: 'break-all', lineHeight: 1.4 }}>
                          {row.path}
                        </span>
                        <Tag color="blue" style={{ fontSize: 10, margin: 0 }}>
                          {workTitles[key] || key}
                          {p.season != null ? ` S${p.season}` : ''}
                          {p.epStart != null ? ` E${p.epStart}` : ''}
                        </Tag>
                        <Button size="small" type="text" icon={<X size={11} />} aria-label={t('resource.removeMapping')} onClick={() => unassignPaths([row.path])} />
                      </div>
                    );
                  }
                  return (
                    <PoolFileRow
                      key={row.file.name}
                      name={row.file.name}
                      size={row.file.size}
                      idx={row.idx}
                      checked={checkedFiles.includes(row.file.name)}
                      parsed={detParses[row.file.name]}
                      indented={clusterGroups.length > 1 || clusterGroups[0]?.title != null}
                      onPointerDown={beginPointerSelect}
                      onPointerEnter={extendDragSelect}
                      onToggle={toggleFileChecked}
                    />
                  );
                })
              )}
            </div>
            <div style={{ marginTop: 4 }}>
              <Text type="secondary" style={{ fontSize: 11 }}>{t('resource.dragSelectHint')}</Text>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
