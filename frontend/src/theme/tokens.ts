import type { ThemeConfig } from 'antd';

// Cohere-inspired design tokens from DESIGN.md.
export const lightColors = {
  canvas: '#ffffff',
  surface: '#ffffff',
  'surface-elevated': '#f7f7f5',
  'surface-card': '#eeece7',
  'button-fg': '#ffffff',

  hairline: '#d9d9dd',
  'hairline-soft': '#e5e7eb',
  'hairline-strong': '#93939f',

  ink: '#212121',
  body: '#616161',
  charcoal: '#17171c',
  mute: '#93939f',
  ash: '#75758a',
  stone: '#d9d9dd',
  'on-dark': '#ffffff',
  'on-dark-mute': 'rgba(255,255,255,0.72)',

  primary: '#17171c',
  'primary-pressed': '#000000',
  'on-primary': '#ffffff',

  'accent-blue': '#1863dc',
  'accent-blue-soft': '#f1f5ff',
  'accent-red': '#b30000',
  'accent-red-soft': '#fff1f0',
  'accent-green': '#003c33',
  'accent-green-soft': '#edfce9',
  'accent-yellow': '#ff7759',
  'accent-yellow-soft': '#ffad9b',

  'hero-stripe-start': '#003c33',
  'hero-stripe-end': '#071829',

  'key-bg-start': '#eeece7',
  'key-bg-end': '#ffffff',
};

export const darkColors: typeof lightColors = {
  canvas: '#111114',
  surface: '#17171c',
  'surface-elevated': '#1f1f24',
  'surface-card': '#282830',
  'button-fg': '#17171c',

  hairline: '#303038',
  'hairline-soft': '#26262d',
  'hairline-strong': '#5f6070',

  ink: '#f3f3f5',
  body: '#c4c4cc',
  charcoal: '#ffffff',
  mute: '#93939f',
  ash: '#a6a6b2',
  stone: '#383844',
  'on-dark': '#ffffff',
  'on-dark-mute': 'rgba(255,255,255,0.72)',

  primary: '#ffffff',
  'primary-pressed': '#d9d9dd',
  'on-primary': '#17171c',

  'accent-blue': '#72a2ff',
  'accent-blue-soft': '#17243d',
  'accent-red': '#ff8b8b',
  'accent-red-soft': '#341919',
  'accent-green': '#89d9c8',
  'accent-green-soft': '#15352f',
  'accent-yellow': '#ffb29c',
  'accent-yellow-soft': '#3d241f',

  'hero-stripe-start': '#0c3c35',
  'hero-stripe-end': '#08111e',

  'key-bg-start': '#282830',
  'key-bg-end': '#17171c',
};

export const raycastColors = lightColors;

export const raycastRadius = {
  xs: 4,
  sm: 8,
  md: 8,
  lg: 16,
  xl: 22,
};

export const raycastSpacing = {
  xxs: 2,
  xs: 4,
  sm: 8,
  md: 12,
  lg: 16,
  xl: 24,
  xxl: 32,
  section: 96,
};

// Map design tokens to antd seed tokens.
export const createSeedTokens = (
  mode: 'light' | 'dark' = 'light',
): ThemeConfig['token'] => {
  const colors = mode === 'dark' ? darkColors : lightColors;
  return {
  // Colors
  colorPrimary: colors.primary,
  colorTextLightSolid: colors['button-fg'],
  colorSuccess: colors['accent-green'],
  colorWarning: colors['accent-yellow'],
  colorError: colors['accent-red'],
  colorInfo: colors['accent-blue'],

  colorBgBase: colors.canvas,
  colorBgContainer: colors.surface,
  colorBgElevated: colors['surface-elevated'],
  colorBgLayout: colors.canvas,
  colorBgSpotlight: colors['surface-card'],

  // Text colors
  colorText: colors.ink,
  colorTextSecondary: colors.body,
  colorTextTertiary: colors.mute,
  colorTextQuaternary: colors.ash,

  // Border
  colorBorder: colors.hairline,
  colorBorderSecondary: colors['hairline-soft'],

  fontFamily: "'Inter', 'Unica77 Cohere Web', system-ui, -apple-system, sans-serif",
  fontSize: 14,

  // Shape
  borderRadius: raycastRadius.md,
  borderRadiusLG: raycastRadius.lg,
  borderRadiusSM: raycastRadius.sm,

  // Sizing
  controlHeight: 36,
  controlHeightLG: 44,

  // Motion
  motion: true,
  };
};

export const seedTokens: ThemeConfig['token'] = createSeedTokens('light');
