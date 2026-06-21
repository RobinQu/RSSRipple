import { theme as antdTheme } from 'antd';
import type { ThemeConfig } from 'antd';
import { seedTokens } from './tokens';
import { componentTokens } from './components';

export const raycastTheme: ThemeConfig = {
  algorithm: antdTheme.darkAlgorithm,
  token: seedTokens,
  components: componentTokens,
  cssVar: {
    prefix: 'rss',
    key: 'raycast',
  },
};
