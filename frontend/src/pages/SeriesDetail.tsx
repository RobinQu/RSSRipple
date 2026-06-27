import { useState, useEffect } from 'react';
import { useParams, Link } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import {
  Typography, Spin, Card, Button, Space, Tag, Descriptions,
  Row, Col, Statistic, Table,
} from 'antd';
import type { TableColumnsType } from 'antd';
import { seriesApi } from '../api/series';
import type { TVSeries, Episode, FileResource } from '../types';
import { timeAgo } from '../utils/format';

const { Title, Text } = Typography;

const episodeColumns: TableColumnsType<Episode> = [
  { title: '季', dataIndex: 'season', key: 'season', width: 60 },
  { title: '集', dataIndex: 'episode', key: 'episode', width: 60 },
  { title: '标题', dataIndex: 'title', key: 'title', render: (v: string | null) => v || '—' },
  { title: '播出日期', dataIndex: 'air_date', key: 'air_date', width: 130, render: (v: string | null) => v || '—' },
];

const resourceColumns: TableColumnsType<FileResource> = [
  { title: '标题', dataIndex: 'title_raw', key: 'title', ellipsis: true },
  { title: '分辨率', dataIndex: 'resolution', key: 'resolution', width: 100, render: (v: string | null) => v ? <Tag>{v}</Tag> : '—' },
  { title: '字幕组', dataIndex: 'subtitle_group', key: 'subtitle_group', width: 140, render: (v: string | null) => v || '—' },
  { title: '发布时间', dataIndex: 'published_at', key: 'published_at', width: 160, render: (v: string | null) => (v ? timeAgo(v) : '—') },
];

export default function SeriesDetail() {
  const { id } = useParams<{ id: string }>();
  const [series, setSeries] = useState<TVSeries | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!id) return;
    seriesApi.get(id).then((r) => {
      if (r.success) setSeries(r.data);
      setLoading(false);
    });
  }, [id]);

  if (loading) return <Spin style={{ display: 'flex', justifyContent: 'center', padding: 48 }} />;
  if (!series) return <Text type="danger">未找到</Text>;

  return (
    <div>
      <Space style={{ marginBottom: 24 }}>
        <Link to="/series">
          <Button type="text" icon={<ArrowLeft size={18} />} />
        </Link>
        <Title level={3} style={{ margin: 0 }}>
          {series.title_cn || series.title_en || series.original_title}
        </Title>
        <Tag color="blue">剧集</Tag>
      </Space>

      {/* Metadata card */}
      <Card style={{ marginBottom: 16 }}>
        <Space align="start" size={16}>
          {series.poster_url && (
            <img src={series.poster_url} alt="" style={{ width: 160, height: 240, objectFit: 'cover', borderRadius: 8 }} />
          )}
          <div style={{ flex: 1 }}>
            <Descriptions column={1} size="small">
              <Descriptions.Item label="中文标题">{series.title_cn || '—'}</Descriptions.Item>
              <Descriptions.Item label="英文标题">{series.title_en || '—'}</Descriptions.Item>
              <Descriptions.Item label="原始标题">{series.original_title || '—'}</Descriptions.Item>
              <Descriptions.Item label="评分">{series.rating ?? '—'}</Descriptions.Item>
              <Descriptions.Item label="状态">{series.status || '—'}</Descriptions.Item>
              <Descriptions.Item label="季/集">
                {series.number_of_seasons ? `${series.number_of_seasons}季 ${series.number_of_episodes || '?'}集` : '—'}
              </Descriptions.Item>
              <Descriptions.Item label="首播">{series.start_date || '—'}</Descriptions.Item>
              <Descriptions.Item label="完结">{series.end_date || '—'}</Descriptions.Item>
              <Descriptions.Item label="更新时间">{timeAgo(series.updated_at)}</Descriptions.Item>
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
            <Statistic title="资源数" value={series.resource_count ?? 0} />
          </Card>
        </Col>
        <Col span={8}>
          <Card size="small">
            <Statistic title="下载任务" value={series.task_count ?? 0} />
          </Card>
        </Col>
        <Col span={8}>
          <Card size="small">
            <Statistic title="关联 Agent" value={series.agent_work_count ?? 0} />
          </Card>
        </Col>
      </Row>

      {/* Episodes */}
      {series.episodes && series.episodes.length > 0 && (
        <Card title={`剧集列表 (${series.episodes.length})`} style={{ marginBottom: 16 }} size="small">
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
        <Card title={`最近资源 (${series.resource_count ?? series.resources.length})`} size="small">
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
