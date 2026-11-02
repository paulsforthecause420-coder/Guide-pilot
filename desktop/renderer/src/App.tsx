import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, api } from './api';
import { ChatPanel } from './components/ChatPanel';
import { InstructionCard } from './components/InstructionCard';
import { NoticeStack, ProgressStrip, ShareBanner, StateChip, TitleBar } from './components/Chrome';
import { ObjectiveForm } from './components/ObjectiveForm';
import { LibraryPanel } from './panels/LibraryPanel';
import { PrivacyPanel } from './panels/PrivacyPanel';
import { SettingsPanel } from './panels/SettingsPanel';
import type { ChatEntry, Notice, TaskStatePayload } from './types';

type Tab = 'guide' | 'library' | 'settings' | 'privacy';

const WATCHABLE = new Set(['READY_FOR_USER', 'WAITING_FOR_ACTION', 'VERIFYING']);

export default function App(): JSX.Element {
  const [tab, setTab] = useState<Tab>('guide');
  const [caps, setCaps] = useState<any>(null);
  const [settings, setSettings] = useState<any>(null);
  const [state, setState] = useState<TaskStatePayload | null>(null);
  const [notices, setNotices] = useState<Notice[]>([]);
  const [objective, setObjective] = useState('');
  const [contextValues, setContextValues] = useState<Record<string, string>>({});
  const [chat, setChat] = useState<ChatEntry[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [compact, setCompact] = useState(false);
  const [resumePrompt, setResumePrompt] = useState<any>(null);
  const [resumeDismissed, setResumeDismissed] = useState(false);
  const [watchEnabled, setWatchEnabled] = useState(true);
  const [teachMode, setTeachMode] = useState(false);
  const [speech, setSpeech] = useState<any>(null);
  const [workflowName, setWorkflowName] = useState('');
  const [saveWorkflowOpen, setSaveWorkflowOpen] = useState(false);
  const [pendingField, setPendingField] = useState<string>('');
  const [pendingValue, setPendingValue] = useState('');
  const [showDetails, setShowDetails] = useState(false);

  const busyRef = useRef<string | null>(null);
  const stateRef = useRef<TaskStatePayload | null>(null);
  stateRef.current = state;

  const theme = settings?.ui?.theme ?? 'dark';
  const shareEnabled = Boolean(settings?.privacy?.share_enabled) && !caps?.stopped;
  const task = state?.task ?? null;
  const guidance = state?.guidance ?? null;
  const can = state?.can ?? {};

  const pushNotice = useCallback((notice: Notice) => {
    setNotices((current) => [...current.slice(-3), notice]);
    window.setTimeout(() => {
      setNotices((current) => current.filter((item) => item.at !== notice.at || item.message !== notice.message));
    }, 9000);
  }, []);

  const handleError = useCallback(
    (thrown: unknown, prefix?: string) => {
      const message =
        thrown instanceof ApiError
          ? `${prefix ? `${prefix}: ` : ''}${thrown.message}`
          : thrown instanceof Error
            ? thrown.message
            : String(thrown);
      setError(message);
      pushNotice({ message, level: 'error', at: Date.now() });
    },
    [pushNotice],
  );

  const refreshCapabilities = useCallback(async () => {
    try {
      const data = await api.capabilities();
      setCaps(data);
      setSettings((current: any) => (data.privacy ? { ...(current ?? {}), privacy: { ...(current?.privacy ?? {}), ...data.privacy } } : current));
    } catch (thrown) {
      handleError(thrown, 'Could not read capabilities');
    }
  }, [handleError]);

  const loadSettings = useCallback(async () => {
    try {
      const data = await api.settings();
      setSettings(data);
      setTeachMode(Boolean(data?.ui?.teach_me_mode));
    } catch (thrown) {
      handleError(thrown, 'Could not load settings');
    }
  }, [handleError]);

  const applyState = useCallback(
    (payload: TaskStatePayload | null) => {
      if (!payload) return;
      setState(payload);
      if (payload.warnings?.length) {
        for (const warning of payload.warnings) {
          pushNotice({ message: warning, level: 'warning', at: Date.now() });
        }
      }
      const current = payload.task?.current_step;
      if (current?.requires_user_input && !pendingValue) {
        setPendingField(String(current.target_description || current.title || ''));
      }
    },
    [pendingValue, pushNotice],
  );

  useEffect(() => {
    void (async () => {
      await loadSettings();
      await refreshCapabilities();
      try {
        applyState(await api.taskState());
        setResumePrompt((await api.resumePrompt())?.prompt ?? null);
        setSpeech(await api.speechStatus());
      } catch (thrown) {
        handleError(thrown, 'Startup');
      }
    })();
  }, [loadSettings, refreshCapabilities, applyState, handleError]);

  useEffect(() => {
    const offState = window.guidepilot.events.onTaskState((payload) => applyState(payload));
    const offNotice = window.guidepilot.events.onNotice((notice) => pushNotice(notice as Notice));
    const offShortcut = window.guidepilot.events.onShortcut((payload) => {
      if (payload?.action === 'stop') {
        pushNotice({ message: 'Emergency stop pressed.', level: 'warning', at: Date.now() });
        void refreshCapabilities();
      }
      if (payload?.action === 'repeat') void handleRepeat();
    });
    return () => {
      offState();
      offNotice();
      offShortcut();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [applyState, pushNotice, refreshCapabilities]);

  const run = useCallback(
    async <T,>(label: string, operation: () => Promise<T>): Promise<T | null> => {
      if (busyRef.current) return null;
      busyRef.current = label;
      setBusy(label);
      setError(null);
      try {
        return await operation();
      } catch (thrown) {
        handleError(thrown, label);
        return null;
      } finally {
        busyRef.current = null;
        setBusy(null);
      }
    },
    [handleError],
  );

  const startTask = useCallback(async () => {
    if (!objective.trim()) {
      setError('Describe what you would like help doing first.');
      return;
    }
    if (!shareEnabled) {
      setError('Enable screen sharing so GuidePilot can see the screen it is guiding you on.');
      return;
    }
    const result = await run('Starting guidance', () => api.createTask({ objective: objective.trim(), contextValues }));
    if (result) {
      applyState(result);
      setChat((current) => [
        ...current,
        { role: 'assistant', text: `Plan ready: ${result?.task?.summary ?? ''}`, source: 'plan' },
      ]);
      setResumeDismissed(true);
    }
  }, [objective, contextValues, shareEnabled, run, applyState]);

  const doAction = useCallback(
    async (action: string, payload?: unknown, label?: string) => {
      const result = await run(label ?? action, () => api.action(action, payload));
      if (result) applyState(result);
      return result;
    },
    [run, applyState],
  );

  const handleRepeat = useCallback(async () => {
    const result = await run('Looking again', () => api.action('look'));
    if (result) applyState(result);
  }, [run, applyState]);

  const pasteApproved = useCallback(
    async (value: string) => {
      const confirmed = window.confirm(
        `Paste this value into the highlighted field?\n\n${value}\n\nOnly approve if the highlighted field is the right one.`,
      );
      if (!confirmed) return;
      const result = await run('Pasting', () => api.paste(value, true));
      if (result) {
        pushNotice({ message: 'Value pasted into the focused field.', level: 'info', at: Date.now() });
        await handleRepeat();
      }
    },
    [run, pushNotice, handleRepeat],
  );

  const sendChat = useCallback(
    async (message: string) => {
      if (!message.trim()) return;
      setChat((current) => [...current, { role: 'user', text: message }]);
      const result = await run('Thinking', () => api.chat(message));
      if (result) {
        setChat((current) => [
          ...current,
          { role: 'assistant', text: result.reply, source: result.source, warnings: result.warnings },
        ]);
        if (result.task) applyState(result.task);
      }
    },
    [run, applyState],
  );

  const copyText = useCallback(
    async (value: string) => {
      try {
        await navigator.clipboard.writeText(value);
        pushNotice({ message: 'Copied to the clipboard.', level: 'info', at: Date.now() });
        return;
      } catch {
        /* fall back below */
      }
      try {
        const area = document.createElement('textarea');
        area.value = value;
        area.setAttribute('readonly', 'true');
        area.style.position = 'fixed';
        area.style.opacity = '0';
        document.body.appendChild(area);
        area.select();
        const copied = document.execCommand('copy');
        document.body.removeChild(area);
        pushNotice({
          message: copied ? 'Copied to the clipboard.' : 'Could not copy automatically — select the text and copy it.',
          level: copied ? 'info' : 'warning',
          at: Date.now(),
        });
      } catch {
        pushNotice({ message: 'Clipboard is unavailable; select the value and copy it manually.', level: 'warning', at: Date.now() });
      }
    },
    [pushNotice],
  );

  const toggleSharing = useCallback(async () => {
    if (shareEnabled) {
      const result = await run('Stopping capture', () => api.emergencyStop());
      if (result) {
        await refreshCapabilities();
        await loadSettings();
        setState(null);
        pushNotice({ message: 'Screen sharing stopped. Nothing is being captured.', level: 'warning', at: Date.now() });
      }
    } else {
      const result = await run('Enabling screen sharing', () => api.enableSharing(true));
      if (result) {
        await refreshCapabilities();
        await loadSettings();
        pushNotice({ message: 'Screen sharing enabled. A sharing indicator is shown in the header.', level: 'info', at: Date.now() });
      }
    }
  }, [shareEnabled, run, refreshCapabilities, loadSettings, pushNotice]);

  const saveWorkflow = useCallback(async () => {
    const result = await run('Saving workflow', () => api.action('saveWorkflow', { name: workflowName }));
    if (result) {
      setSaveWorkflowOpen(false);
      setWorkflowName('');
      pushNotice({ message: 'Workflow saved to your local library.', level: 'info', at: Date.now() });
    }
  }, [run, workflowName, pushNotice]);

  const submitContextValue = useCallback(async () => {
    if (!pendingField.trim() || !pendingValue.trim()) return;
    const values = { ...contextValues, [pendingField.trim()]: pendingValue.trim() };
    setContextValues(values);
    setPendingValue('');
    const result = await run('Updating the plan', () => api.setContext(values));
    if (result) applyState(result);
  }, [pendingField, pendingValue, contextValues, run, applyState]);

  const applySettings = useCallback(
    async (patch: any) => {
      const updated = await run('Saving settings', () => api.updateSettings(patch));
      if (updated) {
        setSettings(updated);
        await refreshCapabilities();
        if (patch?.ui?.theme) document.documentElement.dataset.theme = patch.ui.theme;
        if (patch?.ui?.teach_me_mode !== undefined) setTeachMode(Boolean(patch.ui.teach_me_mode));
      }
    },
    [run, refreshCapabilities],
  );

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  useEffect(() => {
    if (!watchEnabled || !shareEnabled || !task || !WATCHABLE.has(task.state)) return;
    const cadence = Math.max(1200, Number(settings?.observation?.cadence_ms ?? 2500));
    const interval = window.setInterval(() => {
      if (busyRef.current) return;
      const current = stateRef.current;
      if (!current?.task || !WATCHABLE.has(current.task.state)) return;
      if (!stateRef.current?.guidance) return;
      busyRef.current = 'watch';
      api
        .action('watch', { autoVerify: true })
        .then((payload) => {
          if (payload) applyState(payload);
        })
        .catch(() => {
          /* transient capture errors are surfaced by explicit actions */
        })
        .finally(() => {
          busyRef.current = null;
        });
    }, cadence);
    return () => window.clearInterval(interval);
  }, [watchEnabled, shareEnabled, task?.id, task?.state, settings?.observation?.cadence_ms, applyState]);

  const overlayMode = state?.overlay_mode ?? caps?.display?.overlay_backend ?? 'in-window';
  const stepNumber = (task?.current_index ?? 0) + 1;
  const totalSteps = task?.steps?.length ?? 0;
  const captureBlockedReason = useMemo(() => {
    if (caps?.stopped) return 'Emergency stop is active. Enable screen sharing to continue.';
    if (!shareEnabled) return 'Screen sharing is off. GuidePilot cannot see or guide until you enable it.';
    if (caps && !caps.capture?.available) return `Capture backend unavailable: ${caps.capture?.reason ?? 'unknown reason'}`;
    return null;
  }, [caps, shareEnabled]);

  return (
    <div className={`app ${compact ? 'is-compact' : ''}`}>
      <TitleBar
        shareEnabled={shareEnabled}
        sessionType={caps?.display?.session_type ?? 'unknown'}
        busy={busy}
        compact={compact}
        onToggleSharing={() => void toggleSharing()}
        onToggleCompact={() => {
          const next = !compact;
          setCompact(next);
          void api.collapse(next);
        }}
        onMinimize={() => void api.minimize()}
        onClose={() => void api.close()}
        onSettings={() => setTab('settings')}
      />

      <NoticeStack notices={notices} onDismiss={(notice) => setNotices((current) => current.filter((item) => item !== notice))} />

      {error ? (
        <div className="inline-error" role="alert">
          {error}
          <button type="button" className="link-button" onClick={() => setError(null)}>
            Dismiss
          </button>
        </div>
      ) : null}

      <nav className="tabs" aria-label="GuidePilot sections">
        {(
          [
            ['guide', 'Guide'],
            ['library', 'Library'],
            ['settings', 'Settings'],
            ['privacy', 'Privacy'],
          ] as Array<[Tab, string]>
        ).map(([key, label]) => (
          <button
            key={key}
            type="button"
            className={tab === key ? 'tab is-active' : 'tab'}
            onClick={() => setTab(key)}
            aria-current={tab === key ? 'page' : undefined}
          >
            {label}
          </button>
        ))}
      </nav>

      <main className="content">
        {tab === 'guide' ? (
          <>
            <ShareBanner reason={captureBlockedReason} shareEnabled={shareEnabled} onEnable={() => void toggleSharing()} />

            {resumePrompt && !resumeDismissed && !task ? (
              <div className="resume-banner" role="status">
                <div>
                  <strong>Resume your previous task?</strong>
                  <p>{resumePrompt.objective}</p>
                  <p className="muted small">
                    State: {resumePrompt.state}
                    {resumePrompt.current_step ? ` · ${resumePrompt.current_step}` : ''}
                  </p>
                </div>
                <div className="row gap">
                  <button
                    type="button"
                    className="primary"
                    onClick={() =>
                      void run('Resuming task', () => api.resumeTask(resumePrompt.task_id)).then((result) => {
                        if (result) applyState(result);
                        setResumeDismissed(true);
                      })
                    }
                  >
                    Resume
                  </button>
                  <button type="button" className="secondary" onClick={() => setResumeDismissed(true)}>
                    Not now
                  </button>
                </div>
              </div>
            ) : null}

            {!task ? (
              <ObjectiveForm
                objective={objective}
                onChange={setObjective}
                onSubmit={() => void startTask()}
                busy={busy === 'Starting guidance'}
                contextValues={contextValues}
                onContextChange={setContextValues}
                observation={state?.observation ?? null}
                disabled={!shareEnabled}
              />
            ) : (
              <>
                <div className="task-head">
                  <div>
                    <StateChip state={task.state} />
                    <p className="objective">{task.objective}</p>
                    <p className="muted small">
                      {task.app_context ? `${task.app_context} · ` : ''}
                      {totalSteps ? `step ${Math.min(stepNumber, totalSteps)} of ${totalSteps}` : 'no steps'}
                      {overlayMode === 'in-window' ? ' · overlay unavailable on this session' : ` · ${overlayMode}`}
                    </p>
                  </div>
                  <label className="switch small">
                    <input type="checkbox" checked={watchEnabled} onChange={(event) => setWatchEnabled(event.target.checked)} />
                    <span>Auto-check</span>
                  </label>
                </div>

                <ProgressStrip completed={task.progress?.completed ?? 0} total={task.progress?.total ?? 0} />

                {task.state === 'COMPLETED' ? (
                  <div className="completion" role="status">
                    <h2>Task complete</h2>
                    <p>{task.summary || 'All steps were verified on screen.'}</p>
                    {!saveWorkflowOpen ? (
                      <div className="row gap wrap">
                        <button type="button" className="primary" onClick={() => setSaveWorkflowOpen(true)}>
                          Save as reusable workflow
                        </button>
                        <button
                          type="button"
                          className="secondary"
                          onClick={() => {
                            setState(null);
                            setObjective('');
                            setContextValues({});
                            setChat([]);
                          }}
                        >
                          Start another task
                        </button>
                      </div>
                    ) : (
                      <div className="row gap">
                        <input
                          aria-label="Workflow name"
                          value={workflowName}
                          onChange={(event) => setWorkflowName(event.target.value)}
                          placeholder="Workflow name"
                        />
                        <button type="button" className="primary" onClick={() => void saveWorkflow()}>
                          Save
                        </button>
                        <button type="button" className="secondary" onClick={() => setSaveWorkflowOpen(false)}>
                          Cancel
                        </button>
                      </div>
                    )}
                  </div>
                ) : null}

                {guidance && task.state !== 'COMPLETED' ? (
                  <InstructionCard
                    guidance={guidance}
                    step={task.current_step}
                    stepNumber={stepNumber}
                    totalSteps={totalSteps}
                    verification={state?.verification ?? null}
                    teachMode={teachMode}
                    busy={busy}
                    can={can}
                    allowPaste={Boolean(settings?.automation?.assisted_actions_enabled && settings?.automation?.allow_paste)}
                    onPaste={(value) => void pasteApproved(value)}
                    onCopy={(value) => void copyText(value)}
                    onAction={(action, payload) => void doAction(action, payload)}
                    onExplain={() => void sendChat('Explain this step. Why is it necessary?')}
                    showDetails={showDetails}
                    onToggleDetails={() => setShowDetails((value) => !value)}
                    overlayMode={overlayMode}
                  />
                ) : null}

                {task.state === 'COMPLETED' ? null : (
                  <section className="controls" aria-label="Step controls">
                    <button
                      type="button"
                      className="secondary"
                      onClick={() => void doAction('previous', undefined, 'Going back')}
                      disabled={!can.can_previous || Boolean(busy)}
                    >
                      Previous
                    </button>
                    <button
                      type="button"
                      className="secondary"
                      onClick={() => void handleRepeat()}
                      disabled={Boolean(busy)}
                    >
                      Look again
                    </button>
                    <button
                      type="button"
                      className="primary"
                      onClick={() => void doAction('verify', undefined, 'Verifying')}
                      disabled={Boolean(busy) || !task.current_step}
                    >
                      Verify step
                    </button>
                    <button
                      type="button"
                      className="secondary"
                      onClick={() => void doAction('confirm', undefined, 'Confirming')}
                      disabled={Boolean(busy) || !task.current_step}
                    >
                      I did it
                    </button>
                    <button
                      type="button"
                      className="secondary"
                      onClick={() => {
                        if (window.confirm('Skip this step without verification? It will be recorded as skipped.')) {
                          void doAction('skip', { reason: 'Skipped by user' }, 'Skipping');
                        }
                      }}
                      disabled={Boolean(busy) || !task.current_step}
                    >
                      Skip
                    </button>
                    <button
                      type="button"
                      className="secondary"
                      onClick={() => void doAction(task.state === 'PAUSED' ? 'resume' : 'pause', undefined, 'Updating task')}
                      disabled={Boolean(busy)}
                    >
                      {task.state === 'PAUSED' ? 'Resume' : 'Pause'}
                    </button>
                    <button
                      type="button"
                      className="danger"
                      onClick={() => {
                        if (window.confirm('Cancel this task? Progress stays in history.')) {
                          void doAction('cancel', undefined, 'Cancelling');
                        }
                      }}
                      disabled={Boolean(busy)}
                    >
                      Stop task
                    </button>
                  </section>
                )}

                {task.current_step?.requires_user_input || state?.guidance?.text_to_enter === null ? (
                  <section className="context-card" aria-label="Provide missing information">
                    <h3>Needs your input</h3>
                    <p className="muted small">
                      I will not invent this value. Enter it here and I will update the instruction and highlight.
                    </p>
                    <div className="row gap wrap">
                      <input
                        aria-label="Field name"
                        value={pendingField}
                        onChange={(event) => setPendingField(event.target.value)}
                        placeholder="Field"
                      />
                      <input
                        aria-label="Value"
                        value={pendingValue}
                        onChange={(event) => setPendingValue(event.target.value)}
                        placeholder="Value"
                      />
                      <button type="button" className="primary" onClick={() => void submitContextValue()}>
                        Use this value
                      </button>
                    </div>
                  </section>
                ) : null}
              </>
            )}

            <ChatPanel
              entries={chat}
              onSend={(message) => void sendChat(message)}
              busy={busy === 'Thinking'}
              onRepeat={() => void handleRepeat()}
              speech={speech}
              onSpeak={(text) => void api.speak(text)}
            />
          </>
        ) : null}

        {tab === 'library' ? (
          <LibraryPanel
            onUseWorkflow={(workflowId, values) => {
              void run('Starting workflow', () => api.startWorkflow(workflowId, values)).then((result) => {
                if (result) {
                  applyState(result);
                  setTab('guide');
                }
              });
            }}
            onResumeTask={(taskId) => {
              void run('Resuming task', () => api.resumeTask(taskId)).then((result) => {
                if (result) {
                  applyState(result);
                  setTab('guide');
                }
              });
            }}
            onNotice={pushNotice}
            onError={(thrown) => handleError(thrown)}
          />
        ) : null}

        {tab === 'settings' ? (
          <SettingsPanel
            settings={settings}
            caps={caps}
            speech={speech}
            busy={busy}
            onApply={(patch) => void applySettings(patch)}
            onNotice={pushNotice}
            onError={(thrown) => handleError(thrown)}
            onRefresh={async () => {
              await refreshCapabilities();
              await loadSettings();
            }}
          />
        ) : null}

        {tab === 'privacy' ? (
          <PrivacyPanel
            caps={caps}
            settings={settings}
            onEnableSharing={() => void toggleSharing()}
            onEmergencyStop={() =>
              void run('Emergency stop', () => api.emergencyStop()).then(() => {
                pushNotice({ message: 'Emergency stop: capture and guidance halted.', level: 'warning', at: Date.now() });
                void refreshCapabilities();
                void loadSettings();
                setState(null);
              })
            }
            onPurge={() =>
              void run('Clearing local data', () => api.purge()).then(() => {
                pushNotice({ message: 'Local tasks, workflows and audit events cleared.', level: 'info', at: Date.now() });
                setState(null);
                setChat([]);
              })
            }
            onError={(thrown) => handleError(thrown)}
            onApplySettings={(patch) => void applySettings(patch)}
          />
        ) : null}
      </main>

      <footer className="footer">
        <span className={`dot ${shareEnabled ? 'live' : 'off'}`} aria-hidden="true" />
        {shareEnabled ? 'Screen observation active' : 'Screen observation stopped'}
        <span className="spacer" />
        <span title="No screenshots are written to disk by default">No screenshots stored</span>
      </footer>
    </div>
  );
}
