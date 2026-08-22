import { useEffect, useState } from 'react';
import { Button, Checkbox, Modal, Tag, Typography } from 'antd';
import { useTranslation } from 'react-i18next';
import { channelsApi, type RequiredFieldCatalogEntry } from '../api/channels';

const { Text } = Typography;

interface Props {
  value?: string[] | null;
  onChange?: (v: string[]) => void;
  /** Previously-saved keys (edit mode): add-only policy forbids unchecking. */
  saved?: string[] | null;
}

/** Channel "required metadata fields" picker: a summary line + dialog with
 * catalog fields grouped two levels deep — sections by work type first
 * (基础必选 / 剧集作品 / 多作品合集, then cross-cutting 发布信息/作品信息),
 * semantic categories inside. The list is mandatory and add-only:
 * code-enforced fields (``lock`` scope) and previously-saved keys can never
 * be unchecked — new fields may only be added on top. */
export default function RequiredFieldsInput({ value, onChange, saved }: Props) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [catalog, setCatalog] = useState<RequiredFieldCatalogEntry[]>([]);
  const [sections, setSections] = useState<string[]>([]);
  const [draft, setDraft] = useState<string[]>([]);

  useEffect(() => {
    channelsApi.requiredFieldCatalog().then((r) => {
      if (r.success && r.data) {
        setCatalog(r.data.fields || []);
        setSections(r.data.sections || []);
      }
    });
  }, []);

  const selected = value ?? [];
  const savedKeys = new Set(saved ?? []);
  const label = (key: string) => t(`channels.requiredField_${key}`, { defaultValue: key });
  const lockLabel = (scope: string | null) =>
    scope ? t(`channels.requiredFieldLock_${scope}`, { defaultValue: scope }) : null;

  const openModal = () => {
    // Draft always starts from the current selection; locked/saved keys are
    // non-removable so the draft can never shrink below them.
    const merged = [...selected];
    for (const f of catalog) {
      if ((f.locked || savedKeys.has(f.key)) && !merged.includes(f.key)) merged.push(f.key);
    }
    setDraft(merged);
    setOpen(true);
  };

  const handleOk = () => {
    onChange?.(draft);
    setOpen(false);
  };

  const toggle = (key: string, checked: boolean) => {
    setDraft((cur) => (checked ? [...cur, key] : cur.filter((k) => k !== key)));
  };

  return (
    <>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        {selected.map((key) => (
          <Tag key={key}>{label(key)}</Tag>
        ))}
        <Button size="small" onClick={openModal}>
          {t('channels.requiredFieldsConfigure')}
        </Button>
      </div>
      <Modal
        open={open}
        title={t('channels.requiredFieldsLabel')}
        okText={t('common.confirm')}
        cancelText={t('common.cancel')}
        onOk={handleOk}
        onCancel={() => setOpen(false)}
      >
        <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 12 }}>
          {t('channels.requiredFieldsDesc')}
        </Text>
        <div style={{ marginBottom: 12 }}>
          <Text type="secondary" style={{ fontSize: 12, display: 'inline' }}>
            · {t('channels.requiredFieldsLockedHint')}
          </Text>
          {savedKeys.size > 0 && (
            <Text type="secondary" style={{ fontSize: 12, display: 'inline', marginLeft: 12 }}>
              · {t('channels.requiredFieldsSavedHint')}
            </Text>
          )}
        </div>
        {sections.map((sectionKey) => {
          const sectionFields = catalog.filter((f) => f.section === sectionKey);
          if (sectionFields.length === 0) return null;
          // Semantic sub-groups inside each work-type section, in catalog
          // order; single-group sections skip the redundant sub-heading.
          const groupKeys: string[] = [];
          for (const f of sectionFields) {
            if (!groupKeys.includes(f.group)) groupKeys.push(f.group);
          }
          return (
            <div
              key={sectionKey}
              style={{
                marginBottom: 12,
                padding: '8px 10px',
                border: '1px solid var(--rr-border-soft)',
                borderRadius: 6,
              }}
            >
              <Text strong style={{ fontSize: 13, display: 'block', marginBottom: 4 }}>
                {t(`channels.requiredFieldSection_${sectionKey}`, { defaultValue: sectionKey })}
              </Text>
              {groupKeys.map((g) => {
                const fields = sectionFields.filter((f) => f.group === g);
                return (
                  <div key={g} style={{ marginBottom: groupKeys.length > 1 ? 8 : 0 }}>
                    {groupKeys.length > 1 && (
                      <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 2 }}>
                        {t(`channels.requiredFieldGroup_${g}`, { defaultValue: g })}
                      </Text>
                    )}
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                      {fields.map((f) => {
                        const pinned = f.locked || savedKeys.has(f.key);
                        const scope = lockLabel(f.lock);
                        return (
                          <Checkbox
                            key={f.key}
                            checked={draft.includes(f.key)}
                            disabled={pinned}
                            onChange={(e) => toggle(f.key, e.target.checked)}
                          >
                            {label(f.key)}
                            {(f.locked || savedKeys.has(f.key)) && (
                              <Text type="secondary" style={{ fontSize: 12, marginLeft: 6 }}>
                                ({f.locked && scope ? scope : t('channels.requiredFieldsSavedTag')})
                              </Text>
                            )}
                          </Checkbox>
                        );
                      })}
                    </div>
                  </div>
                );
              })}
            </div>
          );
        })}
      </Modal>
    </>
  );
}
