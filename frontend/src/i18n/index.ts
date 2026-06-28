import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';
import enUS from './locales/en-US.json';
import zhCN from './locales/zh-CN.json';

const savedLang = localStorage.getItem('rssripple-lang') || 'zh-CN';

i18n.use(initReactI18next).init({
  resources: {
    'en-US': { translation: enUS },
    'zh-CN': { translation: zhCN },
  },
  lng: savedLang,
  fallbackLng: 'zh-CN',
  interpolation: {
    escapeValue: false, // React already escapes
    prefix: '{',
    suffix: '}',
  },
});

export default i18n;
