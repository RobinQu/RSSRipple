import { Button, Result } from 'antd';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';

export default function NotFound() {
  const { t } = useTranslation();
  return (
    <Result
      status="404"
      title="404"
      subTitle={t('common.pageNotFound')}
      extra={(
        <Link to="/">
          <Button type="primary">{t('common.backHome')}</Button>
        </Link>
      )}
    />
  );
}
