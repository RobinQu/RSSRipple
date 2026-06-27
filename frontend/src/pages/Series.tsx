import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { Input, Table, Typography, Tag, Empty } from 'antd';
import type { TableColumnsType } from 'antd';
import { SearchOutlined } from '@ant-design/icons';
import { seriesApi } from '../api/series';
import type { TVSeries } from '../types';
import { timeAgo } from '../utils/format';

const { Title } = Typography;

export default function Series() {
  const [list, setList] = useState<TVSeries[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [search, setSearch] = useState('');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const t = setTimeout(() => {
      setLoading(true);
      seriesApi.list(page, 20, search.trim() || undefined).then((r) => {
        if (r.success) {
          setList(r.data);
          if (r.meta) setTotal(r.meta.total);
        }
        setLoading(false);
      });
    }, 250);
    return () => clearTimeout(t);
  }, [page, search]);

  const columns: TableColumnsType<TVSeries> = [
    {
      title: '海报',
      dataIndex: 'poster_url',
      key: 'poster',
      width: 60,
      render: (url: string | null) =>
        url ? (
          <img
            src={url}
            alt=""
            style={{ width: 36, height: 54, objectFit: 'cover', borderRadius: 4 }}
            onError={(e) => ((e.target as HTMLImageElement).style.display = 'none')}
          />
        ) : (
          <div style={{ width: 36, height: 54, background: '#eeece7', borderRadius: 4 }} />
        ),
    },
    {
      title: '标题',
      key: 'title',
      render: (_, r) => (
        <Link to={`/series/${r.id}`}>
          {r.title_cn || r.title_en || r.original_title || '未命名'}
        </Link>
      ),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 120,
      render: (s: string | null) => (s ? <Tag>{s}</Tag> : '—'),
    },
    {
      title: '季/集',
      key: 'se',
      width: 100,
      render: (_, r) =>
        r.number_of_seasons
          ? `${r.number_of_seasons}季 ${r.number_of_episodes || '?'}集`
          : '—',
    },
    {
      title: '评分',
      dataIndex: 'rating',
      key: 'rating',
      width: 80,
      render: (v: number | null) => (v != null ? v.toFixed(1) : '—'),
    },
    {
      title: '更新时间',
      dataIndex: 'updated_at',
      key: 'updated',
      width: 150,
      render: (v: string) => timeAgo(v),
    },
  ];

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 24 }}>
        <Title level={3} style={{ margin: 0 }}>剧集</Title>
        <Input
          prefix={<SearchOutlined />}
          placeholder="搜索..."
          value={search}
          onChange={(e) => {
            setSearch(e.target.value);
            setPage(1);
          }}
          style={{ width: 280 }}
          allowClear
        />
      </div>
      <Table
        columns={columns}
        dataSource={list}
        rowKey="id"
        loading={loading}
        locale={{ emptyText: <Empty description="暂无数据" /> }}
        pagination={{ current: page, pageSize: 20, total, onChange: setPage, showSizeChanger: false }}
      />
    </div>
  );
}
