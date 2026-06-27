import type { ThemeConfig } from 'antd';
import { seedTokens } from './tokens';
import { componentTokens } from './components';

export const raycastTheme: ThemeConfig = {
  token: seedTokens,
  components: componentTokens,
  cssVar: {
    prefix: 'rss',
    key: 'cohere',
  },
};
