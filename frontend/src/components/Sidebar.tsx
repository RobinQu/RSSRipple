import { useState } from 'react';
import { Layout, Menu, Typography, Button, Dropdown } from 'antd';
import type { MenuProps } from 'antd';
import { useNavigate, useLocation } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { LayoutDashboard, Rss, HardDrive, Bot, Languages } from 'lucide-react';

const { Sider } = Layout;
const { Text } = Typography;

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
      width={220}
      collapsedWidth={72}
      breakpoint="lg"
      style={{
        borderRight: '1px solid #d9d9dd',
      }}
    >
      <div
        style={{
          padding: collapsed ? '20px 0' : '20px 16px',
          textAlign: 'center',
          borderBottom: '1px solid #d9d9dd',
          overflow: 'hidden',
        }}
      >
        {collapsed ? (
          <Text strong style={{ fontSize: 18, color: '#17171c' }}>R</Text>
        ) : (
          <Text strong style={{ fontSize: 18, color: '#17171c' }}>RSSRipple</Text>
        )}
      </div>
      <Menu
        mode="inline"
        selectedKeys={[(selectedKey as { key: string })?.key || '/']}
        items={menuItems}
        onClick={handleMenuClick}
        style={{
          borderRight: 'none',
          padding: '8px 0',
        }}
      />
      {!collapsed && (
        <div
          style={{
            position: 'absolute',
            bottom: 48,
            left: 0,
            right: 0,
            textAlign: 'center',
            color: '#93939f',
            fontSize: 12,
          }}
        >
          v0.2.0
        </div>
      )}
      <div
        style={{
          position: 'absolute',
          bottom: collapsed ? 16 : 80,
          left: 0,
          right: 0,
          display: 'flex',
          justifyContent: 'center',
        }}
      >
        {collapsed ? (
          <Button
            type="text"
            icon={<Languages size={16} />}
            onClick={() => switchLanguage(i18n.language === 'zh-CN' ? 'en-US' : 'zh-CN')}
            title={t('language.switch')}
          />
        ) : (
          <Dropdown menu={{ items: langItems }} trigger={['click']}>
            <Button type="text" icon={<Languages size={16} />}>
              {t('language.switch')}
            </Button>
          </Dropdown>
        )}
      </div>
    </Sider>
  );
}
