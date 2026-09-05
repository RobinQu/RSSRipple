import { useState, useEffect, useCallback, useMemo } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import useDocumentTitle from '../hooks/useDocumentTitle';
import { ArrowLeft, GitMerge, Pencil, Plus, Trash2, X } from 'lucide-react';
import { App, Button, Empty, Modal, Radio, Spin, Tag, Typography } from 'antd';
import { collectionsApi } from '../api/collections';
import { worksApi } from '../api/works';
import { genreSlug } from '../constants/genres';
import { seasonLabel, seasonWorkInfo } from '../utils/season';
import CollectionFormModal from '../components/CollectionFormModal';
import AttachWorkModal from '../components/AttachWorkModal';
import Pagination from '../components/Pagination';
import type { Work, WorkCollection } from '../types';

const { Title, Text } = Typography;
const PAGE_SIZE = 20;

function getDisplayTitle(w: Work): string {
  return w.title_cn || w.title_en || w.original_title || '—';
}

/** Collection detail page (/collections/:id): header info + management
 * actions, paginated member-works table, untracked TMDB parts. */
export default function CollectionDetail() {
  const { t } = useTranslation();
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { message, modal } = App.useApp();

  const [collection, setCollection] = useState<WorkCollection | null>(null);
  const [loading, setLoading] = useState(true);
  const [works, setWorks] = useState<Work[]>([]);
  const [worksLoading, setWorksLoading] = useState(true);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [formOpen, setFormOpen] = useState(false);
  const [attachOpen, setAttachOpen] = useState(false);
  const [mergeOpen, setMergeOpen] = useState(false);

  useDocumentTitle(collection?.title_cn ?? t('collections.title'));

  const loadDetail = useCallback(async () => {
    if (!id) return;
    setLoading(true);
    try {
      const r = await collectionsApi.get(id, true);
      setCollection(r.success ? r.data : null);
    } finally {
      setLoading(false);
    }
  }, [id]);

  const loadWorks = useCallback(
    async (p: number) => {
      if (!id) return;
      setWorksLoading(true);
      try {
        const r = await collectionsApi.works(id, p, PAGE_SIZE);
        if (r.success) {
          setWorks(r.data);
          setTotal(r.meta?.total ?? 0);
        }
      } finally {
        setWorksLoading(false);
      }
    },
    [id],
  );

  // Deferred one tick so the loading setState stays out of the effect body
  // (react-hooks/set-state-in-effect).
  useEffect(() => {
    const timeout = setTimeout(loadDetail, 0);
    return () => clearTimeout(timeout);
  }, [loadDetail]);

  useEffect(() => {
    const timeout = setTimeout(() => loadWorks(page), 0);
    return () => clearTimeout(timeout);
  }, [page, loadWorks]);

  const reload = () => {
    loadDetail();
    loadWorks(page);
  };

  const sourceTag = (c: WorkCollection) => {
    const src = c.external_source || 'manual';
    const color =
      src === 'tmdb_collection'
        ? 'blue'
        : src === 'wikidata'
          ? 'purple'
          : src === 'series_group'
            ? 'green'
            : src === 'franchise_pack'
              ? 'orange'
              : 'default';
    return <Tag color={color}>{t(`collections.source_${src}`, src)}</Tag>;
  };

  const handleDelete = () => {
    if (!collection) return;
    modal.confirm({
      title: t('collections.deleteConfirm'),
      content: t('collections.deleteWarning'),
      okText: t('common.delete'),
      okButtonProps: { danger: true },
      cancelText: t('common.cancel'),
      onOk: async () => {
        const r = await collectionsApi.remove(collection.id);
        if (r.success) {
          message.success(t('collections.deleted'));
          navigate('/works?view=collections');
        } else {
          message.error(r.error?.message || t('collections.deleteFailed'));
        }
      },
    });
  };

  const handleDetach = (w: Work) => {
    if (!collection) return;
    modal.confirm({
      title: t('collections.detachConfirm', { title: getDisplayTitle(w) }),
      okText: t('common.confirm'),
      cancelText: t('common.cancel'),
      onOk: async () => {
        const workType = w.content_type === 'movie' ? 'movie' : 'series';
        const r = await collectionsApi.detachWork(collection.id, workType, w.id);
        if (r.success) {
          message.success(t('collections.detached'));
          loadDetail();
          // If the last row of a later page was removed, step back a page.
          if (works.length === 1 && page > 1) setPage(page - 1);
          else loadWorks(page);
        } else {
          message.error(r.error?.message || t('collections.detachFailed'));
        }
      },
    });
  };

  if (loading) return <Spin style={{ display: 'flex', justifyContent: 'center', padding: 48 }} />;
  if (!collection) return <Text type="danger">{t('collections.notFound')}</Text>;

  return (
    <div>
      {/* Header */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          flexWrap: 'wrap',
          gap: 8,
          marginBottom: 16,
        }}
      >
        <Button
          type="text"
          icon={<ArrowLeft size={18} />}
          onClick={() => navigate('/works?view=collections')}
        />
        <Title
          level={3}
          style={{ margin: 0, flex: '1 1 200px', minWidth: 0, wordBreak: 'break-word' }}
        >
          {collection.title_cn}
          {collection.title_en && (
            <Text type="secondary" style={{ fontSize: 16, fontWeight: 400, marginLeft: 8 }}>
              {collection.title_en}
            </Text>
          )}
        </Title>
        {sourceTag(collection)}
        <Button type="primary" icon={<Plus size={14} />} onClick={() => setAttachOpen(true)}>
          {t('collections.attach')}
        </Button>
        <Button icon={<GitMerge size={14} />} onClick={() => setMergeOpen(true)}>
          {t('collections.merge')}
        </Button>
        <Button icon={<Pencil size={14} />} onClick={() => setFormOpen(true)}>
          {t('common.edit')}
        </Button>
        <Button danger icon={<Trash2 size={14} />} onClick={handleDelete}>
          {t('common.delete')}
        </Button>
      </div>

      {collection.description && (
        <Text type="secondary" style={{ display: 'block', marginBottom: 12, fontSize: 13 }}>
          {collection.description}
        </Text>
      )}
      <Text type="secondary" style={{ display: 'block', marginBottom: 8, fontSize: 13 }}>
        {t('collections.colWorks')}: {total}
      </Text>

      {/* Member works */}
      {works.length === 0 && !worksLoading ? (
        <Empty
          description={t('collections.noWorks')}
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          style={{ marginTop: 48 }}
        />
      ) : (
        <>
          <Spin spinning={worksLoading}>
            <div className="resource-table-wrap" style={{ marginBottom: 16 }}>
              <table className="resource-table works-table">
                <colgroup>
                  <col style={{ width: 60 }} />
                  <col />
                  <col style={{ width: 84 }} />
                  <col style={{ width: 96 }} />
                  <col style={{ width: 116 }} />
                  <col style={{ width: 200 }} />
                  <col style={{ width: 72 }} />
                </colgroup>
                <thead>
                  <tr style={{ color: 'var(--rr-text-muted)', fontSize: 12 }}>
                    <th style={{ textAlign: 'left', padding: '8px' }}>{t('works.colType')}</th>
                    <th style={{ textAlign: 'left', padding: '8px' }}>{t('works.colTitle')}</th>
                    <th style={{ textAlign: 'left', padding: '8px' }}>{t('works.colRating')}</th>
                    <th style={{ textAlign: 'left', padding: '8px' }}>{t('works.colReleaseDate')}</th>
                    <th style={{ textAlign: 'left', padding: '8px' }}>{t('works.colInfo')}</th>
                    <th style={{ textAlign: 'left', padding: '8px' }}>{t('works.colGenre')}</th>
                    <th style={{ textAlign: 'left', padding: '8px' }}>{t('common.operation')}</th>
                  </tr>
                </thead>
                <tbody>
                  {works.map((w) => {
                    const displayTitle = getDisplayTitle(w);
                    const info =
                      w.content_type === 'movie'
                        ? (w.year ?? '—')
                        : seasonWorkInfo(t, w.season_number ?? null, w.number_of_episodes ?? null) || '—';
                    return (
                      <tr
                        key={w.id}
                        className="resource-row works-row"
                        style={{ borderTop: '1px solid var(--rr-border-soft)', cursor: 'pointer' }}
                        onClick={() =>
                          navigate(w.content_type === 'movie' ? `/movies/${w.id}` : `/series/${w.id}`)
                        }
                      >
                        <td style={{ padding: '8px' }} data-label={t('works.colType')}>
                          <Tag
                            color={w.content_type === 'movie' ? 'green' : 'blue'}
                            style={{ margin: 0 }}
                          >
                            {w.content_type === 'movie' ? t('works.movie') : t('works.tv')}
                          </Tag>
                        </td>
                        <td
                          className="resource-title-cell"
                          style={{ padding: '8px' }}
                          data-label={t('works.colTitle')}
                        >
                          <Text ellipsis={{ tooltip: displayTitle }} style={{ fontWeight: 600 }}>
                            {displayTitle}
                          </Text>
                        </td>
                        <td style={{ padding: '8px' }} data-label={t('works.colRating')}>
                          {w.rating != null ? `★ ${w.rating.toFixed(1)}` : '—'}
                        </td>
                        <td style={{ padding: '8px', whiteSpace: 'nowrap' }} data-label={t('works.colReleaseDate')}>
                          {w.release_date || '—'}
                        </td>
                        <td
                          style={{ padding: '8px', whiteSpace: 'nowrap' }}
                          data-label={t('works.colInfo')}
                        >
                          {info}
                        </td>
                        <td
                          className="resource-text-cell"
                          style={{ padding: '8px' }}
                          data-label={t('works.colGenre')}
                        >
                          <Text ellipsis style={{ display: 'block' }}>
                            {w.genre && w.genre.length
                              ? w.genre.map((g) => t(`genre.${genreSlug(g)}`, { defaultValue: g })).join(', ')
                              : '—'}
                          </Text>
                        </td>
                        <td
                          style={{ padding: '8px' }}
                          data-label={t('common.operation')}
                          onClick={(e) => e.stopPropagation()}
                        >
                          <Button
                            type="text"
                            size="small"
                            icon={<X size={13} />}
                            title={t('collections.detach')}
                            onClick={() => handleDetach(w)}
                          />
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </Spin>

          <div style={{ display: 'flex', justifyContent: 'center', padding: '8px 0' }}>
            <Pagination page={page} pageSize={PAGE_SIZE} total={total} onPageChange={setPage} />
          </div>
        </>
      )}

      {/* Untracked TMDB collection parts */}
      {collection.untracked_parts && collection.untracked_parts.length > 0 && (
        <div style={{ marginTop: 16 }}>
          <Text strong style={{ fontSize: 13, display: 'block', marginBottom: 8 }}>
            {t('collections.untrackedParts')}
          </Text>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
            {collection.untracked_parts.map((p) => (
              <Tag key={p.tmdb_id} style={{ margin: 0, opacity: 0.55 }}>
                {p.title}
                {p.year ? ` (${p.year})` : ''} · {t('collections.untracked')}
              </Tag>
            ))}
          </div>
        </div>
      )}

      <CollectionFormModal
        open={formOpen}
        collection={collection}
        onClose={() => setFormOpen(false)}
        onSaved={loadDetail}
      />
      <AttachWorkModal
        open={attachOpen}
        collectionId={collection.id}
        onClose={() => setAttachOpen(false)}
        onAttached={reload}
      />
      <MergeWorksModal
        open={mergeOpen}
        collectionId={collection.id}
        onClose={() => setMergeOpen(false)}
        onMerged={reload}
      />
    </div>
  );
}

/** 「合并作品」弹窗（per-season works 的常规修复工具）：列出同季重复的
 * 季作品分组，选定保留作品后先以 confirm=false 试调（后端 422 返回不可逆
 * 警告文案），弹确认框后再以 confirm=true 提交。 */
function MergeWorksModal({
  open,
  collectionId,
  onClose,
  onMerged,
}: {
  open: boolean;
  collectionId: string;
  onClose: () => void;
  onMerged: () => void;
}) {
  const { t } = useTranslation();
  const { message, modal } = App.useApp();
  const [loading, setLoading] = useState(false);
  const [works, setWorks] = useState<Work[]>([]);
  // Survivor pick per season group (keyed by season_number).
  const [survivorBySeason, setSurvivorBySeason] = useState<Record<number, string>>({});
  const [merging, setMerging] = useState(false);

  useEffect(() => {
    if (!open) return;
    setSurvivorBySeason({});
    setLoading(true);
    collectionsApi
      .works(collectionId, 1, 100)
      .then((r) => {
        if (r.success) setWorks(r.data);
      })
      .finally(() => setLoading(false));
  }, [open, collectionId]);

  // Only same-season series groups with 2+ members are merge candidates
  // (the server rejects cross-season merges — a work IS one season).
  const groups = useMemo(() => {
    const bySeason = new Map<number, Work[]>();
    for (const w of works) {
      if (w.content_type !== 'tv' || w.season_number == null) continue;
      const arr = bySeason.get(w.season_number) ?? [];
      arr.push(w);
      bySeason.set(w.season_number, arr);
    }
    return [...bySeason.entries()]
      .filter(([, arr]) => arr.length > 1)
      .sort((a, b) => a[0] - b[0]);
  }, [works]);

  const doMerge = async (season: number, members: Work[]) => {
    const survivorId = survivorBySeason[season] ?? members[0]?.id;
    if (!survivorId) return;
    const duplicateIds = members.map((w) => w.id).filter((wid) => wid !== survivorId);
    setMerging(true);
    try {
      const probe = await worksApi.merge({
        survivor_type: 'series',
        survivor_id: survivorId,
        duplicate_ids: duplicateIds,
        confirm: false,
      });
      if (probe.success) return; // unexpected without confirm — nothing to do
      if (probe.error?.code !== 'VALIDATION_ERROR') {
        message.error(probe.error?.message || t('collections.mergeFailed'));
        return;
      }
      modal.confirm({
        title: t('collections.mergeTitle'),
        content: probe.error.message,
        okText: t('collections.mergeConfirm'),
        okButtonProps: { danger: true },
        cancelText: t('common.cancel'),
        onOk: async () => {
          const r = await worksApi.merge({
            survivor_type: 'series',
            survivor_id: survivorId,
            duplicate_ids: duplicateIds,
            confirm: true,
          });
          if (r.success) {
            message.success(t('collections.mergeDone', { n: r.data.merged }));
            onMerged();
            onClose();
          } else {
            message.error(r.error?.message || t('collections.mergeFailed'));
          }
        },
      });
    } finally {
      setMerging(false);
    }
  };

  return (
    <Modal
      open={open}
      title={t('collections.mergeTitle')}
      footer={null}
      onCancel={onClose}
      destroyOnHidden
      width={560}
    >
      <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 12 }}>
        {t('collections.mergeHint')}
      </Text>
      <Spin spinning={loading}>
        {groups.length === 0 && !loading ? (
          <Empty
            description={t('collections.mergeNoDuplicates')}
            image={Empty.PRESENTED_IMAGE_SIMPLE}
          />
        ) : (
          groups.map(([season, members]) => {
            const survivorId = survivorBySeason[season] ?? members[0]?.id;
            return (
              <div
                key={season}
                style={{
                  border: '1px solid var(--rr-border-soft)',
                  borderRadius: 8,
                  padding: '8px 12px',
                  marginBottom: 10,
                }}
              >
                <Text strong style={{ fontSize: 13, display: 'block', marginBottom: 6 }}>
                  {seasonLabel(t, season)}
                </Text>
                <Radio.Group
                  value={survivorId}
                  onChange={(e) =>
                    setSurvivorBySeason((prev) => ({ ...prev, [season]: e.target.value as string }))
                  }
                  style={{ display: 'flex', flexDirection: 'column', gap: 4 }}
                >
                  {members.map((w) => (
                    <Radio key={w.id} value={w.id}>
                      <Text style={{ fontSize: 12 }}>
                        {getDisplayTitle(w)}
                        {w.year ? ` (${w.year})` : ''}
                      </Text>
                      <Text type="secondary" style={{ fontSize: 11, marginLeft: 6 }}>
                        {survivorId === w.id ? t('collections.mergeSurvivor') : t('collections.mergeDuplicates')}
                      </Text>
                    </Radio>
                  ))}
                </Radio.Group>
                <div style={{ textAlign: 'right', marginTop: 8 }}>
                  <Button
                    size="small"
                    type="primary"
                    danger
                    loading={merging}
                    onClick={() => void doMerge(season, members)}
                  >
                    {t('collections.mergeConfirm')}
                  </Button>
                </div>
              </div>
            );
          })
        )}
      </Spin>
    </Modal>
  );
}
