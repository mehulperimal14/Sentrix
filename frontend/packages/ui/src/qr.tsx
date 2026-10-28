import QRCode from 'qrcode';
import { useMemo } from 'react';

/** A QR code drawn as inline SVG (no network, works on printed letters). */
export function QrCode({ value, size = 96, label }: { value: string; size?: number; label: string }) {
  const path = useMemo(() => {
    const qr = QRCode.create(value, { errorCorrectionLevel: 'M' });
    const n = qr.modules.size;
    let d = '';
    for (let y = 0; y < n; y += 1) {
      for (let x = 0; x < n; x += 1) {
        if (qr.modules.get(x, y)) d += `M${x} ${y}h1v1h-1z`;
      }
    }
    return { d, n };
  }, [value]);
  return (
    <svg role="img" aria-label={label} viewBox={`-2 -2 ${path.n + 4} ${path.n + 4}`} width={size} height={size} shapeRendering="crispEdges">
      <rect x={-2} y={-2} width={path.n + 4} height={path.n + 4} fill="#fff" />
      <path d={path.d} fill="#111" />
    </svg>
  );
}
