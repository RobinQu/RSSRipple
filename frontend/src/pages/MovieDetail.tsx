import { useState, useEffect } from 'react';
import { useParams, Link } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { Typography, Spin, Card, Button, Space, Tag, Descriptions, Statistic, Table, Row, Col } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { moviesApi } from '../api/movies';
import type { Movie, FileResource } from '../types';
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

  const resourceColumns: ColumnsType<FileResource> = [
    {
      title: '原始标题',
      dataIndex: 'title_raw',
      key: 'title_raw',
      ellipsis: true,
      render: (text: string) => (
        <Text style={{ fontSize: 13, color: '#212121' }}>{text}</Text>
      ),
    },
    {
      title: '分辨率',
      dataIndex: 'resolution',
      key: 'resolution',
      width: 100,
      render: (val: string | null) =>
        val ? <Tag color="blue">{val}</Tag> : <Text type="secondary">—</Text>,
    },
    {
      title: '字幕组',
      dataIndex: 'subtitle_group',
      key: 'subtitle_group',
      width: 140,
      ellipsis: true,
      render: (val: string | null) => val || <Text type="secondary">—</Text>,
    },
    {
      title: '发布时间',
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
              title="资源数"
              value={movie.resource_count ?? 0}
              valueStyle={{ fontSize: 28, fontWeight: 600, color: '#212121' }}
            />
          </Col>
          <Col>
            <Statistic
              title="下载任务数"
              value={movie.task_count ?? 0}
              valueStyle={{ fontSize: 28, fontWeight: 600, color: '#212121' }}
            />
          </Col>
        </Row>
      </Card>

      {/* Recent Resources */}
      {movie.resources && movie.resources.length > 0 && (
        <Card title="最近资源" style={{ marginTop: 16 }}>
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
