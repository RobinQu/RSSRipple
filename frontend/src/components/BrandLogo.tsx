type BrandLogoProps = {
  collapsed?: boolean;
};

function PixelRIcon({ size = 38 }: { size?: number }) {
  return (
    <svg
      aria-hidden="true"
      width={size}
      height={size}
      viewBox="0 0 76 76"
      role="img"
      style={{ display: 'block', flex: '0 0 auto', shapeRendering: 'crispEdges' }}
    >
      <rect fill="var(--rr-logo-bg)" x="0" y="0" width="76" height="76" />
      <rect fill="var(--rr-logo-fg)" x="20" y="14" width="12" height="48" />
      <rect fill="var(--rr-logo-fg)" x="32" y="14" width="20" height="12" />
      <rect fill="var(--rr-logo-fg)" x="52" y="26" width="12" height="12" />
      <rect fill="var(--rr-logo-fg)" x="32" y="38" width="20" height="12" />
      <rect fill="#ff7759" x="44" y="50" width="12" height="12" />
      <rect fill="#1863dc" x="56" y="62" width="8" height="8" />
    </svg>
  );
}

export default function BrandLogo({ collapsed = false }: BrandLogoProps) {
  if (collapsed) {
    return (
      <div
        aria-label="RSSRipple"
        style={{
          display: 'flex',
          justifyContent: 'center',
          width: '100%',
        }}
      >
        <PixelRIcon size={38} />
      </div>
    );
  }

  return (
    <div
      aria-label="RSSRipple"
      style={{
        alignItems: 'center',
        display: 'flex',
        gap: 12,
        justifyContent: 'flex-start',
        width: '100%',
      }}
    >
      <PixelRIcon size={38} />
      <span
        style={{
          color: 'var(--rr-text)',
          fontFamily: "'Inter', 'Unica77 Cohere Web', system-ui, -apple-system, sans-serif",
          fontSize: 23,
          fontWeight: 850,
          letterSpacing: 0,
          lineHeight: 1,
          whiteSpace: 'nowrap',
        }}
      >
        RSSRipple
      </span>
    </div>
  );
}
