import { useState, useCallback } from 'react';
import { Link } from 'react-router-dom';
import { Bot, AlertTriangle, Download, Plus, Rss } from 'lucide-react';
import { Typography, Row, Col, Card, Statistic, Spin, Empty, Button, Space } from 'antd';
import { dashboardApi } from '../api/tasks';
import { usePolling } from '../hooks/usePolling';
import StatusBadge from '../components/StatusBadge';
import ProgressBar from '../components/ProgressBar';
import { formatSpeed, formatEta, timeAgo } from '../utils/format';
import type { DashboardData } from '../types';

const { Title } = Typography;

export default function Dashboard() {
  const [dashboard, setDashboard] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchData = useCallback(async () => {
    const res = await dashboardApi.get();
    if (res.success) setDashboard(res.data);
    setLoading(false);
  }, []);

  usePolling(fetchData, 10000);

  if (loading) return <div style={{ display: 'flex', justifyContent: 'center', padding: 48 }}><Spin size="large" /></div>;
  if (!dashboard) return <Empty description="No data" />;

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 24 }}>
        <Title level={3} style={{ margin: 0 }}>Dashboard</Title>
        <Space>
          <Link to="/channels/new">
            <Button type="primary" icon={<Rss size={16} />}>Add Channel</Button>
          </Link>
          <Link to="/agents/new">
            <Button icon={<Plus size={16} />}>Add Agent</Button>
          </Link>
        </Space>
      </div>

      <Row gutter={[16, 16]} style={{ marginBottom: 32 }}>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic title="Active Agents" value={dashboard.active_agents} prefix={<Bot size={18} />} />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic title="Active Downloads" value={dashboard.active_downloads.length} prefix={<Download size={18} />} />
          </Card>
        </Col>
        <Col xs={24} sm={8}>
          <Card>
            <Statistic title="Pending Decisions" value={dashboard.pending_decisions.length} prefix={<AlertTriangle size={18} />} />
          </Card>
        </Col>
      </Row>

      <Card title="Active Downloads" style={{ marginBottom: 24 }}>
        {dashboard.active_downloads.length === 0 ? (
          <Empty description="No active downloads" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
            {dashboard.active_downloads.map((task) => (
              <div key={task.id} style={{ padding: '8px 0', borderBottom: '1px solid rgba(255,255,255,0.06)' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                  <Space>
                    <span style={{ fontWeight: 500, maxWidth: 400, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {task.file_resource?.title_raw || task.id}
                    </span>
                    <StatusBadge status={task.status} />
                  </Space>
                  <Space size="large" style={{ color: 'rgba(255,255,255,0.45)', fontSize: 13 }}>
                    <span>{formatSpeed(task.download_speed)}</span>
                    <span>ETA: {formatEta(task.eta)}</span>
                    <span>{task.progress.toFixed(1)}%</span>
                  </Space>
                </div>
                <ProgressBar progress={task.progress} />
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card title="Pending Decisions">
        {dashboard.pending_decisions.length === 0 ? (
          <Empty description="No pending decisions" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
            {dashboard.pending_decisions.map((d) => (
              <div key={d.id} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '8px 0', borderBottom: '1px solid rgba(255,255,255,0.06)' }}>
                <div>
                  <div style={{ fontWeight: 500 }}>{d.reason}</div>
                  <div style={{ fontSize: 12, color: 'rgba(255,255,255,0.45)', marginTop: 4 }}>
                    {d.candidates.length} candidates · {timeAgo(d.created_at)}
                  </div>
                </div>
                <StatusBadge status={d.status} />
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
