import { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { useParams, Link, useNavigate } from 'react-router-dom';
import { ArrowLeft, Trash2 } from 'lucide-react';
import {
  Typography, Spin, Card, Button, Space, Tag, Descriptions,
  Row, Col, Statistic, Table, Modal, App,
} from 'antd';
import type { TableColumnsType } from 'antd';
import { seriesApi } from '../api/series';
import type { TVSeries, Episode, FileResource } from '../types';
import { timeAgo } from '../utils/format';

const { Title, Text } = Typography;

export default function SeriesDetail() {
  const { t } = useTranslation();
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { message } = App.useApp();
  const [series, setSeries] = useState<TVSeries | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!id) return;
    seriesApi.get(id).then((r) => {
      if (r.success) setSeries(r.data);
      setLoading(false);
    });
  }, [id]);

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
  ];

  if (loading) return <Spin style={{ display: 'flex', justifyContent: 'center', padding: 48 }} />;
  if (!series) return <Text type="danger">{t('series.notFound')}</Text>;

  return (
    <div>
      <Space style={{ marginBottom: 24 }}>
        <Link to="/series">
          <Button type="text" icon={<ArrowLeft size={18} />} />
        </Link>
        <Title level={3} style={{ margin: 0 }}>
          {series.title_cn || series.title_en || series.original_title}
        </Title>
        <Tag color="blue">{t('series.title')}</Tag>
        <Button
          type="default"
          danger
          icon={<Trash2 size={14} />}
          onClick={handleDelete}
          style={{ marginLeft: 8 }}
        >
          {t('common.delete')}
        </Button>
      </Space>

      {/* Metadata card */}
      <Card style={{ marginBottom: 16 }}>
        <Space align="start" size={16}>
          {series.poster_url && (
            <img src={series.poster_url} alt="" style={{ width: 160, height: 240, objectFit: 'cover', borderRadius: 8 }} />
          )}
          <div style={{ flex: 1 }}>
            <Descriptions column={1} size="small">
              <Descriptions.Item label={t('series.cnTitle')}>{series.title_cn || '—'}</Descriptions.Item>
              <Descriptions.Item label={t('series.enTitle')}>{series.title_en || '—'}</Descriptions.Item>
              <Descriptions.Item label={t('series.originalTitle')}>{series.original_title || '—'}</Descriptions.Item>
              <Descriptions.Item label={t('series.rating')}>{series.rating ?? '—'}</Descriptions.Item>
              <Descriptions.Item label={t('common.status')}>{series.status || '—'}</Descriptions.Item>
              <Descriptions.Item label={t('series.seasonsEpisodes')}>
                {series.number_of_seasons ? `${series.number_of_seasons}${t('series.season')} ${series.number_of_episodes || '?'}${t('series.episode')}` : '—'}
              </Descriptions.Item>
              <Descriptions.Item label={t('series.startDate')}>{series.start_date || '—'}</Descriptions.Item>
              <Descriptions.Item label={t('series.endDate')}>{series.end_date || '—'}</Descriptions.Item>
              <Descriptions.Item label={t('series.updatedAt')}>{timeAgo(series.updated_at)}</Descriptions.Item>
            </Descriptions>
            {series.description && (
              <Text style={{ display: 'block', marginTop: 12, color: '#93939f' }}>
                {series.description}
              </Text>
            )}
          </div>
        </Space>
      </Card>

      {/* Stats */}
      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col span={8}>
          <Card size="small">
            <Statistic title={t('series.resourceCount')} value={series.resource_count ?? 0} />
          </Card>
        </Col>
        <Col span={8}>
          <Card size="small">
            <Statistic title={t('series.downloadTasks')} value={series.task_count ?? 0} />
          </Card>
        </Col>
        <Col span={8}>
          <Card size="small">
            <Statistic title={t('series.linkedAgents')} value={series.agent_work_count ?? 0} />
          </Card>
        </Col>
      </Row>

      {/* Episodes */}
      {series.episodes && series.episodes.length > 0 && (
        <Card title={`${t('series.episodeList')} (${series.episodes.length})`} style={{ marginBottom: 16 }} size="small">
          <Table
            columns={episodeColumns}
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
            columns={resourceColumns}
            dataSource={series.resources}
            rowKey="id"
            pagination={false}
            size="small"
          />
        </Card>
      )}
    </div>
  );
}
