import type { Guidance, StepView, VerificationView } from '../types';

interface Props {
  guidance: Guidance;
  step: StepView | null;
  stepNumber: number;
  totalSteps: number;
  verification: VerificationView | null;
  teachMode: boolean;
  busy: string | null;
  can: Record<string, boolean>;
  allowPaste: boolean;
  onPaste: (value: string) => void;
  overlayMode: string;
  showDetails: boolean;
  onToggleDetails: () => void;
  onCopy: (value: string) => void;
  onAction: (action: string, payload?: unknown) => void;
  onExplain: () => void;
}

const OUTCOME_LABEL: Record<string, string> = {
  VERIFIED: 'Verified on screen',
  FAILED: 'Could not confirm',
  INCONCLUSIVE: 'Not yet confirmed',
};

const SOURCE_LABEL: Record<string, string> = {
  user_provided: 'you provided this',
  project_config: 'from your project files',
  derived: 'derived — please check',
  documentation: 'from documentation',
  model_assumption: 'model assumption — not used',
  unknown: 'unknown',
};

export function InstructionCard(props: Props): JSX.Element {
  const { guidance, step, verification } = props;
  const highlight = guidance.highlight;
  const text = guidance.text_to_enter;
  const warnings = guidance.warnings ?? [];
  const risk = step?.risk ?? 'none';

  return (
    <section className="instruction-card" aria-label="Current step">
      <header className="card-head">
        <div>
          <span className="eyebrow">Current step</span>
          <h2>
            {props.totalSteps ? `Step ${Math.min(props.stepNumber, props.totalSteps)} of ${props.totalSteps}` : 'Step'}
            {step ? <span className="step-title"> · {step.title}</span> : null}
          </h2>
        </div>
        <div className="badges">
          <span className={`badge action-${guidance.action_type}`}>{guidance.action_type}</span>
          {risk !== 'none' ? <span className={`badge risk-${risk}`}>{risk}</span> : null}
          <span className={`badge confidence ${guidance.confidence >= 0.8 ? 'high' : guidance.confidence >= 0.6 ? 'mid' : 'low'}`}>
            {Math.round(guidance.confidence * 100)}% confidence
          </span>
        </div>
      </header>

      <p className="instruction">{guidance.instruction}</p>

      <div className="card-block target">
        <span className="label">Highlighted target</span>
        {highlight ? (
          <>
            <p className="value">
              <strong>{highlight.label}</strong>
              <span className="muted small">
                {' '}
                · {highlight.source} · {highlight.rect[0]},{highlight.rect[1]} {highlight.rect[2]}×{highlight.rect[3]} px
              </span>
            </p>
            <p className="muted small">
              {props.overlayMode === 'in-window'
                ? 'This session cannot host a click-through overlay: follow the coordinates in the assistant window.'
                : 'A glowing outline marks this control on your screen.'}
            </p>
            {guidance.alternatives?.length ? (
              <p className="muted small">
                {guidance.alternatives.length} other candidate(s) were considered; the best match is highlighted.
              </p>
            ) : null}
          </>
        ) : (
          <p className="value warn-text">
            No confident highlight. GuidePilot will not draw a highlight on a guess — use “Look again” or tell me the exact
            control name.
          </p>
        )}
      </div>

      {text ? (
        <div className={text.sensitive ? 'card-block exact sensitive' : 'card-block exact'}>
          <span className="label">Exact text</span>
          {text.sensitive ? (
            <p className="value warn-text">
              Sensitive field: GuidePilot will not display, copy, store or send this value. Enter it yourself.
            </p>
          ) : (
            <>
              <div className="exact-row">
                <code>{text.value || '—'}</code>
                <button
                  type="button"
                  className="secondary"
                  onClick={() => props.onCopy(text.value)}
                  disabled={!text.value || !text.copyable}
                  aria-label="Copy exact text"
                >
                  Copy
                </button>
                {props.allowPaste ? (
                  <button
                    type="button"
                    className="secondary"
                    onClick={() => props.onPaste(text.value)}
                    title="Assisted paste asks for approval first and never handles sensitive fields"
                  >
                    Paste…
                  </button>
                ) : null}
              </div>
              <p className="muted small">
                Source: {SOURCE_LABEL[text.source] ?? text.source}
                {text.field_label ? ` · field: ${text.field_label}` : ''}
              </p>
              {text.explanation ? <p className="muted small">{text.explanation}</p> : null}
            </>
          )}
        </div>
      ) : null}

      <div className="card-block expected">
        <span className="label">Expected result</span>
        <p className="value">{guidance.expected_result || '—'}</p>
        <span className="label">Verification</span>
        <p className={`value verification ${verification?.outcome?.toLowerCase() ?? 'pending'}`}>
          {verification ? OUTCOME_LABEL[verification.outcome] ?? verification.outcome : 'Not checked yet'}
          {verification?.summary ? <span className="muted small"> — {verification.summary}</span> : null}
        </p>
        {props.showDetails && verification?.evidence?.length ? (
          <ul className="evidence">
            {verification.evidence.map((item, index) => (
              <li key={`${item.rule}-${index}`} className={item.passed === true ? 'pass' : item.passed === false ? 'fail' : 'unknown'}>
                <strong>{item.rule}</strong>: {item.detail}
              </li>
            ))}
          </ul>
        ) : null}
      </div>

      {warnings.length ? (
        <div className="card-block warnings">
          <span className="label">Watch out</span>
          <ul>
            {warnings.map((warning, index) => (
              <li key={index}>{warning}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {props.teachMode || props.showDetails ? (
        <div className="card-block why">
          <span className="label">Why this step</span>
          <p className="value">{guidance.why || 'This step moves the task forward; it has no explanation recorded.'}</p>
          {guidance.notes?.length ? (
            <ul className="muted small">
              {guidance.notes.map((note, index) => (
                <li key={index}>{note}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}

      <div className="card-actions">
        <button type="button" className="secondary" onClick={props.onExplain}>
          Explain
        </button>
        <button type="button" className="secondary" onClick={() => props.onAction('look')}>
          Show me again
        </button>
        <button type="button" className="link-button" onClick={props.onToggleDetails} aria-expanded={props.showDetails}>
          {props.showDetails ? 'Hide details' : 'Show details'}
        </button>
      </div>
    </section>
  );
}
