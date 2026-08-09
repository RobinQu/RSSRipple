import { useState } from 'react';
import { Alert, Button, Card, Input, Typography } from 'antd';
import { useTranslation } from 'react-i18next';
import { authApi } from '../api/auth';

const { Title, Text } = Typography;

export default function Login() {
  const { t } = useTranslation();
  const [code, setCode] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (value: string) => {
    if (value.length !== 6) return;
    setLoading(true);
    setError(null);
    const r = await authApi.verifyOtp(value);
    setLoading(false);
    if (r.success && r.data.authenticated) {
      // Full reload so every component re-mounts with a clean, authenticated
      // state rather than relying on stale in-memory data.
      location.href = '/';
    } else {
      setError(r.error?.message || t('auth.failed'));
    }
  };

  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 24,
      }}
    >
      <Card style={{ width: 360 }}>
        {/* Let the OTP inputs stretch to the card width instead of clustering
            in the middle at their default narrow size. antd renders each digit
            as .ant-otp-input-wrapper > .ant-otp-input, so the wrappers are the
            flex items that must grow. */}
        <style>{`
          .login-otp.ant-otp { display: flex; width: 100%; }
          .login-otp.ant-otp .ant-otp-input-wrapper { flex: 1; }
          .login-otp.ant-otp .ant-otp-input { width: 100%; }
        `}</style>
        <Title level={3} style={{ marginTop: 0, textAlign: 'center' }}>
          {t('auth.title')}
        </Title>
        <Text
          type="secondary"
          style={{ display: 'block', fontSize: 12, marginBottom: 16, textAlign: 'center' }}
        >
          {t('auth.hint')}
        </Text>
        {error && (
          <Alert type="error" showIcon message={error} style={{ marginBottom: 16 }} />
        )}
        <Input.OTP
          className="login-otp"
          size="large"
          length={6}
          value={code}
          onChange={setCode}
          onInput={(values) => {
            const joined = values.join('');
            if (joined.length === 6) void submit(joined);
          }}
          style={{ marginBottom: 16 }}
        />
        <Button
          type="primary"
          block
          loading={loading}
          disabled={code.length !== 6}
          onClick={() => submit(code)}
        >
          {t('auth.submit')}
        </Button>
      </Card>
    </div>
  );
}
