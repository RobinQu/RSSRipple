import { useEffect, useState } from 'react';
import { Modal, Input, Select, Button, Space, Spin, Empty, Typography, App, Tag } from 'antd';
import { SearchOutlined } from '@ant-design/icons';
import { resourcesApi } from '../api/channels';
import type { FileResource, MetadataSearchResult } from '../types';

const { Text, Paragraph } = Typography;

interface Props {
  resourceId: string | null;
  open: boolean;
  onClose: () => void;
  onCorrected?: () => void;
}

export default function MetadataCorrectionModal({
  resourceId,
  open,
  onClose,
  onCorrected,
}: Props) {
  const { message } = App.useApp();
  const [step, setStep] = useState<'search' | 'results'>('search');
  const [searchTitle, setSearchTitle] = useState('');
  const [contentType, setContentType] = useState<'tv' | 'movie'>('tv');
  const [loading, setLoading] = useState(false);
  const [linking, setLinking] = useState(false);
  const [results, setResults] = useState<MetadataSearchResult[]>([]);
  const [error, setError] = useState<string | null>(null);

  // On open, prefill from resource if possible
  useEffect(() => {
    if (!open || !resourceId) return;
    setStep('search');
    setResults([]);
    setError(null);
    setSearchTitle('');
    // Fetch resource to prefill search_title
    resourcesApi.get(resourceId).then((res) => {
      if (res.success) {
        const r: FileResource = res.data;
        setSearchTitle(r.search_title || r.title_cn || r.title_en || r.title_raw);
      }
    });
  }, [open, resourceId]);

  const handleSearch = async () => {
    if (!resourceId || !searchTitle.trim()) {
      message.warning('请输入搜索词');
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const res = await resourcesApi.searchMetadata(resourceId, {
        search_title: searchTitle.trim(),
        content_type: contentType,
      });
      if (res.success) {
        setResults(res.data.results || []);
        setStep('results');
      } else {
        setError(res.error?.message || '搜索失败');
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : '搜索失败');
    } finally {
      setLoading(false);
    }
  };

  const handleSelect = async (result: MetadataSearchResult) => {
    if (!resourceId) return;
    setLinking(true);
    try {
      const res = await resourcesApi.linkMetadata(resourceId, {
        selected_result: { ...result, content_type: contentType },
      });
      if (res.success) {
        message.success('元数据已关联');
        onCorrected?.();
        onClose();
      } else {
        message.error(res.error?.message || '关联失败');
      }
    } finally {
      setLinking(false);
    }
  };

  return (
    <Modal
      open={open}
      onCancel={onClose}
      title="修正元数据"
      footer={null}
      destroyOnClose
      width={640}
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
        {/* Step 1: search */}
        <div>
          <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
            输入标题关键词并选择内容类型，搜索元数据候选
          </Text>
          <Space.Compact style={{ width: '100%' }}>
            <Input
              value={searchTitle}
              onChange={(e) => setSearchTitle(e.target.value)}
              placeholder="搜索标题..."
              onPressEnter={handleSearch}
              prefix={<SearchOutlined />}
            />
            <Select
              value={contentType}
              onChange={(v) => setContentType(v)}
              style={{ width: 120 }}
              options={[
                { value: 'tv', label: '剧集' },
                { value: 'movie', label: '电影' },
              ]}
            />
            <Button type="primary" onClick={handleSearch} loading={loading}>
              搜索
            </Button>
          </Space.Compact>
        </div>

        {error && (
          <div style={{ color: '#b30000', fontSize: 13 }}>{error}</div>
        )}

        {/* Step 2: results */}
        {loading && (
          <div style={{ textAlign: 'center', padding: 48 }}>
            <Spin />
            <div style={{ marginTop: 12, color: '#93939f', fontSize: 13 }}>
              正在搜索...
            </div>
          </div>
        )}

        {!loading && step === 'results' && (
          <div>
            {results.length === 0 ? (
              <Empty description="未找到匹配结果，请尝试调整搜索词" />
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 10, maxHeight: 480, overflow: 'auto' }}>
                {results.map((r, idx) => (
                  <div
                    key={idx}
                    style={{
                      display: 'flex',
                      gap: 12,
                      padding: 12,
                      border: '1px solid #e5e7eb',
                      borderRadius: 8,
                      background: '#f7f7f5',
                    }}
                  >
                    {r.poster_url ? (
                      <img
                        src={r.poster_url}
                        alt="poster"
                        style={{
                          width: 60,
                          height: 90,
                          objectFit: 'cover',
                          borderRadius: 4,
                          flexShrink: 0,
                          background: '#eeece7',
                        }}
                        onError={(e) => {
                          (e.target as HTMLImageElement).style.display = 'none';
                        }}
                      />
                    ) : (
                      <div
                        style={{
                          width: 60,
                          height: 90,
                          borderRadius: 4,
                          background: '#eeece7',
                          flexShrink: 0,
                        }}
                      />
                    )}
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                        <Text strong style={{ color: '#212121' }}>
                          {r.title_cn || r.title_en || r.original_title}
                        </Text>
                        <Tag color={r.content_type === 'tv' ? 'blue' : 'green'}>
                          {r.content_type === 'tv' ? '剧集' : '电影'}
                        </Tag>
                        {r.year && (
                          <Text type="secondary" style={{ fontSize: 12 }}>
                            {r.year}
                          </Text>
                        )}
                      </div>
                      {(r.title_en || r.original_title) &&
                        (r.title_en || r.original_title) !== r.title_cn && (
                          <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 6 }}>
                            {r.title_en || r.original_title}
                          </Text>
                        )}
                      {r.description && (
                        <Paragraph
                          ellipsis={{ rows: 2 }}
                          style={{ fontSize: 12, color: '#93939f', marginBottom: 8 }}
                        >
                          {r.description}
                        </Paragraph>
                      )}
                      <Button
                        type="primary"
                        size="small"
                        loading={linking}
                        onClick={() => handleSelect(r)}
                      >
                        确认选择
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
            )}
            <div style={{ marginTop: 12, textAlign: 'center' }}>
              <Button
                type="text"
                size="small"
                onClick={() => setStep('search')}
              >
                ← 重新搜索
              </Button>
            </div>
          </div>
        )}
      </div>
    </Modal>
  );
}
