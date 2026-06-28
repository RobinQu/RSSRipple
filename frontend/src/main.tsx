import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { ConfigProvider, App as AntdApp } from 'antd';
import { useTranslation } from 'react-i18next';
import enUS from 'antd/locale/en_US';
import zhCN from 'antd/locale/zh_CN';
import { raycastTheme } from './theme';
import './i18n';
import './index.css';
import App from './App';

const antdLocales = {
  'en-US': enUS,
  'zh-CN': zhCN,
};

function LocaleProvider({ children }: { children: React.ReactNode }) {
  const { i18n } = useTranslation();
  const locale = antdLocales[i18n.language as keyof typeof antdLocales] || zhCN;
  return (
    <ConfigProvider locale={locale} theme={raycastTheme}>
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
