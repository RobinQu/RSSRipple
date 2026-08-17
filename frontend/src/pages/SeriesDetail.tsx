import { useState, useEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import useDocumentTitle from '../hooks/useDocumentTitle';
import { useParams, Link, useNavigate } from 'react-router-dom';
import { ArrowLeft, Trash2, RefreshCw, Download, Pencil } from 'lucide-react';
import {
  Typography, Spin, Card, Button, Tag, Descriptions,
  Row, Col, Statistic, Table, Modal, App, Checkbox,
} from 'antd';
import type { TableColumnsType } from 'antd';
import { seriesApi } from '../api/series';
import { worksApi } from '../api/works';
import type { TVSeries, Episode, FileResource } from '../types';
import { timeAgo } from '../utils/format';
import { withMobileLabels } from '../utils/table';
import { posterUrl, useDefaultPoster } from '../utils/poster';
import CreateTaskModal from '../components/CreateTaskModal';
import CollectionSiblingsCard from '../components/CollectionSiblingsCard';
import WorkEditModal from '../components/WorkEditModal';

const { Title, Text, Link: AntdLink } = Typography;

export default function SeriesDetail() {
  const { t } = useTranslation();
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { message } = App.useApp();
  const [series, setSeries] = useState<TVSeries | null>(null);
  useDocumentTitle(series ? series.title_cn || series.title_en || series.original_title : t('series.title'));
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [createTaskResourceId, setCreateTaskResourceId] = useState<string | null>(null);
  const [editOpen, setEditOpen] = useState(false);
  const [refreshOpen, setRefreshOpen] = useState(false);
  const [overrideManualEdits, setOverrideManualEdits] = useState(false);

  const loadSeries = useCallback(async () => {
    if (!id) return;
    const r = await seriesApi.get(id);
    if (r.success) setSeries(r.data);
  }, [id]);

  useEffect(() => {
    if (!id) return;
    let active = true;
    seriesApi
      .get(id)
      .then((r) => {
        if (active && r.success) setSeries(r.data);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [id]);

  const openRefreshDialog = () => {
    setOverrideManualEdits(false);
    setRefreshOpen(true);
  };

  const handleRefreshMetadata = async () => {
    if (!id) return;
    setRefreshOpen(false);
    setRefreshing(true);
    try {
      const r = await worksApi.refreshMetadata(id, 'tv', null, overrideManualEdits);
      if (r.success) {
        const filled = r.data.filled?.length ?? 0;
        message.success(
          filled > 0
            ? t('works.refreshFilled', { n: filled })
            : t('works.refreshNoChange'),
        );
        await loadSeries();
      } else {
        message.error(r.error?.message || t('works.refreshFailed'));
      }
    } finally {
      setRefreshing(false);
    }
  };

  const handleDelete = () => {
    if (!series) return;
    const agentWorkCount = series.agent_work_count ?? 0;

    Modal.confirm({
      title: t('series.deleteConfirm'),
      icon: null,
      content: (
        <div>
          <p>{t('series.deleteWarning')}</p>
          {agentWorkCount > 0 && (
            <div
              style={{
                marginTop: 12,
                padding: '10px 14px',
                borderRadius: 6,
                background: '#2a1a1a',
                border: '1px solid #6b3434',
                color: '#e88a8a',
                fontSize: 13,
              }}
            >
              {t('series.deleteBlockedByAgents', { n: agentWorkCount })}
            </div>
          )}
        </div>
      ),
      okText: t('common.delete'),
      okType: 'danger',
      cancelText: t('common.cancel'),
      onOk: async () => {
        const r = await seriesApi.delete(id!);
        if (r.success) {
          message.success(t('series.deleted'));
          navigate('/works');
        } else if (r.error?.code === 'DELETE_BLOCKED') {
          message.error(r.error?.message || t('series.deleteBlocked'));
        } else {
          message.error(r.error?.message || t('series.deleteFailed'));
        }
      },
    });
  };

  const episodeColumns: TableColumnsType<Episode> = [
    { title: t('series.season'), dataIndex: 'season', key: 'season', width: 60 },
    { title: t('series.episode'), dataIndex: 'episode', key: 'episode', width: 60 },
    { title: t('series.name'), dataIndex: 'title', key: 'title', render: (v: string | null) => v || '—' },
    { title: t('series.airDate'), dataIndex: 'air_date', key: 'air_date', width: 130, render: (v: string | null) => v || '—' },
  ];

  const resourceColumns: TableColumnsType<FileResource> = [
    { title: t('series.name'), dataIndex: 'title_raw', key: 'title', ellipsis: true },
    { title: t('series.resolution'), dataIndex: 'resolution', key: 'resolution', width: 100, render: (v: string | null) => v ? <Tag>{v}</Tag> : '—' },
    { title: t('series.subtitleGroup'), dataIndex: 'subtitle_group', key: 'subtitle_group', width: 140, render: (v: string | null) => v || '—' },
    { title: t('series.publishedAt'), dataIndex: 'published_at', key: 'published_at', width: 160, render: (v: string | null) => (v ? timeAgo(v) : '—') },
    {
      title: t('common.actions'),
      key: 'actions',
      width: 150,
      render: (_: unknown, record: FileResource) => (
        <Button
          size="small"
          icon={<Download size={12} />}
          onClick={() => setCreateTaskResourceId(record.id)}
        >
          {t('tasks.createTask')}
        </Button>
      ),
    },
  ];

  if (loading) return <Spin style={{ display: 'flex', justifyContent: 'center', padding: 48 }} />;
  if (!series) return <Text type="danger">{t('series.notFound')}</Text>;

  const sourceLinks = series.source_links ?? [];

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 8, marginBottom: 24 }}>
        <Link to="/works">
          <Button type="text" icon={<ArrowLeft size={18} />} />
        </Link>
        <Title level={3} style={{ margin: 0, flex: '1 1 200px', minWidth: 0, wordBreak: 'break-word' }}>
          {series.title_cn || series.title_en || series.original_title}
        </Title>
        <Tag color="blue">{t('series.title')}</Tag>
        <Button
          icon={<Pencil size={14} />}
          onClick={() => setEditOpen(true)}
        >
          {t('common.edit')}
        </Button>
        <Button
          icon={<RefreshCw size={14} />}
          loading={refreshing}
          onClick={openRefreshDialog}
        >
          {t('works.refreshMetadata')}
        </Button>
        <Button
          type="default"
          danger
          icon={<Trash2 size={14} />}
          onClick={handleDelete}
        >
          {t('common.delete')}
        </Button>
      </div>

      {/* Metadata card */}
      <Card style={{ marginBottom: 16 }}>
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 16, flexWrap: 'wrap' }}>
          <img
            src={posterUrl(series.poster_url)}
            alt=""
            style={{ width: 160, height: 240, objectFit: 'cover', borderRadius: 8, flexShrink: 0 }}
            onError={useDefaultPoster}
          />
          <div style={{ flex: '1 1 260px', minWidth: 0 }}>
            <Descriptions column={1} size="small">
              <Descriptions.Item label={t('series.cnTitle')}>{series.title_cn || '—'}</Descriptions.Item>
              <Descriptions.Item label={t('series.enTitle')}>{series.title_en || '—'}</Descriptions.Item>
              <Descriptions.Item label={t('series.originalTitle')}>{series.original_title || '—'}</Descriptions.Item>
              <Descriptions.Item label={t('series.rating')}>{series.rating ?? '—'}</Descriptions.Item>
              <Descriptions.Item label={t('common.status')}>{series.status || '—'}</Descriptions.Item>
              <Descriptions.Item label={t('works.animeStatus')}>
                {series.is_anime === true ? (
                  <Tag color="magenta">{t('works.anime')}</Tag>
                ) : series.is_anime === false ? (
                  <Tag>{t('works.liveAction')}</Tag>
                ) : (
                  t('common.unknown')
                )}
              </Descriptions.Item>
              {series.collection && (
                <Descriptions.Item label={t('works.colCollection')}>
                  <Link to={`/collections/${series.collection.id}`}>{series.collection.name}</Link>
                </Descriptions.Item>
              )}
              <Descriptions.Item label={t('works.sourceLinks')}>
                {sourceLinks.length > 0
                  ? sourceLinks.map((l, i) => (
                      <span key={l.url}>
                        {i > 0 && ' · '}
                        <AntdLink href={l.url} target="_blank" rel="noreferrer">{l.label}</AntdLink>
                      </span>
                    ))
                  : '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t('series.seasonsEpisodes')}>
                {series.number_of_seasons ? `${series.number_of_seasons}${t('series.season')} ${series.number_of_episodes || '?'}${t('series.episode')}` : '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t('series.startDate')}>{series.start_date || '—'}</Descriptions.Item>
              <Descriptions.Item label={t('series.endDate')}>{series.end_date || '—'}</Descriptions.Item>
              <Descriptions.Item label={t('series.updatedAt')}>{timeAgo(series.updated_at)}</Descriptions.Item>
            </Descriptions>
            {series.description && (
              <Text style={{ display: 'block', marginTop: 12, color: 'var(--rr-text-muted)' }}>
                {series.description}
              </Text>
            )}
          </div>
        </div>
      </Card>

      {/* Stats */}
      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={24} sm={8}>
          <Card size="small">
            <Statistic title={t('series.resourceCount')} value={series.resource_count ?? 0} />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card size="small">
            <Statistic title={t('series.downloadTasks')} value={series.task_count ?? 0} />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card size="small">
            <Statistic title={t('series.linkedAgents')} value={series.agent_work_count ?? 0} />
          </Card>
        </Col>
      </Row>

      {/* Collection siblings (franchise grouping) */}
      {series.collection && (
        <CollectionSiblingsCard
          collection={series.collection}
          siblings={series.collection_siblings ?? []}
        />
      )}

      {/* Episodes */}
      {series.episodes && series.episodes.length > 0 && (
        <Card title={`${t('series.episodeList')} (${series.episodes.length})`} style={{ marginBottom: 16 }} size="small">
          <Table
            className="stack-table"
            columns={withMobileLabels(episodeColumns)}
            dataSource={series.episodes}
            rowKey={(e) => `${e.season}-${e.episode}`}
            pagination={false}
            size="small"
          />
        </Card>
      )}

      {/* Resources */}
      {series.resources && series.resources.length > 0 && (
        <Card title={`${t('series.recentResources')} (${series.resource_count ?? series.resources.length})`} size="small">
          <Table
            className="stack-table"
            columns={withMobileLabels(resourceColumns)}
            dataSource={series.resources}
            rowKey="id"
            pagination={false}
            size="small"
          />
        </Card>
      )}

      <CreateTaskModal
        resourceId={createTaskResourceId}
        open={!!createTaskResourceId}
        onClose={() => setCreateTaskResourceId(null)}
      />

      <WorkEditModal
        open={editOpen}
        work={series}
        contentType="tv"
        onClose={() => setEditOpen(false)}
        onSaved={(updated) => setSeries(updated as TVSeries)}
      />

      <Modal
        open={refreshOpen}
        title={t('works.refreshMetadata')}
        okText={t('common.confirm')}
        cancelText={t('common.cancel')}
        onOk={handleRefreshMetadata}
        onCancel={() => setRefreshOpen(false)}
      >
        <p>{t('works.refreshDesc')}</p>
        <Checkbox
          checked={overrideManualEdits}
          onChange={(e) => setOverrideManualEdits(e.target.checked)}
        >
          {t('works.overrideManualEdits')}
        </Checkbox>
        <p style={{ marginTop: 8, color: 'var(--rr-text-muted)', fontSize: 12 }}>
          {t('works.overrideManualEditsDesc')}
        </p>
      </Modal>
    </div>
  );
}
