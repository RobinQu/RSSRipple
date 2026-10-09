import { useState } from 'react';
import {
  Button,
  Card,
  Col,
  Row,
  Space,
  Statistic,
  Tag,
  Typography,
  App,
} from 'antd';
import { CheckCircle, FlaskConical } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { agentsApi } from '../../api/agents';
import FilterBuilder from '../../components/FilterBuilder';
import { findInvalidConditions, nullIfEmptyFilter } from '../../components/filterUtils';
import type { Agent, FilterConfig, FilterField, FilterTestResponse } from '../../types';

const { Text } = Typography;

/** Filters tab: the global filter DSL editor (gated by the channel's
 * required fields) plus the test-run result breakdown. */
export default function FiltersTab({
  agent,
  filterConfig,
  onFilterConfigChange,
  allowedFilterFields,
  onSaved,
}: {
  agent: Agent;
  filterConfig: FilterConfig | null;
  onFilterConfigChange: (config: FilterConfig | null) => void;
  /** Channel required-fields gate for the filter DSL editor (null =
   * unrestricted; pick preferences are exempt and never receive this). */
  allowedFilterFields: FilterField[] | null;
  /** Filter saved: the parent reloads the agent. */
  onSaved: () => void;
}) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [filterTest, setFilterTest] = useState<FilterTestResponse | null>(null);
  const [testingFilters, setTestingFilters] = useState(false);
  const [savingFilter, setSavingFilter] = useState(false);

  const handleSaveFilter = async () => {
    // Backend rejects value-taking operators with empty values (422).
    if (findInvalidConditions(filterConfig).length > 0) {
      message.error(t('filter.emptyValueNotAllowed'));
      return;
    }
    setSavingFilter(true);
    const r = await agentsApi.update(agent.id, {
      name: agent.name,
      channel_id: agent.channel_id,
      downloader_id: agent.downloader_id,
      filter_config: nullIfEmptyFilter(filterConfig),
    });
    setSavingFilter(false);
    if (r.success) {
      message.success(t('agents.filterSaved'));
      onSaved();
    } else message.error(r.error?.message || t('agents.saveFailed'));
  };

  const handleTestFilters = async () => {
    setTestingFilters(true);
    const r = await agentsApi.testFilters(agent.id);
    setTestingFilters(false);
    if (r.success) setFilterTest(r.data);
    else message.error(r.error?.message || t('agents.testFailed'));
  };

  return (
    <div>
      <Card style={{ marginBottom: 16 }}>
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            flexWrap: 'wrap',
            gap: 8,
            marginBottom: 12,
          }}
        >
          <Text strong>{t('agents.globalFilter')}</Text>
          <Space>
            <Button
              icon={<FlaskConical size={14} />}
              onClick={handleTestFilters}
              loading={testingFilters}
            >
              {t('agents.test')}
            </Button>
            <Button type="primary" onClick={handleSaveFilter} loading={savingFilter}>
              {t('common.save')}
            </Button>
          </Space>
        </div>
        <FilterBuilder value={filterConfig} onChange={onFilterConfigChange} allowedFields={allowedFilterFields} />
      </Card>

      {filterTest && (
        <Card title={t('agents.testResults')} size="small">
          <Row gutter={16} style={{ marginBottom: 16 }}>
            <Col xs={24} sm={8}>
              <Statistic title={t('agents.totalResources')} value={filterTest.total} />
            </Col>
            <Col xs={24} sm={8}>
              <Statistic
                title={t('agents.passed')}
                value={filterTest.passed}
                valueStyle={{ color: 'var(--rr-success)' }}
              />
            </Col>
            <Col xs={24} sm={8}>
              <Statistic
                title={t('agents.failed')}
                value={filterTest.total - filterTest.passed}
                valueStyle={{ color: 'var(--rr-error)' }}
              />
            </Col>
          </Row>
          <div style={{ maxHeight: 500, overflow: 'auto' }}>
            {filterTest.resources.map((r) => (
              <div
                key={r.resource_id}
                style={{
                  padding: 10,
                  marginBottom: 6,
                  borderRadius: 6,
                  border: `1px solid ${r.passed ? 'var(--rr-success-border)' : 'var(--rr-error-border)'}`,
                  background: r.passed
                    ? 'var(--rr-success-soft)'
                    : 'var(--rr-error-soft)',
                }}
              >
                <Space style={{ marginBottom: 4 }}>
                  {r.passed ? (
                    <CheckCircle size={14} color="var(--rr-success)" />
                  ) : (
                    <Tag color="error">FAIL</Tag>
                  )}
                  <Text strong ellipsis style={{ fontSize: 13 }}>
                    {r.title_raw}
                  </Text>
                </Space>
                <div>
                  {r.condition_results.map((c, i) => (
                    <Tag
                      key={i}
                      color={c.passed ? 'green' : 'red'}
                      style={{ fontSize: 11, margin: 2 }}
                    >
                      {c.field} {c.operator} {String(c.value)}
                    </Tag>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}
