import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';

/**
 * Sets the browser tab title to `<title> - RSSRipple` (or plain `RSSRipple`
 * when no page title is given). Pass an already-translated string; the effect
 * re-runs on language change so the title follows the active locale.
 */
export default function useDocumentTitle(title?: string | null) {
  const { i18n } = useTranslation();

  useEffect(() => {
    document.title = title ? `${title} - RSSRipple` : 'RSSRipple';
  }, [title, i18n.language]);
}
