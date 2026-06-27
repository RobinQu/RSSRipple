import { useEffect, useState } from 'react';
import {
  Drawer,
  Spin,
  Typography,
  Tag,
  Divider,
  Empty,
  Button,
  Space,
  App,
  Tooltip,
  Descriptions,
} from 'antd';
import { Copy, Film, Pencil } from 'lucide-react';
import { resourcesApi } from '../api/channels';
import { formatBytes, formatDate } from '../utils/format';
import MetadataCorrectionModal from './MetadataCorrectionModal';
import type { FileResource } from '../types';

const { Text, Paragraph } = Typography;

interface LinkedMeta {
  type: 'series' | 'movie';
  title: string;
  poster_url?: string | null;
  description?: string | null;
}interface ResourceDetailDrawerProps {
  resource: FileResource | null;
  onClose: () => void;
  onCorrected?: () => void;
}

function PosterBlock({ url }: { url: string | null | undefined }) {
  if (!url) {
    return (
      <div
        style={{
          width: 80,
          height: 120,
          borderRadius: 6,
          background: '#1a1a1a',
          border: '1px solid #242728',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: '#434345',
          flexShrink: 0,
        }}
      >
        <Film size={24} />
      </div>
    );
  }
  return (
    <img
      src={url}
      alt="poster"
      style={{
        width: 80,
        height: 120,
        objectFit: 'cover',
        borderRadius: 6,
        border: '1px solid #242728',
        background: '#1a1a1a',
        flexShrink: 0,
      }}
      onError={(e) => {
        (e.target as HTMLImageElement).style.display = 'none';
      }}
    />
  );
}

export default function ResourceDetailDrawer({
  resource,
  onClose,
  onCorrected,
}: ResourceDetailDrawerProps) {
  const { message } = App.useApp();
  const [meta, setMeta] = useState<LinkedMeta | null>(null);
  const [metaLoading, setMetaLoading] = useState(false);
  const [correctionOpen, setCorrectionOpen] = useState(false);
  const [resourceData, setResourceData] = useState<FileResource | null>(null);

  const loadMeta = async (rid: string) => {
    setMetaLoading(true);
    try {
      const [metaRes, resRes] = await Promise.all([
        resourcesApi.getMetadata(rid),
        resourcesApi.get(rid),
      ]);
      if (resRes.success) setResourceData(resRes.data);
      if (metaRes.success && metaRes.data) {
        const d = metaRes.data;
        if (d.series_id && d.series) {
          setMeta({
            type: 'series',
            title:
              d.series.title_cn || d.series.title_en || '未知剧集',
            poster_url: d.series.poster_url,
          });
        } else if (d.movie_id && d.movie) {
          setMeta({
            type: 'movie',
            title:
              d.movie.title_cn || d.movie.title_en || '未知电影',
            poster_url: d.movie.poster_url,
          });
        } else {
          setMeta(null);
        }
      }
    } finally {
      setMetaLoading(false);
    }
  };

  useEffect(() => {
    if (!resource) {
      setMeta(null);
      setResourceData(null);
      return;
    }
    setResourceData(resource);
    loadMeta(resource.id);
  }, [resource]);

  const copyTorrent = (url: string) => {
    navigator.clipboard.writeText(url).then(
      () => message.success('磁力链接已复制'),
      () => message.error('复制失败'),
    );
  };

  const r = resourceData || resource;
  const open = resource !== null;

  const parsedItems: Array<{ key: string; label: string; children: React.ReactNode }> = r
    ? [
        { key: 'subtitle_group', label: '字幕组', children: r.subtitle_group || '—' },
        {
          key: 'episode',
          label: '集数',
          children: r.episode != null ? (r.season != null ? `S${r.season}E${r.episode}` : `第 ${r.episode} 集`) : '—',
        },
        { key: 'resolution', label: '分辨率', children: r.resolution || '—' },
        { key: 'source', label: '来源', children: r.source || '—' },
        { key: 'video_codec', label: '视频编码', children: r.video_codec || '—' },
        { key: 'audio_codec', label: '音频编码', children: r.audio_codec || '—' },
        { key: 'subtitle_type', label: '字幕类型', children: r.subtitle_type || '—' },
        { key: 'container', label: '容器格式', children: r.container || '—' },
        {
          key: 'file_size',
          label: '文件大小',
          children: r.file_size != null ? formatBytes(r.file_size) : '—',
        },
        {
          key: 'published_at',
          label: '发布时间',
          children: r.published_at ? formatDate(r.published_at) : '—',
        },
        {
          key: 'detail_url',
          label: '详情页',
          children: r.detail_url ? (
            <a
              href={r.detail_url}
              target="_blank"
              rel="noreferrer"
              style={{ color: '#57c1ff' }}
            >
              打开
            </a>
          ) : (
            '—'
          ),
        },
        {
          key: 'torrent_url',
          label: '下载链接',
          children: r.torrent_url ? (
            <Space size={4}>
              <Tooltip title={r.torrent_url}>
                <Text
                  ellipsis
                  style={{ maxWidth: 220, color: '#57c1ff', fontSize: 12 }}
                >
                  {r.torrent_url.startsWith('magnet:')
                    ? 'magnet:?xt=...'
                    : r.torrent_url}
                </Text>
              </Tooltip>
              <Button
                type="text"
                size="small"
                icon={<Copy size={12} />}
                onClick={() => copyTorrent(r.torrent_url)}
              />
            </Space>
          ) : (
            '—'
          ),
        },
      ]
    : [];

  return (
    <>
      <Drawer
        title={r ? r.title_cn || r.title_raw : '资源详情'}
        open={open}
        onClose={onClose}
        width={window.innerWidth < 768 ? '100%' : 520}
        destroyOnHidden
        styles={{ body: { padding: 20 } }}
        extra={
          <Button
            type="primary"
            size="small"
            icon={<Pencil size={12} />}
            onClick={() => setCorrectionOpen(true)}
          >
            修正元数据
          </Button>
        }
      >
        {r && (
          <div>
            {/* Raw title */}
            <Paragraph style={{ color: '#9c9c9d', fontSize: 12, marginBottom: 16 }}>
              {r.title_raw}
            </Paragraph>

            {/* Metadata section */}
            <div style={{ marginBottom: 16 }}>
              <Text strong style={{ fontSize: 13, display: 'block', marginBottom: 10 }}>
                元数据
              </Text>
              {metaLoading ? (
                <div style={{ textAlign: 'center', padding: '24px 0' }}>
                  <Spin />
                </div>
              ) : meta ? (
                <div
                  style={{
                    display: 'flex',
                    gap: 12,
                    padding: 12,
                    border: '1px solid rgba(89,212,153,0.2)',
                    borderRadius: 8,
                    background: 'rgba(89,212,153,0.05)',
                  }}
                >
                  <PosterBlock url={meta.poster_url} />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <Space size={6} style={{ marginBottom: 6 }}>
                      <Text strong>{meta.title}</Text>
                      <Tag color={meta.type === 'series' ? 'blue' : 'green'}>
                        {meta.type === 'series' ? '剧集' : '电影'}
                      </Tag>
                    </Space>
                  </div>
                </div>
              ) : (
                <div
                  style={{
                    padding: 16,
                    border: '1px dashed rgba(255,255,255,0.12)',
                    borderRadius: 8,
                    textAlign: 'center',
                  }}
                >
                  <Empty
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                    description={false}
                    style={{ marginBottom: 8 }}
                  />
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    尚未关联元数据
                  </Text>
                  <div style={{ marginTop: 8 }}>
                    <Button
                      size="small"
                      icon={<Pencil size={12} />}
                      onClick={() => setCorrectionOpen(true)}
                    >
                      手动修正
                    </Button>
                  </div>
                </div>
              )}
            </div>

            <Divider style={{ margin: '16px 0', borderColor: '#242728' }} />

            {/* Parsed details */}
            <Text strong style={{ fontSize: 13, display: 'block', marginBottom: 10 }}>
              解析字段
            </Text>
            <Descriptions
              column={1}
              size="small"
              labelStyle={{ color: '#9c9c9d', width: 100, padding: '4px 8px' }}
              contentStyle={{ color: '#cdcdcd', padding: '4px 8px' }}
              style={{ fontSize: 12 }}
              items={parsedItems}
            />
          </div>
        )}
      </Drawer>

      {r && (
        <MetadataCorrectionModal
          resourceId={r.id}
          open={correctionOpen}
          onClose={() => setCorrectionOpen(false)}
          onCorrected={() => {
            setCorrectionOpen(false);
            loadMeta(r.id);
            onCorrected?.();
          }}
        />
      )}
    </>
  );
}
