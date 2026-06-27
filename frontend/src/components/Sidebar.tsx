import { useState } from 'react';
import { Layout, Menu, Typography } from 'antd';
import type { MenuProps } from 'antd';
import { useNavigate, useLocation } from 'react-router-dom';
import { LayoutDashboard, Rss, HardDrive, Bot } from 'lucide-react';

const { Sider } = Layout;
const { Text } = Typography;

const menuItems: MenuProps['items'] = [
  {
    key: '/',
    icon: <LayoutDashboard size={16} />,
    label: '仪表盘',
  },
  {
    key: '/channels',
    icon: <Rss size={16} />,
    label: '频道',
  },
  {
    key: '/agents',
    icon: <Bot size={16} />,
    label: 'Agents',
  },
  {
    key: '/downloaders',
    icon: <HardDrive size={16} />,
    label: '下载器',
  },
];

export default function Sidebar() {
  const navigate = useNavigate();
  const location = useLocation();

  const [collapsed, setCollapsed] = useState(() => {
    return localStorage.getItem('sidebarCollapsed') === 'true';
  });

  const handleCollapse = (value: boolean) => {
    setCollapsed(value);
    localStorage.setItem('sidebarCollapsed', String(value));
  };

  const selectedKey = (menuItems as { key: string }[])?.find((item) => {
    const key = item.key;
    if (key === '/') return location.pathname === '/';
    return location.pathname.startsWith(key as string);
  });

  const handleMenuClick: MenuProps['onClick'] = ({ key }) => {
    navigate(key);
  };

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
    </Sider>
  );
}
