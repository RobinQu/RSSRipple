import { useState, useEffect, useRef, useCallback } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { Form, Input, InputNumber, Select, Button, Space, Card, Typography, App, Tooltip, Spin, Tag } from 'antd';
import { CheckCircle, XCircle, Loader2, Info, Wand2, Plus, Trash2, X, AlertTriangle, Rss, RefreshCw } from 'lucide-react';
import { channelsApi } from '../api/channels';
import type { ChannelStatus, PreviewEntry } from '../types';

type Mode = 'create' | 'edit';

/** Shape of a single field-mapping rule in our editor state. */
interface MappingRule {
  _id: string;
  field: string;
  source: string;
  regex: string;
  group: number;
  transform: string | null;
}

let ruleCounter = 0;
function nextRuleId() {
  return `rule_${++ruleCounter}_${Date.now()}`;
}

function emptyRule(): MappingRule {
  return { _id: nextRuleId(), field: '', source: '', regex: '', group: 0, transform: null };
}

/** Parse a raw field_mapping JSON object into editor state. */
function parseFieldMapping(fm: Record<string, unknown>): {
  listSource: string;
  rules: MappingRule[];
} {
  const listLocator = fm.list_locator as Record<string, unknown> | undefined;
  const listSource = (listLocator?.source as string) || 'entries';
  const fieldMappings = (fm.field_mappings as Record<string, Record<string, unknown>>) || {};
  const rules: MappingRule[] = Object.entries(fieldMappings).map(([field, cfg]) => ({
    _id: nextRuleId(),
    field,
    source: (cfg.source as string) || '',
    regex: (cfg.regex as string) || '',
    group: (cfg.group as number) || 0,
    transform: (cfg.transform as string) || null,
  }));
  return { listSource, rules };
}

/** Serialize editor state back to the field_mapping JSON shape. */
function serializeFieldMapping(listSource: string, rules: MappingRule[]): Record<string, unknown> {
  return {
    list_locator: { source: listSource || 'entries' },
    field_mappings: Object.fromEntries(
      rules
        .filter(r => r.field && r.source)
        .map(r => [
          r.field,
          {
            source: r.source,
            ...(r.regex ? { regex: r.regex } : {}),
            ...(r.group ? { group: r.group } : {}),
            ...(r.transform ? { transform: r.transform } : {}),
          },
        ]),
    ),
  };
}

const TARGET_FIELDS = [
  { value: 'title_cn', label: 'Chinese title', required: false },
  { value: 'title_en', label: 'English title', required: false },
  { value: 'subtitle_group', label: 'Release group', required: false },
  { value: 'episode', label: 'Episode number', required: false },
  { value: 'resolution', label: 'Resolution', required: false },
  { value: 'source', label: 'Source type', required: false },
  { value: 'video_codec', label: 'Video codec', required: false },
  { value: 'audio_codec', label: 'Audio codec', required: false },
  { value: 'subtitle_type', label: 'Subtitle type', required: false },
  { value: 'container', label: 'Container format', required: false },
  { value: 'file_size', label: 'File size (bytes)', required: false },
  { value: 'torrent_url', label: 'Download URL', required: true },
  { value: 'detail_url', label: 'Detail page URL', required: false },
  { value: 'published_at', label: 'Publication date', required: false },
];

const TRANSFORM_OPTIONS = [
  { value: '', label: 'None' },
  { value: 'int', label: 'Integer' },
  { value: 'float', label: 'Decimal number' },
  { value: 'iso_datetime', label: 'ISO date/time' },
  { value: 'lowercase', label: 'Lowercase' },
  { value: 'uppercase', label: 'Uppercase' },
];

function confidenceStyle(confidence: string) {
  const c = confidence.toLowerCase();
  if (c === 'high') return { color: '#59d499', label: 'High confidence' };
  if (c === 'medium') return { color: '#ffc533', label: 'Medium confidence' };
  return { color: '#ff9f43', label: 'Low confidence' };
}

/** Sidebar status during AI analysis. */
type SidebarStatus = 'streaming' | 'done' | 'error';

export default function ChannelForm() {
  const navigate = useNavigate();
  const { id } = useParams<{ id: string }>();
  const mode: Mode = id ? 'edit' : 'create';

  const [form] = Form.useForm();
  const { message } = App.useApp();
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const formTokenRef = useRef<string | null>(null);
  const [urlStatus, setUrlStatus] = useState<'idle' | 'checking' | 'valid' | 'invalid'>('idle');
  const [urlMessage, setUrlMessage] = useState('');
  const [downloadableCount, setDownloadableCount] = useState(0);
  const [loading, setLoading] = useState(mode === 'edit');

  // Field mapping rule editor state
  const [listLocatorSource, setListLocatorSource] = useState('entries');
  const [rules, setRules] = useState<MappingRule[]>([]);

  // Title extraction state
  const [titleMethod, setTitleMethod] = useState<string>('llm');
  const [titleRegex, setTitleRegex] = useState<string>('');
  const [generatingRegex, setGeneratingRegex] = useState(false);

  // Track URL value so we can enable Analyze in create mode
  const [urlValue, setUrlValue] = useState('');

  // Analyze state
  const [analyzing, setAnalyzing] = useState(false);
  const [analyzeConfidence, setAnalyzeConfidence] = useState<string | null>(null);

  // Sidebar state for streaming AI output
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [streamText, setStreamText] = useState('');
  const [sidebarStatus, setSidebarStatus] = useState<SidebarStatus>('streaming');
  const [sidebarError, setSidebarError] = useState('');
  const streamBodyRef = useRef<HTMLDivElement>(null);
  const autoHideTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Feed preview state
  const [previewEntries, setPreviewEntries] = useState<PreviewEntry[]>([]);
  const [previewParsed, setPreviewParsed] = useState<Record<string, unknown>[]>([]);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState('');

  const fetchPreview = useCallback(async (url: string, fieldMapping?: Record<string, unknown> | null) => {
    if (!url) {
      setPreviewEntries([]);
      setPreviewParsed([]);
      return;
    }
    setPreviewLoading(true);
    setPreviewError('');
    try {
      const res = await channelsApi.previewFeed(url, fieldMapping);
      if (res.success && res.data) {
        setPreviewEntries(res.data.entries);
        setPreviewParsed(res.data.parsed);
      } else {
        setPreviewEntries([]);
        setPreviewParsed([]);
        setPreviewError(res.error?.message || 'Failed to load feed');
      }
    } catch {
      setPreviewEntries([]);
      setPreviewParsed([]);
      setPreviewError('Failed to fetch feed');
    } finally {
      setPreviewLoading(false);
    }
  }, []);

  // Auto-scroll stream body to bottom as new text arrives
  useEffect(() => {
    if (streamBodyRef.current) {
      streamBodyRef.current.scrollTop = streamBodyRef.current.scrollHeight;
    }
  }, [streamText]);

  // Auto-hide sidebar 2 seconds after analysis completes
  useEffect(() => {
    if (sidebarStatus === 'done' && sidebarOpen) {
      autoHideTimerRef.current = setTimeout(() => {
        setSidebarOpen(false);
      }, 2000);
    }
    return () => {
      if (autoHideTimerRef.current) {
        clearTimeout(autoHideTimerRef.current);
        autoHideTimerRef.current = null;
      }
    };
  }, [sidebarStatus, sidebarOpen]);

  // Fetch a single-use form token on mount so the backend can reject duplicate submits
  useEffect(() => {
    channelsApi.getFormToken().then(r => {
      if (r.success) formTokenRef.current = r.data.token;
    });
  }, []);

  // Load channel data in edit mode
  useEffect(() => {
    if (mode === 'edit' && id) {
      channelsApi.get(id).then(r => {
        if (r.success) {
          const ch = r.data;
          form.setFieldsValue({
            name: ch.name,
            url: ch.url,
            fetch_interval: ch.fetch_interval,
            status: ch.status,
          });
          setUrlValue(ch.url);
          if (ch.field_mapping) {
            const { listSource, rules: parsed } = parseFieldMapping(ch.field_mapping as Record<string, unknown>);
            setListLocatorSource(listSource);
            setRules(parsed);
            // Auto-fetch preview with the loaded URL + field_mapping
            fetchPreview(ch.url, ch.field_mapping as Record<string, unknown>);
          } else {
            // Auto-fetch preview with just the URL (no field_mapping yet)
            fetchPreview(ch.url);
          }
          setTitleMethod(ch.title_extraction_method || 'none');
          setTitleRegex(ch.title_extraction_regex || '');
        } else {
          message.error('Could not load channel');
          navigate('/channels');
        }
        setLoading(false);
      });
    }
  }, [mode, id, fetchPreview]);

  // Debounced re-preview when field mapping rules change
  useEffect(() => {
    if (mode !== 'edit' || !id) return; // Only in edit mode (URL is known)
    const url = form.getFieldValue('url');
    if (!url || previewEntries.length === 0) return; // Only if we already have entries

    const timer = setTimeout(() => {
      const validRules = rules.filter(r => r.field && r.source);
      const fieldMapping = validRules.length > 0 || listLocatorSource !== 'entries'
        ? serializeFieldMapping(listLocatorSource, validRules)
        : null;
      fetchPreview(url, fieldMapping);
    }, 500);

    return () => clearTimeout(timer);
  }, [rules, listLocatorSource, mode, id, fetchPreview, previewEntries.length, form]);

  const validateUrl = async () => {
    const url = form.getFieldValue('url');
    if (!url) return;
    setUrlStatus('checking');
    const res = await channelsApi.validateUrl(url);
    if (res.success && res.data.valid) {
      setUrlStatus('valid');
      setDownloadableCount(res.data.downloadable_count);
      setUrlMessage(`Valid feed: ${res.data.item_count} items, ${res.data.downloadable_count} with downloads`);
      // Also fetch preview entries
      fetchPreview(url);
    } else {
      setUrlStatus('invalid');
      setUrlMessage(res.data?.message || 'Invalid URL');
    }
  };

  const updateRule = (ruleId: string, patch: Partial<MappingRule>) => {
    setRules(prev => prev.map(r => (r._id === ruleId ? { ...r, ...patch } : r)));
    setAnalyzeConfidence(null);
  };

  const removeRule = (ruleId: string) => {
    setRules(prev => prev.filter(r => r._id !== ruleId));
    setAnalyzeConfidence(null);
  };

  const addRule = () => {
    setRules(prev => [...prev, emptyRule()]);
  };

  const closeSidebar = useCallback(() => {
    setSidebarOpen(false);
    if (autoHideTimerRef.current) {
      clearTimeout(autoHideTimerRef.current);
      autoHideTimerRef.current = null;
    }
  }, []);

  const handleAnalyze = async () => {
    const currentUrl = mode === 'edit' ? urlValue : form.getFieldValue('url');
    if (!currentUrl) {
      message.warning('Enter a feed URL first');
      return;
    }
    setAnalyzing(true);
    setAnalyzeConfidence(null);
    setSidebarOpen(true);
    setStreamText('');
    setSidebarError('');
    setSidebarStatus('streaming');

    try {
      const res = mode === 'edit' && id
        ? await channelsApi.analyzeStream(id)
        : await channelsApi.analyzeUrlStream(currentUrl);

      if (!res.ok) {
        let errMsg = 'Analysis request failed';
        try {
          const body = await res.json();
          errMsg = body?.error?.message || body?.message || errMsg;
        } catch { /* use default */ }
        setSidebarStatus('error');
        setSidebarError(errMsg);
        setAnalyzing(false);
        return;
      }

      const reader = res.body!.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            try {
              const data = JSON.parse(line.slice(6));
              if (data.type === 'delta') {
                setStreamText(prev => prev + data.content);
              } else if (data.type === 'done') {
                const mapping = data.field_mapping;
                if (!mapping || Object.keys(mapping).length === 0) {
                  setSidebarStatus('error');
                  setSidebarError('Analysis returned no rules. Make sure the LLM API key is configured.');
                } else {
                  const { listSource, rules: parsed } = parseFieldMapping(mapping as Record<string, unknown>);
                  setListLocatorSource(listSource);
                  setRules(parsed);
                  setAnalyzeConfidence(data.confidence);
                  setSidebarStatus('done');
                  message.success('Field mapping generated');
                  // Immediately refresh preview with the new mapping
                  const previewUrl = mode === 'edit' ? urlValue : form.getFieldValue('url');
                  if (previewUrl) {
                    const validRules = parsed.filter(r => r.field && r.source);
                    const fieldMapping = validRules.length > 0 ? serializeFieldMapping(listSource, validRules) : null;
                    fetchPreview(previewUrl, fieldMapping);
                  }
                }
              } else if (data.type === 'error') {
                setSidebarStatus('error');
                setSidebarError(data.message || 'Analysis failed');
              }
            } catch {
              // Skip malformed JSON lines
            }
          }
        }
      }
    } catch {
      setSidebarStatus('error');
      setSidebarError('Analysis failed. Check that the LLM API key is configured.');
    }
    setAnalyzing(false);
  };

  const handleSubmit = async (values: {
    name: string;
    url: string;
    fetch_interval: number;
    status: ChannelStatus;
  }) => {
    // 1. Check incomplete rules (field or source blank while the other is filled)
    const incomplete = rules.filter(r => (r.field && !r.source) || (!r.field && r.source));
    if (incomplete.length > 0) {
      message.warning('Some rules are incomplete and will be skipped. Each rule needs both a target field and a source.');
      return;
    }

    // Build valid rules and field_mapping
    const validRules = rules.filter(r => r.field && r.source);
    const fieldMapping = validRules.length > 0 || listLocatorSource !== 'entries'
      ? serializeFieldMapping(listLocatorSource, validRules)
      : null;

    // 2. Check for duplicate target fields
    const fieldNames = validRules.map(r => r.field);
    const dupes = fieldNames.filter((f, i) => fieldNames.indexOf(f) !== i);
    if (dupes.length > 0) {
      message.error(`Duplicate target field: "${dupes[0]}". Each field can only appear once.`);
      return;
    }

    // 3. Check required field: torrent_url must exist when rules are present
    if (validRules.length > 0) {
      const hasTorrentUrl = validRules.some(r => r.field === 'torrent_url');
      if (!hasTorrentUrl) {
        message.warning('A "Download URL" (torrent_url) rule is required. Without it, the system cannot download anything.');
        return;
      }
    }

    // 4. In create mode, block if URL was validated and found invalid
    if (mode === 'create' && urlStatus === 'invalid') {
      message.error('The RSS URL failed validation. Please use a valid feed URL.');
      return;
    }

    // Guard against double-submit: ref check is synchronous so it catches rapid
    // clicks that arrive before React re-renders the disabled button state.
    if (savingRef.current) return;
    savingRef.current = true;
    setSaving(true);

    const token = formTokenRef.current;
    // Consume the token locally so a second in-flight click cannot reuse it
    formTokenRef.current = null;

    try {
      if (mode === 'create') {
        const res = await channelsApi.create({
          name: values.name,
          url: values.url,
          fetch_interval: values.fetch_interval,
          type: 'rss_feed',
          field_mapping: fieldMapping,
          title_extraction_method: titleMethod,
          title_extraction_regex: titleMethod === 'regex' ? (titleRegex || null) : null,
        }, token ?? undefined);
        if (res.success) {
          message.success('Channel created');
          navigate('/channels');
        } else {
          const serverMsg = res.error?.message;
          if (serverMsg) message.error(serverMsg);
          // Rotate the token so the user can retry without reloading
          channelsApi.getFormToken().then(r => { if (r.success) formTokenRef.current = r.data.token; });
        }
      } else if (mode === 'edit' && id) {
        const res = await channelsApi.update(id, {
          name: values.name,
          fetch_interval: values.fetch_interval,
          status: values.status,
          field_mapping: fieldMapping,
          title_extraction_method: titleMethod,
          title_extraction_regex: titleMethod === 'regex' ? (titleRegex || null) : null,
        }, token ?? undefined);
        if (res.success) {
          message.success('Channel updated');
          navigate(`/channels/${id}`);
        } else {
          const serverMsg = res.error?.message;
          if (serverMsg) message.error(serverMsg);
          channelsApi.getFormToken().then(r => { if (r.success) formTokenRef.current = r.data.token; });
        }
      }
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  };

  if (loading) {
    return <Spin style={{ display: 'flex', justifyContent: 'center', padding: 48 }} />;
  }

  // Shared inline styles for rule cards
  const ruleCardStyle: React.CSSProperties = {
    background: '#0a0a0a',
    border: '1px solid #242728',
    borderRadius: 8,
    padding: '12px 14px',
    marginBottom: 8,
  };

  const ruleRowStyle: React.CSSProperties = {
    display: 'grid',
    gridTemplateColumns: '1fr 1fr',
    gap: 10,
    marginBottom: 8,
  };

  const ruleSecondaryRowStyle: React.CSSProperties = {
    display: 'grid',
    gridTemplateColumns: '1fr 80px 140px auto',
    gap: 10,
    alignItems: 'end',
  };

  const miniLabelStyle: React.CSSProperties = {
    fontSize: 11,
    color: '#6a6b6c',
    marginBottom: 3,
    display: 'block',
    letterSpacing: '0.02em',
  };

  return (
    <div className="channel-form-layout">
      {/* ─── Left column: form ─── */}
      <div className="channel-form-main">
        <Typography.Title level={3} style={{ marginBottom: 24 }}>
          {mode === 'create' ? 'Create Channel' : 'Edit Channel'}
        </Typography.Title>

        <Card>
          <Form
            form={form}
            layout="vertical"
            onFinish={handleSubmit}
            initialValues={mode === 'create' ? { name: '', url: '', fetch_interval: 1800 } : undefined}
          >
            <Form.Item name="name" label="Name" rules={[{ required: true, message: 'Please enter a channel name' }]}>
              <Input placeholder="My anime feed" />
            </Form.Item>

            <Form.Item
              name="url"
              label={
                <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                  RSS URL
                  {mode === 'edit' && (
                    <Tooltip title="Feed URL cannot be changed after creation">
                      <Info size={14} style={{ color: '#6a6b6c', cursor: 'help' }} />
                    </Tooltip>
                  )}
                </span>
              }
              rules={mode === 'create' ? [{ required: true, message: 'Please enter the RSS URL' }] : []}
            >
              {mode === 'edit' ? (
                <Input
                  disabled
                  style={{
                    color: '#6a6b6c',
                    cursor: 'not-allowed',
                    opacity: 0.7,
                  }}
                />
              ) : (
                <Space.Compact style={{ width: '100%' }}>
                  <Input
                    placeholder="https://mikanani.me/RSS/..."
                    onChange={e => { setUrlStatus('idle'); setUrlValue(e.target.value); }}
                  />
                  <Button onClick={validateUrl}>Validate</Button>
                </Space.Compact>
              )}
            </Form.Item>

            {mode === 'edit' && (
              <div style={{ marginBottom: 16, marginTop: -8 }}>
                <Typography.Text style={{ fontSize: 12, color: '#6a6b6c' }}>
                  Feed URL cannot be changed after creation
                </Typography.Text>
              </div>
            )}

            {/* URL validation feedback — only in create mode */}
            {mode === 'create' && urlStatus === 'checking' && (
              <div style={{ marginBottom: 16, display: 'flex', alignItems: 'center', gap: 6 }}>
                <Loader2 size={14} style={{ animation: 'spin 1s linear infinite' }} />
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>Checking...</Typography.Text>
              </div>
            )}
            {mode === 'create' && urlStatus === 'valid' && (
              <div style={{ marginBottom: 16 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <CheckCircle size={14} color="#52c41a" />
                  <Typography.Text style={{ fontSize: 12, color: '#52c41a' }}>{urlMessage}</Typography.Text>
                </div>
                {downloadableCount === 0 && (
                  <Typography.Text style={{ fontSize: 12, color: '#faad14' }}>
                    Warning: no torrent files or magnet links found in feed entries
                  </Typography.Text>
                )}
              </div>
            )}
            {mode === 'create' && urlStatus === 'invalid' && (
              <div style={{ marginBottom: 16, display: 'flex', alignItems: 'center', gap: 6 }}>
                <XCircle size={14} color="#ff4d4f" />
                <Typography.Text style={{ fontSize: 12, color: '#ff4d4f' }}>{urlMessage}</Typography.Text>
              </div>
            )}

            <Form.Item name="fetch_interval" label="Fetch Interval (seconds)" rules={[{ required: true, message: 'Required' }]}>
              <InputNumber min={60} style={{ width: 160 }} />
            </Form.Item>

            {/* Edit-only: status field */}
            {mode === 'edit' && (
              <Form.Item name="status" label="Status">
                <Select
                  options={[
                    { value: 'active', label: 'Active' },
                    { value: 'inactive', label: 'Inactive' },
                  ]}
                  style={{ width: 160 }}
                />
              </Form.Item>
            )}

            {/* ─── Field Mapping: Rule Editor ─── */}
            <div style={{ marginTop: 8 }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14 }}>
                <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                  <Typography.Text strong style={{ fontSize: 14 }}>Field Mapping</Typography.Text>
                  <Tooltip title="Extraction rules define how to pull structured data from each RSS entry. Use AI to generate or add rules manually.">
                    <Info size={14} style={{ color: '#6a6b6c', cursor: 'help' }} />
                  </Tooltip>
                </span>
                <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                  {rules.length > 0 && (
                    <Typography.Text style={{ fontSize: 12, color: '#59d499' }}>
                      {rules.filter(r => r.field && r.source).length} rule{rules.filter(r => r.field && r.source).length !== 1 ? 's' : ''}
                    </Typography.Text>
                  )}
                  {analyzeConfidence && (
                    <Typography.Text style={{ fontSize: 12, color: confidenceStyle(analyzeConfidence).color }}>
                      {confidenceStyle(analyzeConfidence).label}
                    </Typography.Text>
                  )}
                  {(mode === 'edit' || !!urlValue.trim()) && (
                    <Button
                      size="small"
                      type="text"
                      icon={<Wand2 size={13} />}
                      loading={analyzing}
                      onClick={handleAnalyze}
                      style={{
                        color: '#9c9c9d',
                        fontSize: 12,
                        height: 26,
                        padding: '0 8px',
                      }}
                    >
                      Analyze with AI
                    </Button>
                  )}
                </span>
              </div>

              {/* Section 1: List Locator */}
              <div style={ruleCardStyle}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
                  <Typography.Text style={{ fontSize: 12, color: '#9c9c9d', fontWeight: 500 }}>
                    Entry List Location
                  </Typography.Text>
                  <Tooltip title="Where to find the list of entries in the RSS feed. For most feeds this is always &quot;entries&quot;.">
                    <Info size={12} style={{ color: '#434345', cursor: 'help' }} />
                  </Tooltip>
                </div>
                <div style={{ maxWidth: 260 }}>
                  <span style={miniLabelStyle}>Source path</span>
                  <Input
                    value={listLocatorSource}
                    onChange={e => {
                      setListLocatorSource(e.target.value);
                      setAnalyzeConfidence(null);
                    }}
                    placeholder="entries"
                    style={{
                      fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
                      fontSize: 13,
                    }}
                  />
                </div>
              </div>

              {/* Section 2: Field Mapping Rules */}
              {rules.length === 0 && (
                <div
                  style={{
                    ...ruleCardStyle,
                    textAlign: 'center',
                    padding: '28px 16px',
                    borderColor: '#1a1a1a',
                  }}
                >
                  <Typography.Text style={{ fontSize: 13, color: '#434345' }}>
                    No extraction rules yet.
                  </Typography.Text>
                  <br />
                  <Typography.Text style={{ fontSize: 12, color: '#434345' }}>
                    {urlValue.trim()
                      ? 'Add rules manually or use "Analyze with AI" to generate them from the feed.'
                      : 'Enter a feed URL above, then use "Analyze with AI" to auto-generate rules.'}
                  </Typography.Text>
                </div>
              )}

              {rules.map(rule => (
                <div key={rule._id} style={ruleCardStyle}>
                  {/* Primary row: Target Field + Source */}
                  <div style={ruleRowStyle}>
                    <div>
                      <span style={miniLabelStyle}>Target field</span>
                      <Select
                        value={rule.field || undefined}
                        onChange={v => updateRule(rule._id, { field: v })}
                        placeholder="Select a field"
                        style={{ width: '100%' }}
                        options={TARGET_FIELDS.map(f => ({
                          ...f,
                          label: f.required
                            ? `${f.label}  (required)`
                            : f.label,
                          disabled: rules.some(r => r._id !== rule._id && r.field === f.value),
                        }))}
                        showSearch
                        filterOption={(input, option) =>
                          (option?.label as string)?.toLowerCase().includes(input.toLowerCase())
                        }
                      />
                    </div>
                    <div>
                      <span style={miniLabelStyle}>Source path</span>
                      <Input
                        value={rule.source}
                        onChange={e => updateRule(rule._id, { source: e.target.value })}
                        placeholder="title, enclosures[0].url ..."
                        style={{
                          fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
                          fontSize: 13,
                        }}
                      />
                    </div>
                  </div>

                  {/* Secondary row: Regex + Group + Transform + Delete */}
                  <div style={ruleSecondaryRowStyle}>
                    <div>
                      <span style={miniLabelStyle}>Regex (optional)</span>
                      <Input
                        value={rule.regex}
                        onChange={e => updateRule(rule._id, { regex: e.target.value })}
                        placeholder="e.g. -\s*(\d+)"
                        style={{
                          fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
                          fontSize: 12,
                        }}
                      />
                    </div>
                    <div>
                      <span style={miniLabelStyle}>Group</span>
                      <InputNumber
                        value={rule.group}
                        onChange={v => updateRule(rule._id, { group: v ?? 0 })}
                        min={0}
                        max={99}
                        style={{ width: '100%' }}
                        size="small"
                      />
                    </div>
                    <div>
                      <span style={miniLabelStyle}>Transform</span>
                      <Select
                        value={rule.transform || ''}
                        onChange={v => updateRule(rule._id, { transform: v || null })}
                        options={TRANSFORM_OPTIONS}
                        style={{ width: '100%' }}
                        size="small"
                      />
                    </div>
                    <Tooltip title="Remove rule">
                      <Button
                        type="text"
                        size="small"
                        icon={<Trash2 size={14} />}
                        onClick={() => removeRule(rule._id)}
                        style={{
                          color: '#434345',
                          marginBottom: 2,
                        }}
                      />
                    </Tooltip>
                  </div>
                </div>
              ))}

              {/* Required-field hint below rules */}
              {rules.length > 0 && !rules.some(r => r.field === 'torrent_url') && (
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 6,
                    padding: '8px 12px',
                    marginTop: 4,
                    borderRadius: 6,
                    background: 'rgba(255, 197, 51, 0.08)',
                    border: '1px solid rgba(255, 197, 51, 0.2)',
                  }}
                >
                  <AlertTriangle size={14} color="#ffc533" />
                  <Typography.Text style={{ fontSize: 12, color: '#ffc533' }}>
                    A Download URL (torrent_url) rule is required for the system to work.
                  </Typography.Text>
                </div>
              )}

              {/* Add Rule button */}
              <Button
                type="dashed"
                icon={<Plus size={14} />}
                onClick={addRule}
                block
                style={{
                  color: '#6a6b6c',
                  borderColor: '#242728',
                  height: 38,
                  marginTop: 4,
                }}
              >
                Add Rule
              </Button>
            </div>

            {/* ─── Title Extraction ─── */}
            <div style={{ marginTop: 8 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 14 }}>
                <Typography.Text strong style={{ fontSize: 14 }}>Title Extraction</Typography.Text>
                <Tooltip title="Controls how the system extracts a clean, searchable title from raw RSS titles before looking up metadata on TMDB/TVDB.">
                  <Info size={14} style={{ color: '#6a6b6c', cursor: 'help' }} />
                </Tooltip>
              </div>

              <div style={ruleCardStyle}>
                <span style={miniLabelStyle}>Extraction Method</span>
                <Select
                  value={titleMethod}
                  onChange={(v) => setTitleMethod(v)}
                  style={{ width: '100%', marginTop: 4 }}
                  options={[
                    { value: 'none', label: 'None — use field mapping title as-is' },
                    { value: 'regex', label: 'Regex — pattern-based cleanup (editable)' },
                    { value: 'llm', label: 'LLM — AI-powered extraction (no regex)' },
                  ]}
                />

                {/* Method: regex — show regex input + generate button */}
                {titleMethod === 'regex' && (
                  <div style={{ marginTop: 12 }}>
                    <span style={miniLabelStyle}>Cleanup Regex</span>
                    <Space.Compact style={{ width: '100%', marginTop: 4 }}>
                      <Input
                        value={titleRegex}
                        onChange={(e) => setTitleRegex(e.target.value)}
                        placeholder="e.g. ^(.+?)\s*(?:Season|第.*?季|S\d+)"
                        style={{
                          fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
                          fontSize: 13,
                        }}
                      />
                      {mode === 'edit' && (
                        <Button
                          icon={<Wand2 size={14} />}
                          loading={generatingRegex}
                          onClick={async () => {
                            if (!id) return;
                            setGeneratingRegex(true);
                            const res = await channelsApi.generateTitleRegex(id);
                            setGeneratingRegex(false);
                            if (res.success && res.data?.regex) {
                              setTitleRegex(res.data.regex);
                              message.success('Title regex generated by AI');
                            } else {
                              message.error(res.error?.message || 'Failed to generate regex');
                            }
                          }}
                        >
                          Generate with AI
                        </Button>
                      )}
                    </Space.Compact>
                    <Typography.Text style={{ fontSize: 11, color: '#6a6b6c', marginTop: 4, display: 'block' }}>
                      The regex should have a capture group (group 1) for the core title. The LLM generates an initial version from feed samples — you can edit it.
                    </Typography.Text>
                    {mode === 'create' && (
                      <Typography.Text style={{ fontSize: 11, color: '#ffc533', marginTop: 4, display: 'block' }}>
                        "Generate with AI" is available after the channel is created.
                      </Typography.Text>
                    )}
                  </div>
                )}

                {/* Method: llm — show info text */}
                {titleMethod === 'llm' && (
                  <div style={{ marginTop: 12, padding: '8px 12px', background: 'rgba(87,193,255,0.08)', border: '1px solid rgba(87,193,255,0.2)', borderRadius: 6 }}>
                    <Typography.Text style={{ fontSize: 12, color: '#57c1ff' }}>
                      The LLM will extract the core title from each RSS entry at metadata lookup time. No regex needed — results are cached for performance.
                    </Typography.Text>
                  </div>
                )}
              </div>
            </div>

            <Form.Item style={{ marginTop: 24 }}>
              <Space>
                <Button type="primary" htmlType="submit" loading={saving}>
                  {mode === 'create' ? 'Create Channel' : 'Save Changes'}
                </Button>
                <Button onClick={() => navigate(mode === 'edit' ? `/channels/${id}` : '/channels')}>
                  Cancel
                </Button>
              </Space>
            </Form.Item>
          </Form>
        </Card>
      </div>

      {/* ─── Right column: AI Analysis + Feed Preview side by side ─── */}
      <div className="channel-form-right">
        {/* AI streaming panel (collapses horizontally when closed) */}
        <div className={`channel-form-stream ${sidebarOpen ? 'open' : ''}`}>
          {/* Header */}
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              padding: '10px 16px',
              borderBottom: '1px solid #1a1a1a',
            }}
          >
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
              <Wand2 size={14} style={{ color: '#9c9c9d' }} />
              <Typography.Text strong style={{ fontSize: 13, color: '#cdcdcd' }}>
                AI Analysis
              </Typography.Text>
            </span>
            <Button
              type="text"
              size="small"
              icon={<X size={14} />}
              onClick={closeSidebar}
              style={{ color: '#6a6b6c' }}
            />
          </div>

          {/* Body: streaming text */}
          <div
            ref={streamBodyRef}
            style={{
              flex: 1,
              overflow: 'auto',
              padding: '12px 16px',
              fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
              fontSize: 12,
              lineHeight: 1.65,
              color: '#9c9c9d',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
            }}
          >
            {streamText || (
              <span style={{ color: '#434345', fontStyle: 'italic' }}>
                {sidebarStatus === 'streaming' && 'Waiting for response...'}
                {sidebarStatus === 'error' && ''}
              </span>
            )}
            {/* Blinking cursor while streaming */}
            {sidebarStatus === 'streaming' && streamText && (
              <span className="stream-cursor" />
            )}
          </div>

          {/* Footer: status indicator */}
          <div
            style={{
              padding: '8px 16px',
              borderTop: '1px solid #1a1a1a',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
            }}
          >
            {sidebarStatus === 'streaming' && (
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                <Loader2 size={13} style={{ color: '#57c1ff', animation: 'spin 1s linear infinite' }} />
                <Typography.Text style={{ fontSize: 12, color: '#57c1ff' }}>
                  Analyzing...
                </Typography.Text>
              </span>
            )}
            {sidebarStatus === 'done' && (
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                <CheckCircle size={13} color="#59d499" />
                <Typography.Text style={{ fontSize: 12, color: '#59d499' }}>
                  Analysis complete
                </Typography.Text>
              </span>
            )}
            {sidebarStatus === 'error' && (
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                <XCircle size={13} color="#ff6161" />
                <Typography.Text style={{ fontSize: 12, color: '#ff6161' }}>
                  {sidebarError || 'Analysis failed'}
                </Typography.Text>
              </span>
            )}

            {sidebarStatus === 'error' && (
              <Button
                type="text"
                size="small"
                onClick={closeSidebar}
                style={{ fontSize: 12, color: '#6a6b6c', height: 24, padding: '0 6px' }}
              >
                Dismiss
              </Button>
            )}
          </div>
        </div>

        {/* Feed Preview panel */}
        <div className="channel-form-preview">

        {/* Preview header */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            padding: '14px 16px',
            borderBottom: '1px solid #1a1a1a',
          }}
        >
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
            <Rss size={14} style={{ color: '#9c9c9d' }} />
            <Typography.Text strong style={{ fontSize: 13, color: '#cdcdcd' }}>
              Feed Preview
            </Typography.Text>
            {previewEntries.length > 0 && (
              <Typography.Text style={{ fontSize: 12, color: '#6a6b6c' }}>
                {previewEntries.length} entries
              </Typography.Text>
            )}
          </span>
          {previewEntries.length > 0 && !previewLoading && (
            <Button
              type="text"
              size="small"
              icon={<RefreshCw size={13} />}
              onClick={() => {
                const url = form.getFieldValue('url');
                if (url) {
                  const validRules = rules.filter(r => r.field && r.source);
                  const fm = validRules.length > 0 ? serializeFieldMapping(listLocatorSource, validRules) : null;
                  fetchPreview(url, fm);
                }
              }}
              style={{ color: '#6a6b6c' }}
            />
          )}
        </div>

        {/* Preview body — scrollable */}
        <div style={{ flex: 1, overflow: 'auto', padding: 0 }}>
          {previewLoading ? (
            <div style={{ display: 'flex', justifyContent: 'center', padding: '48px 0' }}>
              <Spin />
            </div>
          ) : previewError ? (
            <div style={{ padding: '24px 16px', textAlign: 'center' }}>
              <Typography.Text style={{ fontSize: 13, color: '#ff6161' }}>{previewError}</Typography.Text>
            </div>
          ) : previewEntries.length === 0 ? (
            <div style={{ padding: '48px 16px', textAlign: 'center' }}>
              <Typography.Text style={{ fontSize: 13, color: '#434345' }}>
                {mode === 'edit' ? 'Loading feed...' : 'Enter a feed URL and click Validate to preview entries'}
              </Typography.Text>
            </div>
          ) : (
            previewEntries.map((entry, i) => {
              const parsed = previewParsed[i] || {};
              const hasParsed = Object.keys(parsed).length > 0;
              return (
                <div key={i} style={{ padding: '12px 16px', borderBottom: '1px solid #1a1a1a' }}>
                  {/* Raw title */}
                  <Typography.Text style={{ fontSize: 13, color: '#cdcdcd', display: 'block', marginBottom: 4, wordBreak: 'break-word' }}>
                    {entry.title || 'Untitled'}
                  </Typography.Text>
                  {/* Published date */}
                  {entry.published && (
                    <Typography.Text style={{ fontSize: 11, color: '#6a6b6c', display: 'block', marginBottom: hasParsed ? 8 : 0 }}>
                      {new Date(entry.published).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })}
                    </Typography.Text>
                  )}
                  {/* Parsed fields — show as small badges */}
                  {hasParsed && (
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginTop: 4 }}>
                      {Object.entries(parsed).filter(([, v]) => v != null && v !== '').map(([key, val]) => (
                        <Tag key={key} style={{ fontSize: 11, background: '#101111', border: '1px solid #242728', color: '#9c9c9d', marginInlineEnd: 0, borderRadius: 4, padding: '0 6px' }}>
                          <span style={{ color: '#6a6b6c' }}>{key}:</span> {String(val)}
                        </Tag>
                      ))}
                    </div>
                  )}
                </div>
              );
            })
          )}
        </div>
        </div> {/* end channel-form-preview */}
      </div> {/* end channel-form-right */}
    </div>
  );
}
