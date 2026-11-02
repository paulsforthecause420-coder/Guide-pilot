import { useEffect, useMemo, useState } from 'react';
import { api } from '../api';
import type { Notice } from '../types';

interface Props {
  settings: any;
  caps: any;
  speech: any;
  busy: string | null;
  onApply: (patch: any) => void;
  onNotice: (notice: Notice) => void;
  onError: (thrown: unknown) => void;
  onRefresh: () => Promise<void>;
}

function Field({ label, hint, children }: { label: string; hint?: string; children: any }): JSX.Element {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
      {hint ? <span className="field-hint">{hint}</span> : null}
    </label>
  );
}

export function SettingsPanel(props: Props): JSX.Element {
  const [draft, setDraft] = useState<any>(props.settings);
  const [dirty, setDirty] = useState(false);
  const [apiKey, setApiKey] = useState('');
  const [usage, setUsage] = useState<any>(null);

  useEffect(() => {
    if (!dirty) setDraft(props.settings);
  }, [props.settings, dirty]);

  useEffect(() => {
    api
      .usage()
      .then(setUsage)
      .catch(() => setUsage(null));
  }, [props.busy, props.settings?.ai?.call_budget]);

  const capInfo = props.caps?.ai ?? {};
  const keyPresent = Boolean(capInfo.key_present);

  const update = (section: string, key: string, value: unknown) => {
    setDraft((current: any) => ({ ...current, [section]: { ...current[section], [key]: value } }));
    setDirty(true);
  };

  const save = () => {
    const sections = ['ai', 'observation', 'perception', 'ui', 'voice', 'automation'];
    const patch: Record<string, unknown> = {};
    for (const section of sections) if (draft?.[section]) patch[section] = draft[section];
    props.onApply(patch);
    setDirty(false);
  };

  const monitors: any[] = props.caps?.display?.monitors ?? [];

  const privacyNote = useMemo(() => {
    const provider = draft?.ai?.provider ?? 'none';
    if (provider === 'none') return 'No AI provider configured: guidance comes from accessibility metadata and local rules only.';
    if (provider === 'ollama') return `Screenshots and extracted text go only to ${draft?.ai?.base_url} on this machine.`;
    return `Screenshots and extracted text are sent to ${draft?.ai?.base_url} when vision assistance is requested.`;
  }, [draft?.ai?.provider, draft?.ai?.base_url]);

  if (!draft) return <p className="muted">Loading settings…</p>;

  return (
    <div className="settings">
      <section className="panel">
        <h2>AI provider</h2>
        <p className="muted small">{privacyNote}</p>
        <div className="grid-2">
          <Field label="Provider">
            <select value={draft.ai.provider} onChange={(event) => update('ai', 'provider', event.target.value)}>
              <option value="none">None (local rules only)</option>
              <option value="ollama">Ollama / local</option>
              <option value="openai_compat">OpenAI-compatible API</option>
            </select>
          </Field>
          <Field label="API base URL" hint="Ollama defaults to http://127.0.0.1:11434">
            <input value={draft.ai.base_url} onChange={(event) => update('ai', 'base_url', event.target.value)} />
          </Field>
          <Field label="Vision model">
            <input value={draft.ai.model_vision} onChange={(event) => update('ai', 'model_vision', event.target.value)} placeholder="e.g. qwen2.5vl:7b" />
          </Field>
          <Field label="Vision grounding" hint="Uses screenshots for element detection when accessibility data is missing">
            <input
              type="checkbox"
              checked={Boolean(draft.ai.vision_grounding)}
              onChange={(event) => update('ai', 'vision_grounding', event.target.checked)}
            />
          </Field>
          <Field label="Reasoning model">
            <input value={draft.ai.model_reasoning} onChange={(event) => update('ai', 'model_reasoning', event.target.value)} placeholder="e.g. qwen2.5:7b" />
          </Field>
          <Field label="Instruction model">
            <input value={draft.ai.model_instruction} onChange={(event) => update('ai', 'model_instruction', event.target.value)} />
          </Field>
          <Field label="Verification model">
            <input value={draft.ai.model_verification} onChange={(event) => update('ai', 'model_verification', event.target.value)} />
          </Field>
          <Field label="Timeout (ms)">
            <input
              type="number"
              min={2000}
              max={300000}
              step={1000}
              value={draft.ai.timeout_ms}
              onChange={(event) => update('ai', 'timeout_ms', Number(event.target.value))}
            />
          </Field>
          <Field label="Max retries">
            <input
              type="number"
              min={0}
              max={6}
              value={draft.ai.max_retries}
              onChange={(event) => update('ai', 'max_retries', Number(event.target.value))}
            />
          </Field>
          <Field label="Model-call budget" hint="Calls allowed per day; raising this needs a deliberate change">
            <input
              type="number"
              min={0}
              max={5000}
              value={draft.ai.call_budget}
              onChange={(event) => update('ai', 'call_budget', Number(event.target.value))}
            />
          </Field>
        </div>
        <div className="key-row">
          <Field label={keyPresent ? 'API key (stored in the OS keyring)' : 'API key'}>
            <input
              type="password"
              value={apiKey}
              placeholder={keyPresent ? '•••••• stored securely' : 'paste key'}
              onChange={(event) => setApiKey(event.target.value)}
              autoComplete="off"
            />
          </Field>
          <button
            type="button"
            className="secondary"
            disabled={!apiKey.trim()}
            onClick={async () => {
              try {
                await api.setApiKey(apiKey.trim());
                setApiKey('');
                props.onNotice({ message: 'API key stored in the OS keyring.', level: 'info', at: Date.now() });
                await props.onRefresh();
              } catch (thrown) {
                props.onError(thrown);
              }
            }}
          >
            Save key
          </button>
          <button
            type="button"
            className="link-button"
            disabled={!keyPresent}
            onClick={async () => {
              try {
                await api.deleteApiKey();
                props.onNotice({ message: 'API key removed.', level: 'info', at: Date.now() });
                await props.onRefresh();
              } catch (thrown) {
                props.onError(thrown);
              }
            }}
          >
            Remove key
          </button>
        </div>
        {usage ? (
          <p className="muted small">
            Model calls today: {usage.calls_today ?? usage.calls ?? '—'} / {draft.ai.call_budget}
            {usage.remaining !== undefined ? ` · ${usage.remaining} remaining` : ''}
          </p>
        ) : null}
      </section>

      <section className="panel">
        <h2>Screen observation</h2>
        <div className="grid-2">
          <Field label="Scope">
            <select value={draft.observation.scope} onChange={(event) => update('observation', 'scope', event.target.value)}>
              <option value="active_window">Active window</option>
              <option value="monitor">One monitor</option>
              <option value="all_monitors">All monitors</option>
            </select>
          </Field>
          <Field label="Monitor">
            <select
              value={draft.observation.monitor_id ?? ''}
              onChange={(event) => update('observation', 'monitor_id', event.target.value || null)}
              disabled={draft.observation.scope === 'active_window'}
            >
              <option value="">Primary</option>
              {monitors.map((monitor: any) => (
                <option key={monitor.id} value={monitor.id}>
                  {monitor.name} · {monitor.width}×{monitor.height}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Auto-check cadence (ms)" hint="0 disables automatic re-checks entirely">
            <input
              type="number"
              min={0}
              max={60000}
              step={250}
              value={draft.observation.cadence_ms}
              onChange={(event) => update('observation', 'cadence_ms', Number(event.target.value))}
            />
          </Field>
          <Field label="Change threshold" hint="Skip re-analysis when less of the screen than this changed">
            <input
              type="number"
              min={0}
              max={0.5}
              step={0.001}
              value={draft.observation.min_change_ratio}
              onChange={(event) => update('observation', 'min_change_ratio', Number(event.target.value))}
            />
          </Field>
          <Field label="Downscale width for inference">
            <input
              type="number"
              min={640}
              max={3840}
              step={64}
              value={draft.observation.downscale_width}
              onChange={(event) => update('observation', 'downscale_width', Number(event.target.value))}
            />
          </Field>
          <Field label="Low-resource mode" hint="Back off polling when the screen is idle">
            <input
              type="checkbox"
              checked={Boolean(draft.observation.idle_backoff)}
              onChange={(event) => update('observation', 'idle_backoff', event.target.checked)}
            />
          </Field>
          <Field label="Use accessibility tree">
            <input
              type="checkbox"
              checked={Boolean(draft.perception.use_accessibility)}
              onChange={(event) => update('perception', 'use_accessibility', event.target.checked)}
            />
          </Field>
          <Field label="Use OCR fallback">
            <input
              type="checkbox"
              checked={Boolean(draft.perception.use_ocr)}
              onChange={(event) => update('perception', 'use_ocr', event.target.checked)}
            />
          </Field>
          <Field label="Minimum grounding confidence">
            <input
              type="number"
              min={0.3}
              max={0.95}
              step={0.05}
              value={draft.perception.min_grounding_confidence}
              onChange={(event) => update('perception', 'min_grounding_confidence', Number(event.target.value))}
            />
          </Field>
          <Field label="Target application hint" hint="Optional: narrows window matching, e.g. an app name">
            <input value={draft.perception.app_hint} onChange={(event) => update('perception', 'app_hint', event.target.value)} />
          </Field>
        </div>
      </section>

      <section className="panel">
        <h2>Interface</h2>
        <div className="grid-2">
          <Field label="Theme">
            <select value={draft.ui.theme} onChange={(event) => update('ui', 'theme', event.target.value)}>
              <option value="dark">Dark</option>
              <option value="light">Light</option>
            </select>
          </Field>
          <Field label={`Opacity ${Math.round(Number(draft.ui.opacity) * 100)}%`}>
            <input
              type="range"
              min={0.6}
              max={1}
              step={0.01}
              value={draft.ui.opacity}
              onChange={(event) => update('ui', 'opacity', Number(event.target.value))}
            />
          </Field>
          <Field label="Always on top" hint="Uses the X11 path when available; Wayland compositors may ignore it">
            <input
              type="checkbox"
              checked={Boolean(draft.ui.always_on_top)}
              onChange={(event) => update('ui', 'always_on_top', event.target.checked)}
            />
          </Field>
          <Field label="Highlight overlay">
            <input
              type="checkbox"
              checked={Boolean(draft.ui.overlay_enabled)}
              onChange={(event) => update('ui', 'overlay_enabled', event.target.checked)}
            />
          </Field>
          <Field label="Pulse highlight">
            <input
              type="checkbox"
              checked={Boolean(draft.ui.pulse_highlight)}
              onChange={(event) => update('ui', 'pulse_highlight', event.target.checked)}
            />
          </Field>
          <Field label="Dim around target">
            <input
              type="checkbox"
              checked={Boolean(draft.ui.dim_background)}
              onChange={(event) => update('ui', 'dim_background', event.target.checked)}
            />
          </Field>
          <Field label="Edge docking">
            <select value={draft.ui.edge_docking} onChange={(event) => update('ui', 'edge_docking', event.target.value)}>
              <option value="none">None</option>
              <option value="left">Left</option>
              <option value="right">Right</option>
            </select>
          </Field>
          <Field label="Teach me mode" hint="Explains the reasoning behind every step">
            <input
              type="checkbox"
              checked={Boolean(draft.ui.teach_me_mode)}
              onChange={(event) => update('ui', 'teach_me_mode', event.target.checked)}
            />
          </Field>
        </div>
      </section>

      <section className="panel">
        <h2>Voice</h2>
        <div className="grid-2">
          <Field label="Voice enabled" hint="Off by default; no microphone is used unless enabled">
            <input
              type="checkbox"
              checked={Boolean(draft.voice.enabled)}
              onChange={(event) => update('voice', 'enabled', event.target.checked)}
            />
          </Field>
          <Field label="Speech output engine">
            <select value={draft.voice.tts_engine} onChange={(event) => update('voice', 'tts_engine', event.target.value)}>
              <option value="auto">Auto-detect</option>
              <option value="espeak-ng">espeak-ng</option>
              <option value="spd-say">spd-say</option>
              <option value="none">None</option>
            </select>
          </Field>
          <Field label="Speech input engine">
            <select value={draft.voice.stt_engine} onChange={(event) => update('voice', 'stt_engine', event.target.value)}>
              <option value="auto">Auto-detect</option>
              <option value="whisper">Local whisper.cpp</option>
              <option value="none">None</option>
            </select>
          </Field>
          <Field label="Push-to-talk shortcut" hint="Registered globally; disable voice to unregister">
            <input value={draft.voice.push_to_talk} onChange={(event) => update('voice', 'push_to_talk', event.target.value)} />
          </Field>
          <Field label="Speak steps">
            <input
              type="checkbox"
              checked={Boolean(draft.voice.speak_steps)}
              onChange={(event) => update('voice', 'speak_steps', event.target.checked)}
            />
          </Field>
          <Field label="Speak errors">
            <input
              type="checkbox"
              checked={Boolean(draft.voice.speak_errors)}
              onChange={(event) => update('voice', 'speak_errors', event.target.checked)}
            />
          </Field>
        </div>
        <p className="muted small">
          TTS: {props.speech?.tts?.available ? `available (${props.speech.tts.engine})` : 'no engine found'} · STT:{' '}
          {props.speech?.stt?.available ? `available (${props.speech.stt.engine})` : 'unavailable'}
        </p>
      </section>

      <section className="panel">
        <h2>Assisted actions</h2>
        <p className="muted small">
          GuidePilot never acts on its own. Assisted paste only fills text you explicitly approved, and never for sensitive
          fields.
        </p>
        <div className="grid-2">
          <Field label="Assisted actions enabled">
            <input
              type="checkbox"
              checked={Boolean(draft.automation.assisted_actions_enabled)}
              onChange={(event) => update('automation', 'assisted_actions_enabled', event.target.checked)}
            />
          </Field>
          <Field label="Allow assisted paste">
            <input
              type="checkbox"
              checked={Boolean(draft.automation.allow_paste)}
              onChange={(event) => update('automation', 'allow_paste', event.target.checked)}
            />
          </Field>
          <Field label="Allow focusing a field">
            <input
              type="checkbox"
              checked={Boolean(draft.automation.allow_focus)}
              onChange={(event) => update('automation', 'allow_focus', event.target.checked)}
            />
          </Field>
          <Field label="Allow scrolling to target">
            <input
              type="checkbox"
              checked={Boolean(draft.automation.allow_scroll)}
              onChange={(event) => update('automation', 'allow_scroll', event.target.checked)}
            />
          </Field>
          <Field label="Always require confirmation">
            <input
              type="checkbox"
              checked={Boolean(draft.automation.require_confirmation)}
              onChange={(event) => update('automation', 'require_confirmation', event.target.checked)}
            />
          </Field>
        </div>
      </section>

      <div className="sticky-actions">
        <button type="button" className="primary" onClick={save} disabled={!dirty}>
          {dirty ? 'Save changes' : 'Saved'}
        </button>
        {dirty ? (
          <button
            type="button"
            className="secondary"
            onClick={() => {
              setDraft(props.settings);
              setDirty(false);
            }}
          >
            Discard
          </button>
        ) : null}
      </div>

      <details className="panel diagnostics">
        <summary>Environment & capability report</summary>
        <pre>{JSON.stringify(props.caps ?? {}, null, 2)}</pre>
      </details>
    </div>
  );
}
