import { useState } from 'react';
import { Layout, Menu, Button, Dropdown } from 'antd';
import type { MenuProps } from 'antd';
import { useNavigate, useLocation } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import {
  Bot,
  ExternalLink,
  HardDrive,
  Languages,
  LayoutDashboard,
  Library,
  PanelLeftClose,
  PanelLeftOpen,
  Rss,
  Settings,
} from 'lucide-react';
import BrandLogo from './BrandLogo';

const { Sider } = Layout;
const githubUrl = 'https://github.com/RobinQu/RSSRipple';

const iconButtonStyle = {
  color: 'var(--rr-text-secondary)',
  height: 36,
  width: 36,
};

export default function Sidebar() {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();

  const [collapsed, setCollapsed] = useState(() => {
    return localStorage.getItem('sidebarCollapsed') === 'true';
  });

  const handleCollapse = (value: boolean) => {
    setCollapsed(value);
    localStorage.setItem('sidebarCollapsed', String(value));
  };

  const menuItems: MenuProps['items'] = [
    {
      key: '/',
      icon: <LayoutDashboard size={16} />,
      label: t('nav.dashboard'),
    },
    {
      key: '/works',
      icon: <Library size={16} />,
      label: t('nav.repository'),
    },
    {
      key: '/channels',
      icon: <Rss size={16} />,
      label: t('nav.channels'),
    },
    {
      key: '/agents',
      icon: <Bot size={16} />,
      label: t('nav.agents'),
    },
    {
      key: '/downloaders',
      icon: <HardDrive size={16} />,
      label: t('nav.downloaders'),
    },
    {
      key: '/settings',
      icon: <Settings size={16} />,
      label: t('nav.settings'),
    },
  ];

  const selectedKey = (menuItems as { key: string }[])?.find((item) => {
    const key = item.key;
    if (key === '/') return location.pathname === '/';
    return location.pathname.startsWith(key as string);
  });

  const handleMenuClick: MenuProps['onClick'] = ({ key }) => {
    navigate(key);
  };

  const switchLanguage = (lang: string) => {
    i18n.changeLanguage(lang);
    localStorage.setItem('rssripple-lang', lang);
  };

  const langItems: MenuProps['items'] = [
    {
      key: 'zh-CN',
      label: t('language.zh'),
      onClick: () => switchLanguage('zh-CN'),
    },
    {
      key: 'en-US',
      label: t('language.en'),
      onClick: () => switchLanguage('en-US'),
    },
  ];

  return (
    <Sider
      collapsible
      collapsed={collapsed}
      onCollapse={handleCollapse}
      trigger={null}
      width={220}
      collapsedWidth={72}
      breakpoint="lg"
      style={{
        borderRight: '1px solid var(--rr-border)',
        position: 'relative',
      }}
    >
      <div
        style={{
          alignItems: 'center',
          borderBottom: '1px solid var(--rr-border)',
          display: 'flex',
          height: 78,
          justifyContent: collapsed ? 'center' : 'flex-start',
          overflow: 'hidden',
          padding: collapsed ? '0' : '0 16px',
        }}
      >
        <button
          type="button"
          aria-label="RSSRipple"
          title={t('nav.dashboard')}
          onClick={() => navigate('/')}
          style={{
            alignItems: 'center',
            background: 'transparent',
            border: 0,
            cursor: 'pointer',
            display: 'flex',
            padding: 0,
            width: '100%',
          }}
        >
          <BrandLogo collapsed={collapsed} />
        </button>
      </div>
      <Menu
        mode="inline"
        selectedKeys={[(selectedKey as { key: string })?.key || '/']}
        items={menuItems}
        onClick={handleMenuClick}
        style={{
          borderRight: 'none',
          padding: '8px 0',
          paddingBottom: collapsed ? 168 : 136,
        }}
      />
      <div
        style={{
          position: 'absolute',
          bottom: 12,
          left: collapsed ? 0 : 16,
          right: collapsed ? 0 : 16,
          display: 'flex',
          flexDirection: 'column',
          gap: 8,
          alignItems: 'center',
          justifyContent: 'center',
        }}
      >
        {!collapsed && (
          <div
            style={{
              color: 'var(--rr-text-muted)',
              fontSize: 12,
              lineHeight: 1.4,
              textAlign: 'center',
            }}
          >
            <span>v0.2.0</span>
            <span style={{ color: 'var(--rr-border)', padding: '0 6px' }}>/</span>
            <a
              href={githubUrl}
              target="_blank"
              rel="noreferrer"
              style={{ color: 'var(--rr-text-secondary)', fontWeight: 650 }}
            >
              GitHub
            </a>
          </div>
        )}
        <div
          style={{
            alignItems: 'center',
            display: 'flex',
            flexDirection: collapsed ? 'column' : 'row',
            gap: collapsed ? 6 : 8,
            justifyContent: 'center',
          }}
        >
          {collapsed && (
            <Button
              className="sidebar-control-button"
              type="text"
              href={githubUrl}
              target="_blank"
              rel="noreferrer"
              icon={<ExternalLink size={16} />}
              title="GitHub"
              style={iconButtonStyle}
            />
          )}
          {collapsed ? (
            <Button
              className="sidebar-control-button"
              type="text"
              icon={<Languages size={16} />}
              onClick={() => switchLanguage(i18n.language === 'zh-CN' ? 'en-US' : 'zh-CN')}
              title={t('language.switch')}
              style={iconButtonStyle}
            />
          ) : (
            <Dropdown menu={{ items: langItems }} trigger={['click']}>
              <Button
                className="sidebar-control-button"
                type="text"
                icon={<Languages size={16} />}
                style={{ color: 'var(--rr-text-secondary)' }}
              >
                {t('language.switch')}
              </Button>
            </Dropdown>
          )}
          <Button
            className="sidebar-control-button"
            type="text"
            icon={collapsed ? <PanelLeftOpen size={16} /> : <PanelLeftClose size={16} />}
            onClick={() => handleCollapse(!collapsed)}
          title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          style={collapsed ? iconButtonStyle : { color: 'var(--rr-text-secondary)' }}
          />
        </div>
        {collapsed ? null : (
          <a
            href={githubUrl}
            target="_blank"
            rel="noreferrer"
            aria-label="RSSRipple GitHub"
            style={{
              alignItems: 'center',
              color: 'var(--rr-text-muted)',
              display: 'flex',
              fontSize: 11,
              gap: 4,
              lineHeight: 1,
            }}
          >
            <ExternalLink size={12} />
            RobinQu/RSSRipple
          </a>
        )}
      </div>
    </Sider>
  );
}
