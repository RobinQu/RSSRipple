import { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { useParams, Link } from 'react-router-dom';
import { ArrowLeft, Trash2, RefreshCw } from 'lucide-react';
import { Typography, Spin, Card, Button, Space, Tag, Descriptions, Statistic, Table, Row, Col, App } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { moviesApi } from '../api/movies';
import { worksApi } from '../api/works';
import type { Movie, FileResource } from '../types';
import { timeAgo } from '../utils/format';

const { Title, Text } = Typography;

export default function MovieDetail() {
  const { t } = useTranslation();
  const { id } = useParams<{ id: string }>();
  const { modal, message } = App.useApp();
  const [movie, setMovie] = useState<Movie | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    if (!id) return;
    loadMovie();
  }, [id]);

  async function loadMovie() {
    const r = await moviesApi.get(id!);
    if (r.success) setMovie(r.data as Movie);
    setLoading(false);
  }

  async function handleRefreshMetadata() {
    if (!id) return;
    setRefreshing(true);
    try {
      const r = await worksApi.refreshMetadata(id, 'movie');
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
    const blocked = (movie as any)?.agent_work_count > 0;
    modal.confirm({
      title: t('common.delete'),
      content: blocked
        ? t('movies.deleteBlocked', { count: (movie as any)?.agent_work_count ?? 0 })
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
          const code = (r as any).error?.code;
          if (code === 'DELETE_BLOCKED') {
            message.error((r as any).error?.message || t('movies.deleteBlockedGeneric'));
          } else {
            message.error(t('common.error'));
          }
        }
      },
    });
  }

  if (loading) return <Spin style={{ display: 'flex', justifyContent: 'center', padding: 48 }} />;
  if (!movie) return <Text type="danger">{t('movies.notFound')}</Text>;

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
  ];

  return (
    <div>
      <Space style={{ marginBottom: 24 }}>
        <Link to="/works">
          <Button type="text" icon={<ArrowLeft size={18} />} />
        </Link>
        <Title level={3} style={{ margin: 0 }}>
          {movie.title_cn || movie.title_en || movie.original_title}
        </Title>
        <Tag color="green">{t('movies.title')}</Tag>
        <Button
          icon={<RefreshCw size={16} />}
          loading={refreshing}
          onClick={handleRefreshMetadata}
        >
          {t('works.refreshMetadata')}
        </Button>
        <Button danger type="primary" icon={<Trash2 size={16} />} onClick={handleDelete}>
          {t('common.delete')}
        </Button>
      </Space>

      <Card>
        <Space align="start" size={16}>
          {movie.poster_url && (
            <img src={movie.poster_url} alt="" style={{ width: 160, height: 240, objectFit: 'cover', borderRadius: 8 }} />
          )}
          <div style={{ flex: 1 }}>
            <Descriptions column={1} size="small">
              <Descriptions.Item label={t('movies.cnTitle')}>{movie.title_cn || '—'}</Descriptions.Item>
              <Descriptions.Item label={t('movies.enTitle')}>{movie.title_en || '—'}</Descriptions.Item>
              <Descriptions.Item label={t('movies.originalTitle')}>{movie.original_title || '—'}</Descriptions.Item>
              <Descriptions.Item label={t('movies.rating')}>{movie.rating ?? '—'}</Descriptions.Item>
              <Descriptions.Item label={t('common.status')}>{movie.status || '—'}</Descriptions.Item>
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
        </Space>
      </Card>

      {/* Stats */}
      <Card style={{ marginTop: 16 }}>
        <Row gutter={48}>
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

      {/* Recent Resources */}
      {movie.resources && movie.resources.length > 0 && (
        <Card title={t('series.recentResources')} style={{ marginTop: 16 }}>
          <Table<FileResource>
            columns={resourceColumns}
            dataSource={movie.resources}
            rowKey="id"
            size="small"
            pagination={false}
            style={{ marginTop: -8 }}
          />
        </Card>
      )}
    </div>
  );
}
