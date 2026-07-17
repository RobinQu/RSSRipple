import { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { useParams, Link } from 'react-router-dom';
import { ArrowLeft, Trash2 } from 'lucide-react';
import { Typography, Spin, Card, Button, Space, Tag, Descriptions, Statistic, Table, Row, Col, App } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { audioWorksApi } from '../api/audioWorks';
import type { AudioWork, FileResource } from '../types';
import { timeAgo } from '../utils/format';
import { posterUrl, useDefaultPoster } from '../utils/poster';

const { Title, Text } = Typography;

export default function AudioWorkDetail() {
  const { t } = useTranslation();
  const { id } = useParams<{ id: string }>();
  const { modal, message } = App.useApp();
  const [work, setWork] = useState<AudioWork | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!id) return;
    loadWork();
  }, [id]);

  async function loadWork() {
    const r = await audioWorksApi.get(id!);
    if (r.success) setWork(r.data as AudioWork);
    setLoading(false);
  }

  async function handleDelete() {
    if (!id) return;
    modal.confirm({
      title: t('common.delete'),
      content: t('audioWorks.deleteConfirm'),
      okText: t('common.confirm'),
      cancelText: t('common.cancel'),
      okButtonProps: { danger: true },
      onOk: async () => {
        const r = await audioWorksApi.delete(id);
        if (r.success) {
          message.success(t('audioWorks.deleted'));
          window.location.href = '/works';
        } else {
          message.error(t('common.error'));
        }
      },
    });
  }

  if (loading) return <Spin style={{ display: 'flex', justifyContent: 'center', padding: 48 }} />;
  if (!work) return <Text type="danger">{t('audioWorks.notFound')}</Text>;

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
      title: t('series.subtitleGroup'),
      dataIndex: 'subtitle_group',
      key: 'subtitle_group',
      width: 160,
      ellipsis: true,
      render: (val: string | null) => val || <Text type="secondary">-</Text>,
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

  const typeLabel = work.content_type
    ? t(`works.audioType.${work.content_type}`, work.content_type)
    : t('works.audio');

  return (
    <div>
      <Space style={{ marginBottom: 24 }}>
        <Link to="/works">
          <Button type="text" icon={<ArrowLeft size={18} />} />
        </Link>
        <Title level={3} style={{ margin: 0 }}>
          {work.title_cn || work.title_en || work.original_title}
        </Title>
        <Tag color="purple">{typeLabel}</Tag>
        <Button danger type="primary" icon={<Trash2 size={16} />} onClick={handleDelete}>
          {t('common.delete')}
        </Button>
      </Space>

      <Card>
        <Space align="start" size={16}>
          <img
            src={posterUrl(work.poster_url)}
            alt=""
            style={{ width: 160, height: 240, objectFit: 'cover', borderRadius: 8, flexShrink: 0 }}
            onError={useDefaultPoster}
          />
          <div style={{ flex: 1 }}>
            <Descriptions column={1} size="small">
              <Descriptions.Item label={t('movies.cnTitle')}>{work.title_cn || '-'}</Descriptions.Item>
              <Descriptions.Item label={t('movies.enTitle')}>{work.title_en || '-'}</Descriptions.Item>
              <Descriptions.Item label={t('movies.originalTitle')}>{work.original_title || '-'}</Descriptions.Item>
              <Descriptions.Item label={t('movies.rating')}>{work.rating ?? '-'}</Descriptions.Item>
              <Descriptions.Item label={t('common.status')}>{work.status || '-'}</Descriptions.Item>
              <Descriptions.Item label={t('movies.releaseDate')}>{work.release_date || '-'}</Descriptions.Item>
              <Descriptions.Item label={t('audioWorks.source')}>{work.external_source || '-'}</Descriptions.Item>
              <Descriptions.Item label={t('movies.updatedAt')}>{timeAgo(work.updated_at)}</Descriptions.Item>
            </Descriptions>
            {work.wikipedia_url && (
              <a href={work.wikipedia_url} target="_blank" rel="noreferrer" style={{ display: 'block', marginTop: 8 }}>
                Wikipedia
              </a>
            )}
            {work.description && (
              <Text style={{ display: 'block', marginTop: 12, color: '#93939f' }}>
                {work.description}
              </Text>
            )}
          </div>
        </Space>
      </Card>

      <Card style={{ marginTop: 16 }}>
        <Row gutter={48}>
          <Col>
            <Statistic
              title={t('movies.resourceCount')}
              value={work.resource_count ?? 0}
              valueStyle={{ fontSize: 28, fontWeight: 600, color: '#212121' }}
            />
          </Col>
        </Row>
      </Card>

      {work.resources && work.resources.length > 0 && (
        <Card title={t('series.recentResources')} style={{ marginTop: 16 }}>
          <Table<FileResource>
            columns={resourceColumns}
            dataSource={work.resources}
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
