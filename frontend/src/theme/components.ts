import type { ThemeConfig } from 'antd';
import { darkColors, lightColors, raycastRadius } from './tokens';

export const createComponentTokens = (
  mode: 'light' | 'dark' = 'light',
): ThemeConfig['components'] => {
  const colors = mode === 'dark' ? darkColors : lightColors;
  return {
  // Layout
  Layout: {
    bodyBg: colors.canvas,
    siderBg: colors.surface,
    headerBg: colors.canvas,
  },

  // Menu (sidebar navigation)
  Menu: {
    itemBg: colors.surface,
    itemColor: colors.body,
    itemHoverColor: colors.ink,
    itemHoverBg: colors['surface-elevated'],
    itemSelectedBg: colors.primary,
    itemSelectedColor: colors['on-primary'],
    itemBorderRadius: raycastRadius.sm,
    itemMarginInline: 8,
    itemPaddingInline: 12,
  },

  // Table
  Table: {
    headerBg: colors['surface-elevated'],
    headerColor: colors.mute,
    headerSplitColor: colors.hairline,
    borderColor: colors.hairline,
    rowHoverBg: colors['surface-card'],
    // The global colorPrimary is near-black (#17171c), whose derived
    // rowSelectedBg is a muddy dark gray — dark titles on that background
    // become unreadable. Pin selected rows to a light blue tint instead.
    rowSelectedBg: colors['accent-blue-soft'],
    rowSelectedHoverBg: mode === 'dark' ? '#20345a' : '#e0eaff',
    colorBgContainer: colors.surface,
    headerBorderRadius: raycastRadius.md,
  },

  // Card
  Card: {
    colorBgContainer: colors.surface,
    borderRadiusLG: raycastRadius.lg,
    paddingLG: 24,
    actionsBg: colors['surface-elevated'],
  },

  // Button
  Button: {
    borderRadius: 32,
    controlHeight: 36,
    controlHeightLG: 44,
    primaryShadow: 'none',
    defaultShadow: 'none',
    dangerShadow: 'none',
  },

  // Input
  Input: {
    colorBgContainer: colors.surface,
    colorBorder: colors.hairline,
    activeBorderColor: colors['accent-blue'],
    hoverBorderColor: colors['hairline-strong'],
    borderRadius: raycastRadius.md,
    controlHeight: 36,
  },

  // Select
  Select: {
    colorBgContainer: colors.surface,
    colorBorder: colors.hairline,
    borderRadius: raycastRadius.md,
    controlHeight: 36,
  },

  // Form
  Form: {
    labelColor: colors.body,
  },

  // Modal
  Modal: {
    colorBgElevated: colors['surface-elevated'],
    borderRadiusLG: raycastRadius.lg,
    titleColor: colors.ink,
  },

  // Tabs
  Tabs: {
    inkBarColor: colors.primary,
    itemActiveColor: colors.ink,
    itemSelectedColor: colors.ink,
    itemColor: colors.mute,
    itemHoverColor: colors.body,
  },

  // Tag
  Tag: {
    borderRadiusSM: raycastRadius.xs,
  },

  // Badge
  Badge: {
    colorError: colors['accent-red'],
  },

  // Progress
  Progress: {
    defaultColor: colors['accent-blue'],
  },

  // Tooltip
  Tooltip: {
    colorBgSpotlight: mode === 'dark' ? colors['surface-card'] : colors.charcoal,
    colorTextLightSolid: mode === 'dark' ? colors.ink : colors['on-dark'],
  },

  // Pagination
  Pagination: {
    colorBgContainer: colors['surface-elevated'],
    colorBorder: colors.hairline,
    borderRadius: raycastRadius.md,
    itemActiveBg: colors['surface-card'],
  },

  // Descriptions
  Descriptions: {
    labelBg: colors['surface-elevated'],
    colorBgContainer: colors.surface,
  },
  };
};

export const componentTokens: ThemeConfig['components'] = createComponentTokens('light');
