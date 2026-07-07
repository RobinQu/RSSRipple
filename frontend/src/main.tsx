import { StrictMode, useEffect, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { ConfigProvider, App as AntdApp } from 'antd';
import { useTranslation } from 'react-i18next';
import enUS from 'antd/locale/en_US';
import zhCN from 'antd/locale/zh_CN';
import { createAppTheme, type ColorMode } from './theme';
import './i18n';
import './index.css';
import App from './App';

const antdLocales = {
  'en-US': enUS,
  'zh-CN': zhCN,
};

function getSystemColorMode(): ColorMode {
  if (typeof window === 'undefined') return 'light';
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function LocaleProvider({ children }: { children: React.ReactNode }) {
  const { i18n } = useTranslation();
  const [colorMode, setColorMode] = useState<ColorMode>(getSystemColorMode);
  const locale = antdLocales[i18n.language as keyof typeof antdLocales] || zhCN;
  const theme = useMemo(() => createAppTheme(colorMode), [colorMode]);

  useEffect(() => {
    document.documentElement.dataset.theme = colorMode;
    document.documentElement.style.colorScheme = colorMode;
  }, [colorMode]);

  useEffect(() => {
    const media = window.matchMedia('(prefers-color-scheme: dark)');
    const onChange = () => setColorMode(media.matches ? 'dark' : 'light');
    onChange();
    media.addEventListener('change', onChange);
    return () => media.removeEventListener('change', onChange);
  }, []);

  return (
    <ConfigProvider locale={locale} theme={theme}>
      <AntdApp>{children}</AntdApp>
    </ConfigProvider>
  );
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <BrowserRouter>
      <LocaleProvider>
        <App />
      </LocaleProvider>
    </BrowserRouter>
  </StrictMode>,
);
