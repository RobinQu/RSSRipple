import { useState, useEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import useDocumentTitle from '../hooks/useDocumentTitle';
import { useParams, Link } from 'react-router-dom';
import { ArrowLeft, Trash2, RefreshCw, Download, Pencil } from 'lucide-react';
import { Typography, Spin, Card, Button, Tag, Descriptions, Statistic, Table, Row, Col, App, Modal, Checkbox } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { moviesApi } from '../api/movies';
import { worksApi } from '../api/works';
import type { Movie, FileResource } from '../types';
import { timeAgo } from '../utils/format';
import { withMobileLabels } from '../utils/table';
import { posterUrl, useDefaultPoster } from '../utils/poster';
import CreateTaskModal from '../components/CreateTaskModal';
import CollectionSiblingsCard from '../components/CollectionSiblingsCard';
import WorkEditModal from '../components/WorkEditModal';

const { Title, Text, Link: AntdLink } = Typography;

export default function MovieDetail() {
  const { t } = useTranslation();
  const { id } = useParams<{ id: string }>();
  const { modal, message } = App.useApp();
  const [movie, setMovie] = useState<Movie | null>(null);
  useDocumentTitle(movie ? movie.title_cn || movie.title_en || movie.original_title : t('movies.title'));
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [createTaskResourceId, setCreateTaskResourceId] = useState<string | null>(null);
  const [editOpen, setEditOpen] = useState(false);
  const [refreshOpen, setRefreshOpen] = useState(false);
  const [overrideManualEdits, setOverrideManualEdits] = useState(false);

  const loadMovie = useCallback(async () => {
    if (!id) return;
    const r = await moviesApi.get(id);
    if (r.success) setMovie(r.data as Movie);
    setLoading(false);
  }, [id]);

  useEffect(() => {
    loadMovie();
  }, [loadMovie]);

  const openRefreshDialog = () => {
    setOverrideManualEdits(false);
    setRefreshOpen(true);
  };

  async function handleRefreshMetadata() {
    if (!id) return;
    setRefreshOpen(false);
    setRefreshing(true);
    try {
      const r = await worksApi.refreshMetadata(id, 'movie', null, overrideManualEdits);
      if (r.success) {
        const filled = r.data.filled?.length ?? 0;
        message.success(
          filled > 0 ? t('works.refreshFilled', { n: filled }) : t('works.refreshNoChange'),
        );
        await loadMovie();
      } else {
        message.error(r.error?.message || t('works.refreshFailed'));
      }
    } finally {
      setRefreshing(false);
    }
  }

  async function handleDelete() {
    if (!id) return;
    const blocked = (movie?.agent_work_count ?? 0) > 0;
    modal.confirm({
      title: t('common.delete'),
      content: blocked
        ? t('movies.deleteBlocked', { count: movie?.agent_work_count ?? 0 })
        : t('movies.deleteConfirm'),
      okText: t('common.confirm'),
      cancelText: t('common.cancel'),
      okButtonProps: { danger: true, disabled: blocked },
      onOk: async () => {
        const r = await moviesApi.delete(id);
        if (r.success) {
          message.success(t('movies.deleted'));
          window.location.href = '/works';
        } else {
          const code = r.error?.code;
          if (code === 'DELETE_BLOCKED') {
            message.error(r.error?.message || t('movies.deleteBlockedGeneric'));
          } else {
            message.error(t('common.error'));
          }
        }
      },
    });
  }

  if (loading) return <Spin style={{ display: 'flex', justifyContent: 'center', padding: 48 }} />;
  if (!movie) return <Text type="danger">{t('movies.notFound')}</Text>;

  const sourceLinks = movie.source_links ?? [];

  const resourceColumns: ColumnsType<FileResource> = [
    {
      title: t('series.name'),
      dataIndex: 'title_raw',
      key: 'title_raw',
      ellipsis: true,
      render: (text: string) => (
        <Text style={{ fontSize: 13, color: '#212121' }}>{text}</Text>
      ),
    },
    {
      title: t('series.resolution'),
      dataIndex: 'resolution',
      key: 'resolution',
      width: 100,
      render: (val: string | null) =>
        val ? <Tag color="blue">{val}</Tag> : <Text type="secondary">—</Text>,
    },
    {
      title: t('series.subtitleGroup'),
      dataIndex: 'subtitle_group',
      key: 'subtitle_group',
      width: 140,
      ellipsis: true,
      render: (val: string | null) => val || <Text type="secondary">—</Text>,
    },
    {
      title: t('series.publishedAt'),
      dataIndex: 'published_at',
      key: 'published_at',
      width: 140,
      render: (val: string | null) => (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {timeAgo(val)}
        </Text>
      ),
    },
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

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 8, marginBottom: 24 }}>
        <Link to="/works">
          <Button type="text" icon={<ArrowLeft size={18} />} />
        </Link>
        <Title level={3} style={{ margin: 0, flex: '1 1 200px', minWidth: 0, wordBreak: 'break-word' }}>
          {movie.title_cn || movie.title_en || movie.original_title}
        </Title>
        <Tag color="green">{t('movies.title')}</Tag>
        <Button
          icon={<Pencil size={14} />}
          onClick={() => setEditOpen(true)}
        >
          {t('common.edit')}
        </Button>
        <Button
          icon={<RefreshCw size={16} />}
          loading={refreshing}
          onClick={openRefreshDialog}
        >
          {t('works.refreshMetadata')}
        </Button>
        <Button danger type="primary" icon={<Trash2 size={16} />} onClick={handleDelete}>
          {t('common.delete')}
        </Button>
      </div>

      <Card>
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 16, flexWrap: 'wrap' }}>
          <img
            src={posterUrl(movie.poster_url)}
            alt=""
            style={{ width: 160, height: 240, objectFit: 'cover', borderRadius: 8, flexShrink: 0 }}
            onError={useDefaultPoster}
          />
          <div style={{ flex: '1 1 260px', minWidth: 0 }}>
            <Descriptions column={1} size="small">
              <Descriptions.Item label={t('movies.cnTitle')}>{movie.title_cn || '—'}</Descriptions.Item>
              <Descriptions.Item label={t('movies.enTitle')}>{movie.title_en || '—'}</Descriptions.Item>
              <Descriptions.Item label={t('movies.originalTitle')}>{movie.original_title || '—'}</Descriptions.Item>
              <Descriptions.Item label={t('movies.rating')}>{movie.rating ?? '—'}</Descriptions.Item>
              <Descriptions.Item label={t('common.status')}>{movie.status || '—'}</Descriptions.Item>
              <Descriptions.Item label={t('works.animeStatus')}>
                {movie.is_anime === true ? (
                  <Tag color="magenta">{t('works.anime')}</Tag>
                ) : movie.is_anime === false ? (
                  <Tag>{t('works.liveAction')}</Tag>
                ) : (
                  t('common.unknown')
                )}
              </Descriptions.Item>
              {movie.collection && (
                <Descriptions.Item label={t('works.colCollection')}>
                  <Link to={`/collections/${movie.collection.id}`}>{movie.collection.name}</Link>
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
              <Descriptions.Item label={t('movies.runtime')}>{movie.runtime ? `${movie.runtime}${t('movies.runtimeUnit')}` : '—'}</Descriptions.Item>
              <Descriptions.Item label={t('movies.releaseDate')}>{movie.release_date || '—'}</Descriptions.Item>
              <Descriptions.Item label={t('movies.updatedAt')}>{timeAgo(movie.updated_at)}</Descriptions.Item>
            </Descriptions>
            {movie.description && (
              <Text style={{ display: 'block', marginTop: 12, color: '#93939f' }}>
                {movie.description}
              </Text>
            )}
          </div>
        </div>
      </Card>

      {/* Stats */}
      <Card style={{ marginTop: 16 }}>
        <Row gutter={[24, 16]}>
          <Col>
            <Statistic
              title={t('movies.resourceCount')}
              value={movie.resource_count ?? 0}
              valueStyle={{ fontSize: 28, fontWeight: 600, color: '#212121' }}
            />
          </Col>
          <Col>
            <Statistic
              title={t('movies.downloadTasks')}
              value={movie.task_count ?? 0}
              valueStyle={{ fontSize: 28, fontWeight: 600, color: '#212121' }}
            />
          </Col>
        </Row>
      </Card>

      {/* Collection siblings (franchise grouping) */}
      {movie.collection && (
        <CollectionSiblingsCard
          collection={movie.collection}
          siblings={movie.collection_siblings ?? []}
        />
      )}

      {/* Recent Resources */}
      {movie.resources && movie.resources.length > 0 && (
        <Card title={t('series.recentResources')} style={{ marginTop: 16 }}>
          <Table<FileResource>
            className="stack-table"
            columns={withMobileLabels(resourceColumns)}
            dataSource={movie.resources}
            rowKey="id"
            size="small"
            pagination={false}
            style={{ marginTop: -8 }}
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
        work={movie}
        contentType="movie"
        onClose={() => setEditOpen(false)}
        onSaved={(updated) => setMovie(updated as Movie)}
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
        <p style={{ marginTop: 8, color: '#93939f', fontSize: 12 }}>
          {t('works.overrideManualEditsDesc')}
        </p>
      </Modal>
    </div>
  );
}
