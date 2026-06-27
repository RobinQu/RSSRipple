import { useState, useEffect } from 'react';
import { useParams, Link } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { Typography, Spin, Card, Button, Space, Tag, Descriptions } from 'antd';
import { seriesApi } from '../api/series';
import type { TVSeries } from '../types';
import { timeAgo } from '../utils/format';

const { Title, Text } = Typography;

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
      <Card>
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
              <Text style={{ display: 'block', marginTop: 12, color: '#9c9c9d' }}>
                {series.description}
              </Text>
            )}
          </div>
        </Space>
      </Card>
    </div>
  );
}
