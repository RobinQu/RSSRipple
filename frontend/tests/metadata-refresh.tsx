// Browser-only harness: real component and i18n; HTTP responses supplied by the test.
import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { App, ConfigProvider } from 'antd';
import enUS from 'antd/locale/en_US';
import zhCN from 'antd/locale/zh_CN';
import { useTranslation } from 'react-i18next';
import i18n from '../src/i18n';
import WorkMetadataRefreshModal from '../src/components/WorkMetadataRefreshModal';

export default function Harness() {
  const { i18n: current } = useTranslation();
  const [open, setOpen] = useState(true);
  const [applied, setApplied] = useState(false);
  return <ConfigProvider locale={current.language === 'en-US' ? enUS : zhCN}><App>
    <button onClick={() => void i18n.changeLanguage(current.language === 'en-US' ? 'zh-CN' : 'en-US')}>Switch language</button>
    <output data-testid="applied">{String(applied)}</output>
    <WorkMetadataRefreshModal open={open} workId="synthetic-work" contentType="movie"
      initialQuery="Synthetic query" onClose={() => setOpen(false)} onApplied={() => setApplied(true)} />
  </App></ConfigProvider>;
}
createRoot(document.getElementById('root')!).render(<Harness />);
