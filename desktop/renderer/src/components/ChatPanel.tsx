import { useState } from 'react';
import type { ChatEntry } from '../types';

interface Props {
  entries: ChatEntry[];
  onSend: (message: string) => void;
  busy: boolean;
  onRepeat: () => void;
  speech: any;
  onSpeak: (text: string) => void;
}

const QUICK = ['What do I click?', 'Did I do that correctly?', 'Explain like I\'m a beginner', 'Why is this not working?'];

export function ChatPanel(props: Props): JSX.Element {
  const [draft, setDraft] = useState('');
  const submit = () => {
    if (!draft.trim()) return;
    props.onSend(draft.trim());
    setDraft('');
  };

  return (
    <section className="chat" aria-label="Conversation">
      <header className="row between">
        <h3>Ask about what's on screen</h3>
        <button type="button" className="link-button" onClick={props.onRepeat}>
          Repeat step
        </button>
      </header>

      <div className="chat-log" role="log" aria-live="polite">
        {props.entries.length === 0 ? (
          <p className="muted small">
            Ask things like “What do I click?”, “Did that work?”, or “Why does this permission matter?”. Answers use the
            current verified screen state.
          </p>
        ) : null}
        {props.entries.map((entry, index) => (
          <div key={index} className={`bubble ${entry.role}`}>
            <span className="bubble-role">{entry.role === 'user' ? 'You' : 'GuidePilot'}</span>
            <p>{entry.text}</p>
            {entry.warnings?.length ? <p className="muted small">{entry.warnings.join(' ')}</p> : null}
            {entry.role === 'assistant' && props.speech?.tts?.available && props.speech?.enabled ? (
              <button type="button" className="link-button" onClick={() => props.onSpeak(entry.text)}>
                Speak
              </button>
            ) : null}
          </div>
        ))}
        {props.busy ? <p className="muted small">Thinking…</p> : null}
      </div>

      <div className="chips">
        {QUICK.map((prompt) => (
          <button key={prompt} type="button" className="chip" onClick={() => props.onSend(prompt)}>
            {prompt}
          </button>
        ))}
      </div>

      <div className="row gap">
        <input
          aria-label="Ask GuidePilot"
          placeholder="Ask GuidePilot…"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') submit();
          }}
        />
        <button type="button" className="primary" onClick={submit} disabled={props.busy}>
          Send
        </button>
      </div>
      {props.speech ? (
        <p className="muted small">
          Voice: {props.speech.enabled ? 'enabled' : 'off'}
          {props.speech.tts?.available ? ` · TTS ${props.speech.tts.engine}` : ' · no TTS engine'}
          {props.speech.stt?.available ? ` · STT ${props.speech.stt.engine}` : ' · speech input unavailable'}
        </p>
      ) : null}
    </section>
  );
}
