import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Card, Tag, Typography } from 'antd';
import type { CollectionSummary, CollectionSibling } from '../types';

const { Text } = Typography;

interface Props {
  collection: CollectionSummary;
  siblings: CollectionSibling[];
}

/** "同系列作品" section on series/movie detail pages: the franchise
 * (WorkCollection) this work belongs to plus its sibling works, linked. */
export default function CollectionSiblingsCard({ collection, siblings }: Props) {
  const { t } = useTranslation();
  return (
    <Card title={`${t('works.collectionSiblings')} · ${collection.name ?? ''}`} style={{ marginTop: 16 }} size="small">
      {siblings.length === 0 ? (
        <Text type="secondary">—</Text>
      ) : (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
          {siblings.map((s) => (
            <Link key={`${s.type}-${s.id}`} to={s.type === 'series' ? `/series/${s.id}` : `/movies/${s.id}`}>
              <Tag color={s.type === 'series' ? 'blue' : 'green'} style={{ cursor: 'pointer' }}>
                {s.title}
                {s.year ? ` (${s.year})` : ''}
              </Tag>
            </Link>
          ))}
        </div>
      )}
    </Card>
  );
}
