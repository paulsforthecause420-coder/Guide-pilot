import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import type { Notice } from '../types';

interface Props {
  onUseWorkflow: (workflowId: string, values?: Record<string, string>) => void;
  onResumeTask: (taskId: string) => void;
  onNotice: (notice: Notice) => void;
  onError: (thrown: unknown) => void;
}

export function LibraryPanel(props: Props): JSX.Element {
  const [tasks, setTasks] = useState<any[]>([]);
  const [workflows, setWorkflows] = useState<any[]>([]);
  const [importText, setImportText] = useState('');
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(async () => {
    try {
      const [taskData, workflowData] = await Promise.all([api.listTasks(), api.workflows()]);
      setTasks(taskData?.tasks ?? taskData ?? []);
      setWorkflows(workflowData?.workflows ?? workflowData ?? []);
    } catch (thrown) {
      props.onError(thrown);
    } finally {
      setLoaded(true);
    }
  }, [props]);

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="library">
      <section className="panel">
        <header className="row between">
          <h2>Task history</h2>
          <button type="button" className="link-button" onClick={() => void load()}>
            Refresh
          </button>
        </header>
        {!loaded ? <p className="muted small">Loading…</p> : null}
        {loaded && tasks.length === 0 ? <p className="muted small">No tasks yet. Start one from the Guide tab.</p> : null}
        <ul className="list">
          {tasks.map((task: any) => (
            <li key={task.id} className="list-item">
              <div>
                <p className="value">{task.objective}</p>
                <p className="muted small">
                  {task.state?.replace(/_/g, ' ').toLowerCase()} · step {Number(task.current_index ?? 0) + 1} of{' '}
                  {task.step_count ?? task.steps?.length ?? 0} · {task.last_verified_step !== undefined ? `${task.last_verified_step + 1} verified` : ''}
                  {task.updated_at ? ` · ${new Date(task.updated_at).toLocaleString()}` : ''}
                </p>
              </div>
              <div className="row gap">
                <button type="button" className="secondary" onClick={() => props.onResumeTask(task.id)}>
                  Resume
                </button>
                <button
                  type="button"
                  className="link-button"
                  onClick={async () => {
                    if (!window.confirm('Delete this task from history?')) return;
                    try {
                      await api.deleteTask(task.id);
                      await load();
                      props.onNotice({ message: 'Task deleted.', level: 'info', at: Date.now() });
                    } catch (thrown) {
                      props.onError(thrown);
                    }
                  }}
                >
                  Delete
                </button>
              </div>
            </li>
          ))}
        </ul>
      </section>

      <section className="panel">
        <header className="row between">
          <h2>Reusable workflows</h2>
          <button
            type="button"
            className="link-button"
            onClick={async () => {
              if (!window.confirm('Delete all task history? Workflows and settings are kept.')) return;
              try {
                await api.clearTasks();
                await load();
                props.onNotice({ message: 'Task history cleared.', level: 'info', at: Date.now() });
              } catch (thrown) {
                props.onError(thrown);
              }
            }}
          >
            Clear history
          </button>
        </header>
        {loaded && workflows.length === 0 ? (
          <p className="muted small">Complete a task and choose “Save as reusable workflow” to build your library.</p>
        ) : null}
        <ul className="list">
          {workflows.map((workflow: any) => (
            <li key={workflow.id} className="list-item">
              <div>
                <p className="value">{workflow.name}</p>
                <p className="muted small">
                  {workflow.step_count ?? workflow.steps?.length ?? 0} steps · {workflow.source ?? 'recorded'}
                  {workflow.created_at ? ` · ${new Date(workflow.created_at).toLocaleDateString()}` : ''}
                </p>
                {workflow.objective ? <p className="muted small">{workflow.objective}</p> : null}
              </div>
              <div className="row gap">
                <button type="button" className="primary" onClick={() => props.onUseWorkflow(workflow.id)}>
                  Use
                </button>
                <button
                  type="button"
                  className="secondary"
                  onClick={async () => {
                    try {
                      const data = await api.exportWorkflow(workflow.id);
                      const blob = new Blob([JSON.stringify(data?.workflow ?? data, null, 2)], { type: 'application/json' });
                      const url = URL.createObjectURL(blob);
                      const anchor = document.createElement('a');
                      anchor.href = url;
                      anchor.download = `${workflow.name.replace(/[^\w.-]+/g, '-').toLowerCase() || 'workflow'}.json`;
                      anchor.click();
                      URL.revokeObjectURL(url);
                    } catch (thrown) {
                      props.onError(thrown);
                    }
                  }}
                >
                  Export
                </button>
                <button
                  type="button"
                  className="link-button"
                  onClick={async () => {
                    if (!window.confirm(`Delete the workflow “${workflow.name}”?`)) return;
                    try {
                      await api.deleteWorkflow(workflow.id);
                      await load();
                    } catch (thrown) {
                      props.onError(thrown);
                    }
                  }}
                >
                  Delete
                </button>
              </div>
            </li>
          ))}
        </ul>

        <details className="import">
          <summary>Import a workflow (JSON)</summary>
          <p className="muted small">
            Imported workflows are schema-validated and checked against the current screen before reuse; credentials are
            never stored in a workflow.
          </p>
          <textarea rows={6} value={importText} onChange={(event) => setImportText(event.target.value)} placeholder='{"name": "…", "steps": []}' />
          <button
            type="button"
            className="secondary"
            onClick={async () => {
              try {
                const parsed = JSON.parse(importText);
                await api.importWorkflow(parsed?.workflow ?? parsed);
                setImportText('');
                await load();
                props.onNotice({ message: 'Workflow imported.', level: 'info', at: Date.now() });
              } catch (thrown) {
                props.onError(thrown instanceof SyntaxError ? new Error('That is not valid JSON.') : thrown);
              }
            }}
          >
            Import
          </button>
        </details>
      </section>
    </div>
  );
}
