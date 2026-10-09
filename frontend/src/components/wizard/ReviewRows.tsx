import { Space, Typography } from 'antd';
import { useTranslation } from 'react-i18next';
import { mediaLabelKey, type ChangesShape, type MediaFieldKey } from './types';

const { Text } = Typography;

/** Step ④ confirmation review: structured change summary before the save. */
export default function ReviewRows({ changes }: { changes: ChangesShape }) {
  const { t } = useTranslation();
  const hasAny =
    changes.scopeFrom !== changes.scopeTo ||
    changes.worksAdded.length > 0 ||
    changes.worksRemoved.length > 0 ||
    changes.mappingChanged.length > 0 ||
    !!changes.collectionChanged ||
    changes.singleEpChanges.length > 0 ||
    changes.mediaChanges.length > 0;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      {changes.scopeFrom !== changes.scopeTo && (
        <ReviewSection title={t('resource.confirmScope')}>
          <Text style={{ fontSize: 12 }}>
            {changes.scopeFrom} → {changes.scopeTo}
          </Text>
        </ReviewSection>
      )}

      {(changes.worksAdded.length > 0 || changes.worksRemoved.length > 0) && (
        <ReviewSection title={t('resource.confirmWorks')}>
          <Space direction="vertical" size={2}>
            {changes.worksAdded.map((w) => (
              <Text key={`a-${w}`} style={{ fontSize: 12 }} type="success">
                + {w}
              </Text>
            ))}
            {changes.worksRemoved.map((w) => (
              <Text key={`r-${w}`} style={{ fontSize: 12 }} type="danger">
                − {w}
              </Text>
            ))}
          </Space>
        </ReviewSection>
      )}

      {changes.collectionChanged && (
        <ReviewSection title={t('resource.collectionLabel')}>
          <Text style={{ fontSize: 12 }}>
            {changes.collectionChanged.from} → {changes.collectionChanged.to}
          </Text>
        </ReviewSection>
      )}

      {changes.mappingChanged.length > 0 && (
        <ReviewSection title={`${t('resource.confirmMappings')}（${changes.mappingChanged.length}）`}>
          <div>
            {changes.mappingChanged.map((m) => (
              <div key={m.path} style={{ fontSize: 12, padding: '2px 0', overflowWrap: 'anywhere' }}>
                <Text type="secondary">{m.path}</Text>
                <br />
                <Text strong>{m.label}</Text>
              </div>
            ))}
          </div>
        </ReviewSection>
      )}

      {changes.singleEpChanges.length > 0 && (
        <ReviewSection title={t('resource.confirmSingleEp')}>
          {changes.singleEpChanges.map((c) => (
            <div key={c.key} style={{ fontSize: 12 }}>
              {c.key}: {c.from} → {c.to}
            </div>
          ))}
        </ReviewSection>
      )}

      {changes.mediaChanges.length > 0 && (
        <ReviewSection title={t('resource.confirmMedia')}>
          {changes.mediaChanges.map((c) => (
            <div key={c.key} style={{ fontSize: 12 }}>
              {t(`resource.${mediaLabelKey(c.key as MediaFieldKey)}`)}: {c.from} → {c.to}
            </div>
          ))}
        </ReviewSection>
      )}

      {!hasAny && (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {t('resource.noChanges')}
        </Text>
      )}
    </div>
  );
}

function ReviewSection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ border: '1px solid var(--rr-border-soft)', borderRadius: 8, padding: '8px 12px' }}>
      <Text strong style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>{title}</Text>
      {children}
    </div>
  );
}
