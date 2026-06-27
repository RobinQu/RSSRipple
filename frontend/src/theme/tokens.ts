import type { ThemeConfig } from 'antd';

// Cohere-inspired design tokens from DESIGN.md.
export const raycastColors = {
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
export const seedTokens: ThemeConfig['token'] = {
  // Colors
  colorPrimary: raycastColors.primary,
  colorTextLightSolid: raycastColors['button-fg'],
  colorSuccess: raycastColors['accent-green'],
  colorWarning: raycastColors['accent-yellow'],
  colorError: raycastColors['accent-red'],
  colorInfo: raycastColors['accent-blue'],

  colorBgBase: raycastColors.canvas,
  colorBgContainer: raycastColors.surface,
  colorBgElevated: raycastColors['surface-elevated'],
  colorBgLayout: raycastColors.canvas,
  colorBgSpotlight: raycastColors['surface-card'],

  // Text colors
  colorText: raycastColors.ink,
  colorTextSecondary: raycastColors.body,
  colorTextTertiary: raycastColors.mute,
  colorTextQuaternary: raycastColors.ash,

  // Border
  colorBorder: raycastColors.hairline,
  colorBorderSecondary: raycastColors['hairline-soft'],

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
