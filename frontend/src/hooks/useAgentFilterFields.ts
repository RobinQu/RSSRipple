import { useEffect, useState } from 'react';
import { channelsApi } from '../api/channels';
import { allowedAgentFilterFields } from '../components/filterUtils';
import type { FilterField } from '../types';

/**
 * Allowed filter-DSL fields for agents on a channel, per the channel's
 * declared required metadata fields. Returns null while loading or when the
 * channel has no declaration (unrestricted); an array gates the filter field
 * dropdowns. Pick-preference DSL is intentionally exempt (callers simply
 * don't pass this to PreferenceListEditor).
 */
export default function useAgentFilterFields(
  channelId?: string | null,
): FilterField[] | null {
  const [allowed, setAllowed] = useState<FilterField[] | null>(null);

  useEffect(() => {
    if (!channelId) {
      setAllowed(null);
      return;
    }
    let alive = true;
    channelsApi.get(channelId).then((r) => {
      if (alive && r.success && r.data) {
        setAllowed(allowedAgentFilterFields(r.data.required_metadata_fields));
      }
    });
    return () => {
      alive = false;
    };
  }, [channelId]);

  return allowed;
}
