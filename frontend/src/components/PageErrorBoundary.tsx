import { Component, type ErrorInfo, type ReactNode } from 'react';
import { useLocation } from 'react-router-dom';
import { AlertTriangle, RotateCcw } from 'lucide-react';
import { Button } from 'antd';

interface ErrorBoundaryProps {
  children: ReactNode;
}

interface ErrorBoundaryState {
  error: Error | null;
}

class PageErrorBoundaryInner extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Page render failed', error, info);
  }

  render() {
    if (!this.state.error) return this.props.children;

    return (
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          minHeight: '60vh',
          padding: 24,
        }}
      >
        <div
          style={{
            maxWidth: 520,
            width: '100%',
            border: '1px solid #eeece7',
            borderRadius: 8,
            padding: 24,
            background: '#ffffff',
          }}
        >
          <div style={{ display: 'flex', gap: 12, alignItems: 'flex-start' }}>
            <AlertTriangle size={22} style={{ color: '#b30000', marginTop: 2 }} />
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 16, fontWeight: 600, marginBottom: 8 }}>
                Page failed to render
              </div>
              <div style={{ color: '#75758a', fontSize: 13, lineHeight: 1.6, marginBottom: 16 }}>
                {this.state.error.message || 'An unexpected rendering error occurred.'}
              </div>
              <Button
                htmlType="button"
                icon={<RotateCcw size={14} />}
                onClick={() => window.location.reload()}
              >
                Reload
              </Button>
            </div>
          </div>
        </div>
      </div>
    );
  }
}

export default function PageErrorBoundary({ children }: ErrorBoundaryProps) {
  const location = useLocation();
  return (
    <PageErrorBoundaryInner key={location.pathname}>
      {children}
    </PageErrorBoundaryInner>
  );
}
