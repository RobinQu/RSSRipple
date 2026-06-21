import type { ThemeConfig } from 'antd';

// Raycast design tokens from DESIGN.md
export const raycastColors = {
  // Surface ladder
  canvas: '#07080a',
  surface: '#0d0d0d',
  'surface-elevated': '#101111',
  'surface-card': '#121212',
  'button-fg': '#18191a',

  // Borders
  hairline: '#242728',
  'hairline-soft': 'rgba(255,255,255,0.08)',
  'hairline-strong': 'rgba(255,255,255,0.16)',

  // Text
  ink: '#f4f4f6',
  body: '#cdcdcd',
  charcoal: '#d3d3d4',
  mute: '#9c9c9d',
  ash: '#6a6b6c',
  stone: '#434345',
  'on-dark': '#ffffff',
  'on-dark-mute': 'rgba(255,255,255,0.72)',

  // Brand / Primary
  primary: '#ffffff',
  'primary-pressed': '#e8e8e8',
  'on-primary': '#000000',

  // Accent (semantic)
  'accent-blue': '#57c1ff',
  'accent-blue-soft': 'rgba(87,193,255,0.15)',
  'accent-red': '#ff6161',
  'accent-red-soft': 'rgba(255,97,97,0.15)',
  'accent-green': '#59d499',
  'accent-green-soft': 'rgba(89,212,153,0.15)',
  'accent-yellow': '#ffc533',
  'accent-yellow-soft': 'rgba(255,197,51,0.15)',

  // Hero gradient
  'hero-stripe-start': '#ff5757',
  'hero-stripe-end': '#a1131a',

  // Keycap gradient
  'key-bg-start': '#121212',
  'key-bg-end': '#0d0d0d',
};

export const raycastRadius = {
  xs: 4,
  sm: 6,
  md: 8,
  lg: 10,
  xl: 16,
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

// Map Raycast tokens to antd seed tokens
export const seedTokens: ThemeConfig['token'] = {
  // Colors - using dark algorithm as base, overriding with Raycast palette
  colorPrimary: '#ffffff',
  colorTextLightSolid: raycastColors['button-fg'],
  colorSuccess: raycastColors['accent-green'],
  colorWarning: raycastColors['accent-yellow'],
  colorError: raycastColors['accent-red'],
  colorInfo: raycastColors['accent-blue'],

  // Background colors (surface ladder)
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

  // Typography
  fontFamily: "'Inter', 'Inter Fallback', system-ui, -apple-system, sans-serif",
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
