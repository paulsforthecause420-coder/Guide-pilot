import type { Notice } from '../types';

interface TitleBarProps {
  shareEnabled: boolean;
  sessionType: string;
  busy: string | null;
  compact: boolean;
  onToggleSharing: () => void;
  onToggleCompact: () => void;
  onMinimize: () => void;
  onClose: () => void;
  onSettings: () => void;
}

export function TitleBar(props: TitleBarProps): JSX.Element {
  return (
    <header className="titlebar">
      <div className="brand drag-region">
        <span className="logo" aria-hidden="true">
          ◈
        </span>
        <div>
          <h1>GuidePilot</h1>
          <p className="tagline">See it. Learn it. Do it.</p>
        </div>
      </div>
      <div className="titlebar-actions">
        <button
          type="button"
          className={props.shareEnabled ? 'pill live' : 'pill'}
          onClick={props.onToggleSharing}
          aria-pressed={props.shareEnabled}
          title={props.shareEnabled ? 'Stop sharing your screen' : 'Enable screen sharing'}
        >
          <span className="dot" aria-hidden="true" />
          {props.shareEnabled ? 'Sharing' : 'Not sharing'}
        </button>
        <span className="session-chip" title={`Session: ${props.sessionType}`}>
          {props.sessionType.toUpperCase()}
        </span>
        {props.busy ? (
          <span className="busy-chip" role="status">
            {props.busy}…
          </span>
        ) : null}
        <button type="button" className="icon-button" onClick={props.onSettings} aria-label="Open settings">
          ⚙
        </button>
        <button
          type="button"
          className="icon-button"
          onClick={props.onToggleCompact}
          aria-label={props.compact ? 'Expand window' : 'Collapse window'}
          aria-pressed={props.compact}
        >
          {props.compact ? '⤢' : '⤡'}
        </button>
        <button type="button" className="icon-button" onClick={props.onMinimize} aria-label="Minimize window">
          –
        </button>
        <button type="button" className="icon-button close" onClick={props.onClose} aria-label="Hide GuidePilot">
          ×
        </button>
      </div>
    </header>
  );
}

export function NoticeStack({ notices, onDismiss }: { notices: Notice[]; onDismiss: (notice: Notice) => void }): JSX.Element | null {
  if (!notices.length) return null;
  return (
    <div className="notices" role="status" aria-live="polite">
      {notices.map((notice, index) => (
        <div key={`${notice.at}-${index}`} className={`notice ${notice.level}`}>
          <span>{notice.message}</span>
          <button type="button" className="link-button" onClick={() => onDismiss(notice)} aria-label="Dismiss notice">
            ×
          </button>
        </div>
      ))}
    </div>
  );
}

export function StateChip({ state }: { state: string }): JSX.Element {
  const tone =
    state === 'COMPLETED'
      ? 'done'
      : state === 'BLOCKED' || state === 'CANCELLED'
        ? 'bad'
        : state === 'PAUSED'
          ? 'warn'
          : state === 'VERIFYING'
            ? 'verify'
            : 'active';
  return (
    <span className={`state-chip ${tone}`} role="status">
      {state.replace(/_/g, ' ').toLowerCase()}
    </span>
  );
}

export function ProgressStrip({ completed, total }: { completed: number; total: number }): JSX.Element {
  const percent = total > 0 ? Math.round((completed / total) * 100) : 0;
  return (
    <div className="progress" aria-label={`Task progress ${percent}%`}>
      <div className="progress-bar">
        <div className="progress-fill" style={{ width: `${percent}%` }} />
      </div>
      <span className="muted small">
        {completed}/{total} verified
      </span>
    </div>
  );
}

export function ShareBanner({
  reason,
  shareEnabled,
  onEnable,
}: {
  reason: string | null;
  shareEnabled: boolean;
  onEnable: () => void;
}): JSX.Element | null {
  if (!reason) return null;
  return (
    <div className={shareEnabled ? 'gate warning' : 'gate'} role="status">
      <div>
        <strong>{shareEnabled ? 'Guidance limited' : 'Screen sharing is off'}</strong>
        <p>{reason}</p>
        <p className="muted small">
          GuidePilot reads window titles, accessibility labels and a screenshot of the active window. Screenshots stay in
          memory, are never written to disk by default and are never sent anywhere unless you configure a vision provider.
        </p>
      </div>
      {!shareEnabled ? (
        <button type="button" className="primary" onClick={onEnable}>
          Enable screen sharing
        </button>
      ) : null}
    </div>
  );
}
