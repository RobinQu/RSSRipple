import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  App,
  Button,
  Divider,
  Form,
  Input,
  InputNumber,
  Modal,
  Segmented,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
} from 'antd';
import type { TableColumnsType } from 'antd';
import { organizeApi } from '../api/organize';
import FilterBuilder from './FilterBuilder';
import {
  findInvalidConditions,
  isFilterEmpty,
  nullIfEmptyFilter,
} from './filterUtils';
import { ORGANIZE_PRESET_MOVIE, ORGANIZE_PRESET_TV } from '../constants/organize';
import { formatBytes } from '../utils/format';
import type {
  BoolCondition,
  Library,
  OrganizePreviewOp,
  OrganizePreviewResponse,
  OrganizeRule,
} from '../types';

const { Text } = Typography;

/** Create / edit modal for an OrganizeRule, with a dry-run preview section. */
export default function OrganizeRuleFormModal({
  open,
  rule,
  libraries,
  onClose,
  onSaved,
}: {
  open: boolean;
  rule: OrganizeRule | null;
  libraries: Library[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);

  // null filter = match all; the switch toggles the FilterBuilder on/off.
  const [filterEnabled, setFilterEnabled] = useState(false);
  const [filterValue, setFilterValue] = useState<BoolCondition | null>(null);

  // Preview section state.
  const [previewKind, setPreviewKind] = useState<'notification' | 'resource'>('notification');
  const [previewId, setPreviewId] = useState('');
  const [previewCategory, setPreviewCategory] = useState('');
  const [previewing, setPreviewing] = useState(false);
  const [previewResult, setPreviewResult] = useState<OrganizePreviewResponse | null>(null);

  useEffect(() => {
    if (open) {
      form.setFieldsValue({
        name: rule?.name ?? '',
        priority: rule?.priority ?? 100,
        enabled: rule?.enabled ?? true,
        library_id: rule?.library_id ?? undefined,
        path_template: rule?.path_template ?? '',
        file_op: rule?.file_op ?? 'move',
        auto_execute: rule?.auto_execute ?? false,
      });
      setFilterEnabled(!isFilterEmpty(rule?.filter));
      setFilterValue(rule?.filter ?? null);
      setPreviewId('');
      setPreviewCategory('');
      setPreviewResult(null);
    }
  }, [open, rule, form]);

  const currentFilter = (): BoolCondition | null =>
    filterEnabled ? nullIfEmptyFilter(filterValue) : null;

  const submit = async () => {
    const values = await form.validateFields();
    if (filterEnabled) {
      const invalid = findInvalidConditions(filterValue);
      if (invalid.length > 0) {
        message.error(t('libraries.filterInvalid'));
        return;
      }
    }
    const body = {
      name: values.name.trim(),
      priority: values.priority ?? 100,
      enabled: values.enabled ?? true,
      filter: currentFilter(),
      library_id: values.library_id,
      path_template: values.path_template.trim(),
      file_op: values.file_op ?? 'move',
      auto_execute: values.auto_execute ?? false,
    };
    setSaving(true);
    const res = rule
      ? await organizeApi.updateRule(rule.id, body)
      : await organizeApi.createRule(body);
    setSaving(false);
    if (res.success) {
      message.success(t(rule ? 'libraries.ruleSaved' : 'libraries.ruleCreated'));
      onSaved();
      onClose();
    } else {
      message.error(res.error?.message || t(rule ? 'libraries.ruleSaveFailed' : 'libraries.ruleCreateFailed'));
    }
  };

  const runPreview = async () => {
    const values = await form.validateFields();
    const id = previewId.trim();
    if (!id) {
      message.error(t('libraries.previewIdRequired'));
      return;
    }
    setPreviewing(true);
    setPreviewResult(null);
    const res = await organizeApi.preview({
      ...(previewKind === 'notification' ? { notification_id: id } : { resource_id: id }),
      rule: {
        name: values.name || 'preview',
        filter: currentFilter(),
        library_id: values.library_id,
        path_template: values.path_template.trim(),
        file_op: values.file_op ?? 'move',
      },
      category: previewCategory.trim() || null,
    });
    setPreviewing(false);
    if (res.success) {
      setPreviewResult(res.data);
    } else {
      message.error(res.error?.message || t('libraries.previewFailed'));
    }
  };

  const previewColumns: TableColumnsType<OrganizePreviewOp> = [
    {
      title: t('organize.opType'),
      dataIndex: 'op_type',
      key: 'op_type',
      width: 80,
      render: (v: string) =>
        v === 'move' ? (
          <Tag color="green">{t('organize.opMove')}</Tag>
        ) : (
          <Tag>{t('organize.opKeep')}</Tag>
        ),
    },
    {
      title: t('organize.src'),
      dataIndex: 'src',
      key: 'src',
      render: (v: string) => (
        <Text ellipsis={{ tooltip: v }} style={{ maxWidth: 260 }}>{v}</Text>
      ),
    },
    {
      title: t('organize.dst'),
      dataIndex: 'dst',
      key: 'dst',
      render: (v: string | null) =>
        v ? <Text ellipsis={{ tooltip: v }} style={{ maxWidth: 260 }}>{v}</Text> : t('format.dash'),
    },
    {
      title: t('organize.size'),
      dataIndex: 'size',
      key: 'size',
      width: 90,
      render: (v: number) => formatBytes(v),
    },
    {
      title: t('organize.reason'),
      dataIndex: 'reason',
      key: 'reason',
      width: 120,
      render: (v: string) => v || t('format.dash'),
    },
  ];

  return (
    <Modal
      open={open}
      title={t(rule ? 'libraries.editRule' : 'libraries.newRule')}
      okText={t('common.save')}
      cancelText={t('common.cancel')}
      confirmLoading={saving}
      onOk={submit}
      onCancel={onClose}
      width={920}
      destroyOnHidden
    >
      <Form form={form} layout="vertical" style={{ marginTop: 12 }}>
        <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap' }}>
          <Form.Item
            name="name"
            label={t('common.name')}
            style={{ flex: '1 1 280px' }}
            rules={[{ required: true, message: t('libraries.ruleNameRequired') }]}
          >
            <Input maxLength={255} />
          </Form.Item>
          <Form.Item name="priority" label={t('libraries.priority')} style={{ width: 140 }}>
            <InputNumber style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="enabled" label={t('libraries.enabled')} valuePropName="checked">
            <Switch />
          </Form.Item>
          <Form.Item
            name="auto_execute"
            label={t('libraries.autoExecute')}
            valuePropName="checked"
            extra={t('libraries.autoExecuteExtra')}
          >
            <Switch />
          </Form.Item>
          <Form.Item
            name="file_op"
            label={t('libraries.fileOp')}
            style={{ width: 320 }}
          >
            <Select
              options={(['move', 'hardlink', 'copy'] as const).map((op) => ({
                value: op,
                label: (
                  <Space size={8}>
                    <span>{t(`libraries.fileOp_${op}`)}</span>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {t(`libraries.fileOpDesc_${op}`)}
                    </Text>
                  </Space>
                ),
              }))}
            />
          </Form.Item>
        </div>
        <Form.Item
          name="library_id"
          label={t('organize.library')}
          rules={[{ required: true, message: t('libraries.libraryRequired') }]}
        >
          <Select
            options={libraries.map((lib) => ({ value: lib.id, label: lib.name }))}
            placeholder={t('organize.selectLibrary')}
          />
        </Form.Item>
        <Form.Item
          name="path_template"
          label={t('libraries.template')}
          rules={[{ required: true, message: t('libraries.templateRequired') }]}
        >
          <Input.TextArea rows={2} style={{ fontFamily: 'monospace' }} />
        </Form.Item>
        <Space size={8} style={{ marginBottom: 16, flexWrap: 'wrap' }}>
          <Text type="secondary">{t('libraries.presets')}</Text>
          <Button size="small" onClick={() => form.setFieldValue('path_template', ORGANIZE_PRESET_TV)}>
            {t('libraries.presetTv')}
          </Button>
          <Button size="small" onClick={() => form.setFieldValue('path_template', ORGANIZE_PRESET_MOVIE)}>
            {t('libraries.presetMovie')}
          </Button>
        </Space>
        <Form.Item label={t('libraries.filter')}>
          <Space direction="vertical" style={{ width: '100%' }} size={8}>
            <Space size={8}>
              <Switch checked={filterEnabled} onChange={setFilterEnabled} />
              <Text type="secondary">
                {filterEnabled ? t('libraries.filterCustom') : t('libraries.filterUnlimited')}
              </Text>
            </Space>
            {filterEnabled && (
              <FilterBuilder value={filterValue} onChange={setFilterValue} compact />
            )}
          </Space>
        </Form.Item>
      </Form>

      <Divider style={{ margin: '8px 0 16px' }} />
      <Text strong>{t('libraries.preview')}</Text>
      <Space size={8} style={{ display: 'flex', marginTop: 8, flexWrap: 'wrap' }}>
        <Segmented
          value={previewKind}
          onChange={(v) => setPreviewKind(v as 'notification' | 'resource')}
          options={[
            { value: 'notification', label: t('libraries.previewNotification') },
            { value: 'resource', label: t('libraries.previewResource') },
          ]}
        />
        <Input
          style={{ width: 340 }}
          value={previewId}
          onChange={(e) => setPreviewId(e.target.value)}
          placeholder={t('libraries.previewIdPlaceholder')}
        />
        <Input
          style={{ width: 160 }}
          value={previewCategory}
          onChange={(e) => setPreviewCategory(e.target.value)}
          placeholder={t('organize.categoryOptional')}
        />
        <Button onClick={runPreview} loading={previewing}>
          {t('libraries.previewRun')}
        </Button>
      </Space>
      {previewResult && (
        <div style={{ marginTop: 12 }}>
          <Space size={8} style={{ marginBottom: 8, flexWrap: 'wrap' }}>
            <Text type="secondary">{t('libraries.matchedRule')}</Text>
            <Text>{previewResult.matched_rule?.name ?? t('format.dash')}</Text>
            <Text type="secondary">{t('organize.library')}</Text>
            <Text>{previewResult.library?.name ?? t('format.dash')}</Text>
            {previewResult.category && (
              <Tag>{`${t('organize.category')}: ${previewResult.category}`}</Tag>
            )}
            {previewResult.needs_category && (
              <Tag color="gold">{t('organize.needsCategory')}</Tag>
            )}
            {previewResult.uncategorized && (
              <Tag color="orange">{t('organize.uncategorizedTag')}</Tag>
            )}
          </Space>
          <Table
            size="small"
            columns={previewColumns}
            dataSource={previewResult.ops}
            rowKey={(op) => `${op.op_type}:${op.src}`}
            pagination={false}
            locale={{ emptyText: t('libraries.previewEmpty') }}
          />
        </div>
      )}
    </Modal>
  );
}
