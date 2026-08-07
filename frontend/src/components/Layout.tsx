import { Grid, Layout } from 'antd';
import { Outlet } from 'react-router-dom';
import Sidebar, { MobileNav } from './Sidebar';

const { Content } = Layout;

export default function AppLayout() {
  const screens = Grid.useBreakpoint();
  // Below lg (~992px) the sider gives way to a top bar + drawer nav so the
  // content gets the full (narrow) viewport width.
  const isMobile = !screens.lg;
  return (
    <Layout style={{ minHeight: '100vh' }}>
      {isMobile ? <MobileNav /> : <Sidebar />}
      <Layout>
        <Content
          style={{
            padding: isMobile ? 12 : 24,
            overflow: 'auto',
            minHeight: '100vh',
          }}
        >
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  );
}
