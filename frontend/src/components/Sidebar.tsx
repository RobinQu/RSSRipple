import { Layout, Menu } from 'antd';
import type { MenuProps } from 'antd';
import { useNavigate, useLocation } from 'react-router-dom';
import { LayoutDashboard, Rss, HardDrive, Bot } from 'lucide-react';

const { Sider } = Layout;

const menuItems: MenuProps['items'] = [
  {
    key: '/',
    icon: <LayoutDashboard size={16} />,
    label: 'Dashboard',
  },
  {
    key: '/channels',
    icon: <Rss size={16} />,
    label: 'Channels',
  },
  {
    key: '/downloaders',
    icon: <HardDrive size={16} />,
    label: 'Downloaders',
  },
  {
    key: '/agents',
    icon: <Bot size={16} />,
    label: 'Agents',
  },
];

export default function Sidebar() {
  const navigate = useNavigate();
  const location = useLocation();

  // Determine selected key based on current path
  const selectedKey = menuItems?.find((item) => {
    const key = (item as any)?.key;
    if (key === '/') return location.pathname === '/';
    return location.pathname.startsWith(key as string);
  });

  const handleMenuClick: MenuProps['onClick'] = ({ key }) => {
    navigate(key);
  };

  return (
    <Sider
      width={240}
      style={{
        borderRight: '1px solid #242728',
      }}
    >
      <div
        style={{
          padding: '20px 16px',
          borderBottom: '1px solid #242728',
        }}
      >
        <span style={{ fontSize: 18, fontWeight: 600, color: '#f4f4f6' }}>
          RSSRipple
        </span>
      </div>
      <Menu
        mode="inline"
        selectedKeys={[(selectedKey as any)?.key || '/']}
        items={menuItems}
        onClick={handleMenuClick}
        style={{
          borderRight: 'none',
          padding: '8px 0',
        }}
      />
      <div
        style={{
          position: 'absolute',
          bottom: 16,
          left: 0,
          right: 0,
          textAlign: 'center',
          color: '#6a6b6c',
          fontSize: 12,
        }}
      >
        v0.1.0
      </div>
    </Sider>
  );
}
