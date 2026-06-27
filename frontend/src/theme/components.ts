import type { ThemeConfig } from 'antd';
import { raycastColors, raycastRadius } from './tokens';

export const componentTokens: ThemeConfig['components'] = {
  // Layout
  Layout: {
    bodyBg: raycastColors.canvas,
    siderBg: raycastColors.surface,
    headerBg: raycastColors.canvas,
  },

  // Menu (sidebar navigation)
  Menu: {
    itemBg: raycastColors.surface,
    itemColor: raycastColors.body,
    itemHoverColor: raycastColors.ink,
    itemHoverBg: raycastColors['surface-elevated'],
    itemSelectedBg: raycastColors.primary,
    itemSelectedColor: raycastColors['on-primary'],
    itemBorderRadius: raycastRadius.sm,
    itemMarginInline: 8,
    itemPaddingInline: 12,
  },

  // Table
  Table: {
    headerBg: raycastColors['surface-elevated'],
    headerColor: raycastColors.mute,
    headerSplitColor: raycastColors.hairline,
    borderColor: raycastColors.hairline,
    rowHoverBg: raycastColors['surface-card'],
    colorBgContainer: raycastColors.surface,
    headerBorderRadius: raycastRadius.md,
  },

  // Card
  Card: {
    colorBgContainer: raycastColors.surface,
    borderRadiusLG: raycastRadius.lg,
    paddingLG: 24,
    actionsBg: raycastColors['surface-elevated'],
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
    colorBgContainer: raycastColors.surface,
    colorBorder: raycastColors.hairline,
    activeBorderColor: '#9b60aa',
    hoverBorderColor: raycastColors['hairline-strong'],
    borderRadius: raycastRadius.md,
    controlHeight: 36,
  },

  // Select
  Select: {
    colorBgContainer: raycastColors.surface,
    colorBorder: raycastColors.hairline,
    borderRadius: raycastRadius.md,
    controlHeight: 36,
  },

  // Form
  Form: {
    labelColor: raycastColors.body,
  },

  // Modal
  Modal: {
    colorBgElevated: raycastColors['surface-elevated'],
    borderRadiusLG: raycastRadius.lg,
    titleColor: raycastColors.ink,
  },

  // Tabs
  Tabs: {
    inkBarColor: raycastColors.primary,
    itemActiveColor: raycastColors.ink,
    itemSelectedColor: raycastColors.ink,
    itemColor: raycastColors.mute,
    itemHoverColor: raycastColors.body,
  },

  // Tag
  Tag: {
    borderRadiusSM: raycastRadius.xs,
  },

  // Badge
  Badge: {
    colorError: raycastColors['accent-red'],
  },

  // Progress
  Progress: {
    defaultColor: raycastColors['accent-blue'],
  },

  // Tooltip
  Tooltip: {
    colorBgSpotlight: raycastColors.primary,
    colorTextLightSolid: raycastColors['on-primary'],
  },

  // Pagination
  Pagination: {
    colorBgContainer: raycastColors['surface-elevated'],
    colorBorder: raycastColors.hairline,
    borderRadius: raycastRadius.md,
    itemActiveBg: raycastColors['surface-card'],
  },

  // Descriptions
  Descriptions: {
    labelBg: raycastColors['surface-elevated'],
    colorBgContainer: raycastColors.surface,
  },
};
