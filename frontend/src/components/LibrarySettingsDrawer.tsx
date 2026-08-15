import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ArrowDown, ArrowUp, Pencil, Plus, Trash2 } from 'lucide-react';
import { App, Button, Drawer, Empty, Form, Input, Space, Switch, Table, Tabs, Tag, Typography } from 'antd';
import type { TableColumnsType } from 'antd';
import { organizeApi } from '../api/organize';
import OrganizeRuleFormModal from './OrganizeRuleFormModal';
import { describeFilter } from './filterUtils';
import { withMobileLabels } from '../utils/table';
import type { Library, OrganizeRule } from '../types';

const { Text } = Typography;

/** Subtitle-language-map editor (the only manually-editable library field
    besides the volume binding; rendered as the "other settings" tab). */
function LibraryOtherSettingsForm({
  library,
  onSaved,
}: {
  library: Library;
  onSaved: () => void;
}) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [form] = Form.useForm();
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    form.setFieldsValue({
      subtitle_lang_map: library?.subtitle_lang_map
        ? JSON.stringify(library.subtitle_lang_map, null, 2)
        : '',
    });
  }, [library, form]);

  const submit = async () => {
    const values = await form.validateFields();
    let langMap: Record<string, string> | null = null;
    const rawMap = (values.subtitle_lang_map ?? '').trim();
    if (rawMap) {
      try {
        const parsed: unknown = JSON.parse(rawMap);
        if (
          typeof parsed !== 'object' ||
          parsed === null ||
          Array.isArray(parsed) ||
          Object.entries(parsed).some(
            ([k, v]) => typeof k !== 'string' || typeof v !== 'string',
          )
        ) {
          throw new Error('not a string map');
        }
        langMap = parsed as Record<string, string>;
      } catch {
        message.error(t('libraries.subtitleLangMapInvalid'));
        return;
      }
    }
    setSaving(true);
    const res = await organizeApi.updateLibrary(library.id, {
      subtitle_lang_map: langMap,
    });
    setSaving(false);
    if (res.success) {
      message.success(t('mediaServers.librarySaved'));
      onSaved();
    } else {
      message.error(res.error?.message || t('mediaServers.librarySaveFailed'));
    }
  };

  return (
    <Form form={form} layout="vertical" style={{ marginTop: 12 }}>
      <Form.Item
        name="subtitle_lang_map"
        label={t('libraries.subtitleLangMap')}
        extra={t('libraries.subtitleLangMapExtra')}
      >
        <Input.TextArea
          rows={5}
          placeholder='{"zh-CN": "chs", "zh-TW": "cht"}'
          style={{ fontFamily: 'monospace' }}
        />
      </Form.Item>
      <Button type="primary" loading={saving} onClick={submit}>
        {t('common.save')}
      </Button>
    </Form>
  );
}

/** Per-library settings drawer: organize rules + other settings (subtitle map). */
export default function LibrarySettingsDrawer({
  open,
  library,
  libraries,
  rules,
  onClose,
  onChanged,
}: {
  open: boolean;
  library: Library | null;
  libraries: Library[];
  rules: OrganizeRule[];
  onClose: () => void;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const { message, modal } = App.useApp();
  const [tab, setTab] = useState<'rules' | 'settings'>('rules');
  const [ruleModalOpen, setRuleModalOpen] = useState(false);
  const [editingRule, setEditingRule] = useState<OrganizeRule | null>(null);

  const libraryRules = library
    ? rules.filter((r) => r.library_id === library.id)
    : [];

  const handleToggleRule = async (record: OrganizeRule, enabled: boolean) => {
    const r = await organizeApi.updateRule(record.id, { enabled });
    if (r.success) onChanged();
    else message.error(r.error?.message || t('libraries.ruleSaveFailed'));
  };

  // First-match-wins ordering: swap priority with the neighbour (nudging ±1
  // when equal so the order actually changes).
  const handleMoveRule = async (record: OrganizeRule, direction: -1 | 1) => {
    const idx = libraryRules.findIndex((x) => x.id === record.id);
    const neighbor = libraryRules[idx + direction];
    if (!neighbor) return;
    const target =
      neighbor.priority === record.priority
        ? neighbor.priority + direction
        : neighbor.priority;
    const r = await organizeApi.updateRule(record.id, { priority: target });
    if (r.success) onChanged();
    else message.error(r.error?.message || t('libraries.ruleSaveFailed'));
  };

  const handleDeleteRule = (record: OrganizeRule) => {
    modal.confirm({
      title: t('libraries.deleteRuleConfirm'),
      okText: t('common.delete'),
      okButtonProps: { danger: true },
      cancelText: t('common.cancel'),
      onOk: async () => {
        const r = await organizeApi.deleteRule(record.id);
        if (r.success) {
          message.success(t('libraries.ruleDeleted'));
          onChanged();
        } else {
          message.error(r.error?.message || t('libraries.ruleDeleteFailed'));
        }
      },
    });
  };

  const columns: TableColumnsType<OrganizeRule> = [
    {
      title: t('libraries.priority'),
      dataIndex: 'priority',
      key: 'priority',
      width: 100,
      render: (v: number, record) => (
        <Space size={0}>
          <Button
            type="text"
            size="small"
            icon={<ArrowUp size={13} />}
            title={t('libraries.moveUp')}
            disabled={libraryRules.findIndex((x) => x.id === record.id) === 0}
            onClick={() => handleMoveRule(record, -1)}
          />
          <Button
            type="text"
            size="small"
            icon={<ArrowDown size={13} />}
            title={t('libraries.moveDown')}
            disabled={libraryRules.findIndex((x) => x.id === record.id) === libraryRules.length - 1}
            onClick={() => handleMoveRule(record, 1)}
          />
          <Text type="secondary" style={{ display: 'inline-block', minWidth: 20, textAlign: 'center' }}>
            {v}
          </Text>
        </Space>
      ),
    },
    {
      title: t('common.name'),
      dataIndex: 'name',
      key: 'name',
      render: (v: string) => <Text ellipsis={{ tooltip: v }} style={{ maxWidth: 160 }}>{v}</Text>,
    },
    {
      // Merged detail column: filter summary + naming template + file_op /
      // auto_execute tags stacked vertically — keeps the drawer table from
      // overflowing into horizontal scroll.
      title: t('libraries.ruleConfig'),
      key: 'config',
      render: (_, record) => {
        const summary = describeFilter(record.filter, t);
        const opColor =
          record.file_op === 'move' ? 'green' : record.file_op === 'hardlink' ? 'gold' : 'geekblue';
        return (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4, minWidth: 0 }}>
            <Text
              type={summary ? undefined : 'secondary'}
              ellipsis={{ tooltip: summary || undefined }}
              style={{ fontSize: 12 }}
            >
              {summary || t('libraries.filterUnlimited')}
            </Text>
            <Text code ellipsis={{ tooltip: record.path_template }} style={{ fontSize: 12 }}>
              {record.path_template}
            </Text>
            <Space size={4} wrap>
              <Tag color={opColor} style={{ margin: 0 }}>
                {t(`libraries.fileOp_${record.file_op}`)}
              </Tag>
              <Tag color={record.auto_execute ? 'green' : undefined} style={{ margin: 0 }}>
                {record.auto_execute ? t('common.on') : t('common.off')}
              </Tag>
            </Space>
          </div>
        );
      },
    },
    {
      title: t('libraries.enabled'),
      dataIndex: 'enabled',
      key: 'enabled',
      width: 70,
      render: (v: boolean, record) => (
        <Switch size="small" checked={v} onChange={(checked) => handleToggleRule(record, checked)} />
      ),
    },
    {
      title: t('common.actions'),
      key: 'actions',
      width: 100,
      align: 'right',
      render: (_, record) => (
        <Space size={4}>
          <Button
            type="text"
            size="small"
            icon={<Pencil size={14} />}
            title={t('common.edit')}
            onClick={() => {
              setEditingRule(record);
              setRuleModalOpen(true);
            }}
          />
          <Button
            type="text"
            size="small"
            danger
            icon={<Trash2 size={14} />}
            title={t('common.delete')}
            onClick={() => handleDeleteRule(record)}
          />
        </Space>
      ),
    },
  ];

  const rulesTab = (
    <>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
        <Button
          type="primary"
          icon={<Plus size={14} />}
          onClick={() => {
            setEditingRule(null);
            setRuleModalOpen(true);
          }}
        >
          {t('libraries.newRule')}
        </Button>
      </div>
      <Table
        className="stack-table"
        columns={withMobileLabels(columns)}
        dataSource={libraryRules}
        rowKey="id"
        locale={{ emptyText: <Empty description={t('libraries.noRules')} /> }}
        pagination={false}
      />
    </>
  );

  return (
    <>
      <Drawer
        open={open}
        title={t('mediaLibrary.librarySettings', { name: library?.name ?? '' })}
        onClose={onClose}
        width={920}
      >
        <Tabs
          activeKey={tab}
          onChange={(k) => setTab(k as 'rules' | 'settings')}
          items={[
            { key: 'rules', label: t('mediaLibrary.tabRules'), children: rulesTab },
            {
              key: 'settings',
              label: t('mediaLibrary.tabOtherSettings'),
              children: library ? (
                <LibraryOtherSettingsForm library={library} onSaved={onChanged} />
              ) : null,
            },
          ]}
        />
      </Drawer>

      <OrganizeRuleFormModal
        open={ruleModalOpen}
        rule={editingRule}
        libraries={libraries}
        fixedLibraryId={library?.id}
        onClose={() => setRuleModalOpen(false)}
        onSaved={onChanged}
      />
    </>
  );
}
