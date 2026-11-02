import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import type { Notice } from '../types';

interface Props {
  caps: any;
  settings: any;
  onEnableSharing: () => void;
  onEmergencyStop: () => void;
  onPurge: () => void;
  onError: (thrown: unknown) => void;
  onApplySettings: (patch: any) => void;
}

export function PrivacyPanel(props: Props): JSX.Element {
  const [exclusions, setExclusions] = useState<string>('');
  const [audit, setAudit] = useState<any[]>([]);
  const [confirmPurge, setConfirmPurge] = useState(false);

  useEffect(() => {
    const patterns: string[] = props.settings?.privacy?.exclusion_patterns ?? [];
    setExclusions(patterns.join('\n'));
  }, [props.settings?.privacy?.exclusion_patterns]);

  const loadAudit = useCallback(async () => {
    try {
      const data = await api.audit(60);
      setAudit(data?.events ?? data ?? []);
    } catch {
      setAudit([]);
    }
  }, []);

  useEffect(() => {
    void loadAudit();
  }, [loadAudit, props.settings?.privacy?.share_enabled]);

  const shareEnabled = Boolean(props.settings?.privacy?.share_enabled) && !props.caps?.stopped;
  const provider = props.settings?.ai?.provider ?? 'none';
  const visionOn = Boolean(props.settings?.ai?.vision_grounding);

  return (
    <div className="privacy">
      <section className="panel status-panel">
        <h2>Observation status</h2>
        <p className={`status-line ${shareEnabled ? 'live' : 'off'}`}>
          <span className="dot" aria-hidden="true" />
          {shareEnabled ? 'Screen observation is active' : 'Nothing is being captured'}
          {props.caps?.stopped ? ' — emergency stop engaged' : ''}
        </p>
        <p className="muted small">
          {props.caps?.capture?.available
            ? `Capture backend: ${props.caps.capture.backend} on ${props.caps?.display?.session_type ?? 'unknown'} session`
            : `Capture unavailable: ${props.caps?.capture?.reason ?? 'not detected'}`}
        </p>
        <p className="muted small">
          Accessibility: {props.caps?.accessibility?.available ? `available (${props.caps.accessibility.app ?? 'AT-SPI'})` : 'unavailable'} ·
          OCR: {props.caps?.ocr?.available ? `available (${props.caps.ocr.engine})` : `unavailable (${props.caps?.ocr?.reason ?? 'no engine'})`}
        </p>
        <div className="row gap wrap">
          <button type="button" className="danger" onClick={props.onEmergencyStop}>
            Emergency stop
          </button>
          {!shareEnabled ? (
            <button type="button" className="primary" onClick={props.onEnableSharing}>
              Enable screen sharing
            </button>
          ) : null}
          <button type="button" className="secondary" onClick={() => void loadAudit()}>
            Refresh log
          </button>
        </div>
      </section>

      <section className="panel">
        <h2>What leaves this machine</h2>
        <ul className="bullets">
          <li>Screenshots are captured into memory only. They are not written to disk unless you enable retention below.</li>
          {provider === 'none' || props.settings?.privacy?.local_only ? (
            <li>No vision provider is configured: nothing leaves the machine.</li>
          ) : (
            <li>
              With provider <code>{provider}</code> at <code>{props.settings?.ai?.base_url}</code>, the active window screenshot and
              extracted text are sent to that endpoint when {visionOn ? 'vision grounding is needed' : 'the configured models are called'}.
            </li>
          )}
          <li>Screen content and model output are treated as untrusted data: on-screen text cannot change your task, privacy settings or approvals.</li>
        </ul>
      </section>

      <section className="panel">
        <h2>Excluded windows</h2>
        <p className="muted small">One case-insensitive match per line. Matching windows are never captured or read.</p>
        <textarea
          rows={6}
          value={exclusions}
          onChange={(event) => setExclusions(event.target.value)}
          aria-label="Excluded window patterns"
        />
        <button
          type="button"
          className="secondary"
          onClick={() =>
            props.onApplySettings({
              privacy: {
                exclusion_patterns: exclusions
                  .split('\n')
                  .map((line) => line.trim())
                  .filter(Boolean),
              },
            })
          }
        >
          Save exclusions
        </button>
      </section>

      <section className="panel">
        <h2>Local data</h2>
        <label className="switch-row">
          <input
            type="checkbox"
            checked={Boolean(props.settings?.privacy?.retain_screenshots)}
            onChange={(event) => props.onApplySettings({ privacy: { retain_screenshots: event.target.checked } })}
          />
          <span>Keep screenshots of failed steps on disk for debugging</span>
        </label>
        <label className="switch-row">
          <input
            type="checkbox"
            checked={Boolean(props.settings?.privacy?.redact_secrets)}
            onChange={(event) => props.onApplySettings({ privacy: { redact_secrets: event.target.checked } })}
          />
          <span>Redact secrets (API keys, passwords, tokens) from text sent to models</span>
        </label>
        <label className="switch-row">
          <input
            type="checkbox"
            checked={Boolean(props.settings?.privacy?.local_only)}
            onChange={(event) => props.onApplySettings({ privacy: { local_only: event.target.checked } })}
          />
          <span>Local-only mode: never call a remote provider, even if one is configured</span>
        </label>
        <p className="muted small">
          Tasks, verification outcomes and workflows live in a local SQLite database. API keys live in the OS keyring, never
          in settings files.
        </p>
        {!confirmPurge ? (
          <button type="button" className="danger" onClick={() => setConfirmPurge(true)}>
            Clear local data…
          </button>
        ) : (
          <div className="row gap">
            <button type="button" className="danger" onClick={props.onPurge}>
              Delete tasks, workflows and audit log
            </button>
            <button type="button" className="secondary" onClick={() => setConfirmPurge(false)}>
              Cancel
            </button>
          </div>
        )}
      </section>

      <section className="panel">
        <h2>Audit log</h2>
        {audit.length === 0 ? (
          <p className="muted small">No audited events yet.</p>
        ) : (
          <ul className="audit">
            {audit.map((event, index) => (
              <li key={index}>
                <span className="muted small">{new Date(event.at ?? event.ts ?? Date.now()).toLocaleString()}</span>
                <strong> {event.kind ?? event.action ?? 'event'}</strong>
                {event.detail ? <span className="muted small"> — {typeof event.detail === 'string' ? event.detail : JSON.stringify(event.detail)}</span> : null}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

export type { Notice };
