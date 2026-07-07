import type { ThemeConfig } from 'antd';
import { theme } from 'antd';
import { createSeedTokens, seedTokens } from './tokens';
import { componentTokens, createComponentTokens } from './components';

export type ColorMode = 'light' | 'dark';

export const raycastTheme: ThemeConfig = {
  token: seedTokens,
  components: componentTokens,
  cssVar: {
    prefix: 'rss',
    key: 'cohere',
  },
};

export function createAppTheme(mode: ColorMode): ThemeConfig {
  return {
    token: createSeedTokens(mode),
    components: createComponentTokens(mode),
    algorithm: mode === 'dark' ? theme.darkAlgorithm : theme.defaultAlgorithm,
    cssVar: {
      prefix: 'rss',
      key: `cohere-${mode}`,
    },
  };
}
