import { useState, useEffect } from 'react';
import { useParams, Link } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { Typography, Spin, Card, Button, Space, Tag, Descriptions } from 'antd';
import { moviesApi } from '../api/movies';
import type { Movie } from '../types';
import { timeAgo } from '../utils/format';

const { Title, Text } = Typography;

export default function MovieDetail() {
  const { id } = useParams<{ id: string }>();
  const [movie, setMovie] = useState<Movie | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!id) return;
    moviesApi.get(id).then((r) => {
      if (r.success) setMovie(r.data);
      setLoading(false);
    });
  }, [id]);

  if (loading) return <Spin style={{ display: 'flex', justifyContent: 'center', padding: 48 }} />;
  if (!movie) return <Text type="danger">未找到</Text>;

  return (
    <div>
      <Space style={{ marginBottom: 24 }}>
        <Link to="/movies">
          <Button type="text" icon={<ArrowLeft size={18} />} />
        </Link>
        <Title level={3} style={{ margin: 0 }}>
          {movie.title_cn || movie.title_en || movie.original_title}
        </Title>
        <Tag color="green">电影</Tag>
      </Space>
      <Card>
        <Space align="start" size={16}>
          {movie.poster_url && (
            <img src={movie.poster_url} alt="" style={{ width: 160, height: 240, objectFit: 'cover', borderRadius: 8 }} />
          )}
          <div style={{ flex: 1 }}>
            <Descriptions column={1} size="small">
              <Descriptions.Item label="中文标题">{movie.title_cn || '—'}</Descriptions.Item>
              <Descriptions.Item label="英文标题">{movie.title_en || '—'}</Descriptions.Item>
              <Descriptions.Item label="原始标题">{movie.original_title || '—'}</Descriptions.Item>
              <Descriptions.Item label="评分">{movie.rating ?? '—'}</Descriptions.Item>
              <Descriptions.Item label="状态">{movie.status || '—'}</Descriptions.Item>
              <Descriptions.Item label="片长">{movie.runtime ? `${movie.runtime}分钟` : '—'}</Descriptions.Item>
              <Descriptions.Item label="上映日期">{movie.release_date || '—'}</Descriptions.Item>
              <Descriptions.Item label="更新时间">{timeAgo(movie.updated_at)}</Descriptions.Item>
            </Descriptions>
            {movie.description && (
              <Text style={{ display: 'block', marginTop: 12, color: '#9c9c9d' }}>
                {movie.description}
              </Text>
            )}
          </div>
        </Space>
      </Card>
    </div>
  );
}
