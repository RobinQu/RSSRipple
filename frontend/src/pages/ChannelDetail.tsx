import { useState, useEffect } from 'react';
import { useParams, Link } from 'react-router-dom';
import { ArrowLeft, RefreshCw, Wand2 } from 'lucide-react';
import { Typography, Space, Button, Spin, Card, Row, Col, Alert, Table, App } from 'antd';
import type { TableColumnsType } from 'antd';
import { channelsApi } from '../api/channels';
import StatusBadge from '../components/StatusBadge';
import { timeAgo } from '../utils/format';
import type { Channel, FileResource } from '../types';

export default function ChannelDetail() {
  const { id } = useParams<{ id: string }>();
  const { message } = App.useApp();
  const [channel, setChannel] = useState<Channel | null>(null);
  const [resources, setResources] = useState<FileResource[]>([]);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [analyzing, setAnalyzing] = useState(false);
  const [analyzeResult, setAnalyzeResult] = useState<{ confidence: string; mapping: Record<string, unknown> } | null>(null);

  useEffect(() => {
    if (id) {
      channelsApi.get(id).then(r => {
        if (r.success) setChannel(r.data);
        setLoading(false);
      });
    }
  }, [id]);

  useEffect(() => {
    if (!id) return;
    channelsApi.resources(id, page).then(r => {
      if (r.success) { setResources(r.data); if (r.meta) setTotal(r.meta.total); }
    });
  }, [id, page]);

  const handleFetch = async () => {
    if (!id) return;
    await channelsApi.fetch(id);
    message.info('Fetch triggered, refreshing...');
    setTimeout(() => {
      channelsApi.resources(id, page).then(r => {
        if (r.success) { setResources(r.data); if (r.meta) setTotal(r.meta.total); }
      });
    }, 2000);
  };

  const handleAnalyze = async () => {
    if (!id) return;
    setAnalyzing(true);
    setAnalyzeResult(null);
    const res = await channelsApi.analyze(id);
    setAnalyzing(false);
    if (res.success) {
      setAnalyzeResult({ confidence: res.data.confidence, mapping: res.data.field_mapping });
    }
  };

  const handleApplyMapping = async () => {
    if (!id || !analyzeResult) return;
    const res = await channelsApi.applyMapping(id, { field_mapping: analyzeResult.mapping, parser_type: 'custom' });
    if (res.success) {
      message.success('Mapping applied');
      channelsApi.get(id).then(r => { if (r.success) setChannel(r.data); });
      setAnalyzeResult(null);
    }
  };

  const columns: TableColumnsType<FileResource> = [
    {
      title: 'Title',
      dataIndex: 'title_raw',
      key: 'title_raw',
      ellipsis: true,
    },
    {
      title: 'Group',
      dataIndex: 'subtitle_group',
      key: 'subtitle_group',
      render: (v: string | null) => v || '—',
    },
    {
      title: 'EP',
      dataIndex: 'episode',
      key: 'episode',
      render: (v: number | null) => v ?? '—',
    },
    {
      title: 'Resolution',
      dataIndex: 'resolution',
      key: 'resolution',
      render: (v: string | null) => v || '—',
    },
    {
      title: 'Codec',
      dataIndex: 'video_codec',
      key: 'video_codec',
      render: (v: string | null) => v || '—',
    },
    {
      title: 'Container',
      dataIndex: 'container',
      key: 'container',
      render: (v: string | null) => v || '—',
    },
    {
      title: 'Published',
      dataIndex: 'published_at',
      key: 'published_at',
      render: (v: string | null) => v ? timeAgo(v) : '—',
    },
  ];

  if (loading) return <Spin style={{ display: 'flex', justifyContent: 'center', padding: 48 }} />;
  if (!channel) return <Typography.Text type="secondary">Channel not found</Typography.Text>;

  return (
    <div>
      {/* Header */}
      <Space align="center" style={{ marginBottom: 24, width: '100%', justifyContent: 'space-between', flexWrap: 'wrap' }}>
        <Space align="center">
          <Link to="/channels"><Button type="text" icon={<ArrowLeft size={18} />} /></Link>
          <div>
            <Space align="center">
              <Typography.Title level={3} style={{ margin: 0 }}>{channel.name}</Typography.Title>
              <StatusBadge status={channel.status} />
            </Space>
            <div>
              <Typography.Text type="secondary">{channel.url}</Typography.Text>
            </div>
          </div>
        </Space>
        <Space>
          <Button icon={<RefreshCw size={14} />} onClick={handleFetch}>Fetch</Button>
          <Button type="primary" icon={<Wand2 size={14} />} loading={analyzing} onClick={handleAnalyze}>
            Analyze Feed
          </Button>
        </Space>
      </Space>

      {/* Channel info cards */}
      <Row gutter={[12, 12]} style={{ marginBottom: 24 }}>
        <Col xs={12} sm={6}>
          <Card size="small">
            <div style={{ fontSize: 12, color: '#9c9c9d' }}>Parser</div>
            <div style={{ fontWeight: 500 }}>{channel.parser_type}</div>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card size="small">
            <div style={{ fontSize: 12, color: '#9c9c9d' }}>Fetch Interval</div>
            <div style={{ fontWeight: 500 }}>{channel.fetch_interval}s</div>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card size="small">
            <div style={{ fontSize: 12, color: '#9c9c9d' }}>Resources</div>
            <div style={{ fontWeight: 500 }}>{total}</div>
          </Card>
        </Col>
        <Col xs={12} sm={6}>
          <Card size="small">
            <div style={{ fontSize: 12, color: '#9c9c9d' }}>Last Fetched</div>
            <div style={{ fontWeight: 500 }}>{channel.last_fetched_at ? timeAgo(channel.last_fetched_at) : 'Never'}</div>
          </Card>
        </Col>
      </Row>

      {/* Analyze result */}
      {analyzeResult && (
        <Alert
          type="success"
          message={`Feed Analysis Complete — Confidence: ${analyzeResult.confidence}`}
          description={
            <pre style={{ fontSize: 12, maxHeight: 192, overflow: 'auto', margin: '8px 0 0' }}>
              {JSON.stringify(analyzeResult.mapping, null, 2)}
            </pre>
          }
          action={
            <Button size="small" type="primary" onClick={handleApplyMapping}>
              Apply Mapping
            </Button>
          }
          style={{ marginBottom: 24 }}
          showIcon
        />
      )}

      {/* Resources table */}
      <Card
        title="Synced Resources"
        size="small"
        styles={{ body: { padding: 0 } }}
      >
        <Table<FileResource>
          columns={columns}
          dataSource={resources}
          rowKey="id"
          size="small"
          locale={{ emptyText: 'No resources yet. Click "Fetch" to pull from the RSS feed.' }}
          pagination={{
            current: page,
            pageSize: 20,
            total,
            onChange: (p) => setPage(p),
            showSizeChanger: false,
          }}
        />
      </Card>
    </div>
  );
}
