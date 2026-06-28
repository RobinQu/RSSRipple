import { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import { Input, Table, Typography, Tag, Empty } from 'antd';
import type { TableColumnsType } from 'antd';
import { SearchOutlined } from '@ant-design/icons';
import { seriesApi } from '../api/series';
import type { TVSeries } from '../types';
import { timeAgo } from '../utils/format';

const { Title } = Typography;

export default function Series() {
  const { t } = useTranslation();
  const [list, setList] = useState<TVSeries[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [search, setSearch] = useState('');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const timeout = setTimeout(() => {
      setLoading(true);
      seriesApi.list(page, 20, search.trim() || undefined).then((r) => {
        if (r.success) {
          setList(r.data);
          if (r.meta) setTotal(r.meta.total);
        }
        setLoading(false);
      });
    }, 250);
    return () => clearTimeout(timeout);
  }, [page, search]);

  const columns: TableColumnsType<TVSeries> = [
    {
      title: t('series.poster'),
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
      title: t('series.name'),
      key: 'title',
      render: (_, r) => (
        <Link to={`/series/${r.id}`}>
          {r.title_cn || r.title_en || r.original_title || t('series.unnamed')}
        </Link>
      ),
    },
    {
      title: t('common.status'),
      dataIndex: 'status',
      key: 'status',
      width: 120,
      render: (s: string | null) => (s ? <Tag>{s}</Tag> : '—'),
    },
    {
      title: t('series.seasonsEpisodes'),
      key: 'se',
      width: 100,
      render: (_, r) =>
        r.number_of_seasons
          ? `${r.number_of_seasons}${t('series.season')} ${r.number_of_episodes || '?'}${t('series.episode')}`
          : '—',
    },
    {
      title: t('series.rating'),
      dataIndex: 'rating',
      key: 'rating',
      width: 80,
      render: (v: number | null) => (v != null ? v.toFixed(1) : '—'),
    },
    {
      title: t('series.updatedAt'),
      dataIndex: 'updated_at',
      key: 'updated',
      width: 150,
      render: (v: string) => timeAgo(v),
    },
  ];

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 24 }}>
        <Title level={3} style={{ margin: 0 }}>{t('series.title')}</Title>
        <Input
          aria-label={t('series.search')}
          prefix={<SearchOutlined />}
          placeholder={t('series.search')}
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
        locale={{ emptyText: <Empty description={t('common.noData')} /> }}
        pagination={{ current: page, pageSize: 20, total, onChange: setPage, showSizeChanger: false }}
      />
    </div>
  );
}
