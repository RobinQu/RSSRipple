import { Suspense } from 'react';
import { Grid, Layout, Spin } from 'antd';
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
          <Suspense
            fallback={(
              <div
                aria-label="Loading"
                style={{
                  minHeight: 240,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                }}
              >
                <Spin />
              </div>
            )}
          >
            <Outlet />
          </Suspense>
        </Content>
      </Layout>
    </Layout>
  );
}
