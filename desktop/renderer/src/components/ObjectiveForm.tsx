import { useState } from 'react';
import type { ObservationView } from '../types';

interface Props {
  objective: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  busy: boolean;
  disabled: boolean;
  contextValues: Record<string, string>;
  onContextChange: (values: Record<string, string>) => void;
  observation: ObservationView | null;
}

const EXAMPLES = [
  'Walk me through configuring this application',
  'Help me fill in this form correctly',
  'Install PostgreSQL on this machine',
  'Help me set up GitHub OAuth for my app',
];

export function ObjectiveForm(props: Props): JSX.Element {
  const [key, setKey] = useState('');
  const [value, setValue] = useState('');

  const entries = Object.entries(props.contextValues);

  return (
    <section className="objective" aria-label="Start a task">
      <h2>What would you like help doing?</h2>
      <p className="muted small">
        GuidePilot reads the current screen, highlights the real control for each step and verifies your result before
        moving on.
      </p>
      <textarea
        aria-label="Describe your objective"
        value={props.objective}
        onChange={(event) => props.onChange(event.target.value)}
        placeholder="e.g. Walk me through configuring this application"
        rows={3}
        onKeyDown={(event) => {
          if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') props.onSubmit();
        }}
      />
      <div className="chips">
        {EXAMPLES.map((example) => (
          <button key={example} type="button" className="chip" onClick={() => props.onChange(example)}>
            {example}
          </button>
        ))}
      </div>

      <details className="context-editor" open={entries.length > 0}>
        <summary>Values I already know (optional)</summary>
        <p className="muted small">
          Values you provide here are used exactly as typed. GuidePilot never invents credentials, callback URLs or IDs.
        </p>
        {entries.length ? (
          <ul className="kv-list">
            {entries.map(([entryKey, entryValue]) => (
              <li key={entryKey}>
                <span>{entryKey}</span>
                <code>{entryValue}</code>
                <button
                  type="button"
                  className="link-button"
                  onClick={() => {
                    const next = { ...props.contextValues };
                    delete next[entryKey];
                    props.onContextChange(next);
                  }}
                  aria-label={`Remove ${entryKey}`}
                >
                  remove
                </button>
              </li>
            ))}
          </ul>
        ) : null}
        <div className="row gap wrap">
          <input aria-label="Value field name" placeholder="Field (e.g. App Name)" value={key} onChange={(event) => setKey(event.target.value)} />
          <input aria-label="Value" placeholder="Value" value={value} onChange={(event) => setValue(event.target.value)} />
          <button
            type="button"
            className="secondary"
            onClick={() => {
              if (!key.trim() || !value.trim()) return;
              props.onContextChange({ ...props.contextValues, [key.trim()]: value.trim() });
              setKey('');
              setValue('');
            }}
          >
            Add value
          </button>
        </div>
      </details>

      <button type="button" className="primary wide" onClick={props.onSubmit} disabled={props.busy || props.disabled}>
        {props.busy ? 'Reading the screen…' : 'Start guiding'}
      </button>
      {props.disabled ? <p className="muted small">Enable screen sharing to begin.</p> : null}

      {props.observation ? (
        <div className="screen-summary">
          <span className="eyebrow">Current screen</span>
          <p className="value">
            {props.observation.active_window?.title || 'No active window detected'}
            {props.observation.active_window?.app_class ? (
              <span className="muted small"> · {props.observation.active_window.app_class}</span>
            ) : null}
          </p>
          <p className="muted small">
            {props.observation.element_count} labelled elements ·{' '}
            {props.observation.sources?.accessibility?.used
              ? `accessibility via ${props.observation.sources.accessibility.app}`
              : 'accessibility unavailable'}
            {props.observation.sensitive ? ' · privacy-excluded window' : ''}
          </p>
        </div>
      ) : null}
    </section>
  );
}
