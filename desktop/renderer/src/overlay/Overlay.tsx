import { useEffect, useState } from 'react';

interface OverlayPayload {
  rect: [number, number, number, number];
  localRect: { x: number; y: number; width: number; height: number };
  labelBox: { x: number; y: number; placement: 'above' | 'below' };
  label: string;
  instruction: string;
  number?: number | null;
  confidence?: number;
  source?: string;
  pulsing?: boolean;
  dim: boolean;
  monitorName: string;
  viewport: { width: number; height: number };
}

/**
 * The overlay renders one highlight at a time. The main process has already
 * translated physical screen coordinates into this window's CSS pixel space, so
 * the renderer only draws and never computes geometry.
 */
export function Overlay(): JSX.Element | null {
  const [payload, setPayload] = useState<OverlayPayload | null>(null);

  useEffect(() => {
    if (!window.overlay) return;
    return window.overlay.onRender((next: OverlayPayload) => setPayload(next));
  }, []);

  if (!payload) return null;
  const { localRect, labelBox } = payload;
  const arrowSide = labelBox.placement === 'below' ? 'up' : 'down';

  return (
    <div className="overlay-root" aria-live="polite">
      {payload.dim ? <div className="overlay-dim" /> : null}

      <div
        className={`overlay-highlight ${payload.pulsing === false ? 'static' : 'pulse'}`}
        style={{
          left: `${localRect.x}px`,
          top: `${localRect.y}px`,
          width: `${localRect.width}px`,
          height: `${localRect.height}px`,
        }}
      >
        <span className="overlay-ring" />
        <span className={`overlay-arrow ${arrowSide}`} aria-hidden="true">
          {arrowSide === 'up' ? '▲' : '▼'}
        </span>
      </div>

      <div
        className={`overlay-label ${labelBox.placement}`}
        style={{ left: `${labelBox.x}px`, top: `${labelBox.y}px` }}
      >
        <div className="overlay-label-head">
          {payload.number ? <span className="overlay-number">{payload.number}</span> : null}
          <strong>{payload.label}</strong>
          <span className="overlay-confidence">
            {Math.round((payload.confidence ?? 0) * 100)}%{payload.source ? ` · ${payload.source}` : ''}
          </span>
        </div>
        <p>{payload.instruction}</p>
      </div>
    </div>
  );
}
