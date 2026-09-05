import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Card, Tag, Typography } from 'antd';
import type { CollectionSummary, CollectionSibling } from '../types';
import { seasonLabel } from '../utils/season';

const { Text } = Typography;

interface Props {
  collection: CollectionSummary;
  siblings: CollectionSibling[];
}

/** "同系列作品" section on series/movie detail pages: the franchise
 * (WorkCollection) this work belongs to plus its sibling works, linked.
 * Per-season works: series siblings carry a 「第N季/特典」 season tag. */
export default function CollectionSiblingsCard({ collection, siblings }: Props) {
  const { t } = useTranslation();
  return (
    <Card title={`${t('works.collectionSiblings')} · ${collection.name ?? ''}`} style={{ margin: '16px 0' }} size="small">
      {siblings.length === 0 ? (
        <Text type="secondary">—</Text>
      ) : (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
          {siblings.map((s) => (
            <Link key={`${s.type}-${s.id}`} to={s.type === 'series' ? `/series/${s.id}` : `/movies/${s.id}`}>
              <Tag color={s.type === 'series' ? 'blue' : 'green'} style={{ cursor: 'pointer' }}>
                {s.title}
                {s.type === 'series' && s.season_number != null
                  ? ` · ${seasonLabel(t, s.season_number)}`
                  : ''}
                {s.year ? ` (${s.year})` : ''}
              </Tag>
            </Link>
          ))}
        </div>
      )}
    </Card>
  );
}
