/**
 * Backend supervisor and HTTP client for the local GuidePilot service.
 *
 * The renderer never sees the service token and never makes HTTP calls itself:
 * it talks to this main process over IPC, and every request is made here with a
 * per-run bearer token read from a 0600 file written by the Python service.
 */
import { spawn, ChildProcessWithoutNullStreams } from 'node:child_process';
import { readFileSync, existsSync } from 'node:fs';
import * as http from 'node:http';
import * as path from 'node:path';

export interface Handshake {
  app: string;
  version: string;
  event: string;
  host: string;
  port: number;
  pid: number;
  token_path: string;
  session_type: string;
  overlay_supported: boolean;
}

export interface ApiErrorShape {
  code: string;
  message: string;
  detail?: unknown;
}

export class BackendError extends Error {
  code: string;
  detail?: unknown;
  constructor(error: ApiErrorShape) {
    super(error.message);
    this.code = error.code;
    this.detail = error.detail;
    this.name = 'BackendError';
  }
}

export interface BackendOptions {
  appRoot: string;
  backendDir: string;
  logger?: (line: string) => void;
}

export class BackendClient {
  private child: ChildProcessWithoutNullStreams | null = null;
  private handshake: Handshake | null = null;
  private token = '';
  private readonly options: BackendOptions;
  private lastStderr: string[] = [];
  private stopping = false;

  constructor(options: BackendOptions) {
    this.options = options;
  }

  get info(): Handshake | null {
    return this.handshake;
  }

  get isRunning(): boolean {
    return Boolean(this.child && this.handshake);
  }

  private log(line: string): void {
    (this.options.logger ?? ((value: string) => console.log(value)))(`[backend] ${line}`);
  }

  async start(timeoutMs = 25000): Promise<Handshake> {
    if (this.handshake) return this.handshake;
    const python = process.env.GUIDEPILOT_PYTHON || 'python3';
    const args = ['-m', 'guidepilot.serve', '--port', '0', '--print-ready'];
    const env = {
      ...process.env,
      PYTHONPATH: this.options.backendDir,
      PYTHONUNBUFFERED: '1',
      GUIDEPILOT_LOG_LEVEL: process.env.GUIDEPILOT_LOG_LEVEL || 'INFO',
    };
    this.log(`starting ${python} ${args.join(' ')}`);
    this.child = spawn(python, args, { env, cwd: this.options.appRoot });
    const handshake = await new Promise<Handshake>((resolve, reject) => {
      const startedAt = Date.now();
      let buffer = '';
      const timer = setInterval(() => {
        if (Date.now() - startedAt > timeoutMs) {
          clearInterval(timer);
          reject(
            new Error(
              `Local service did not become ready in time. ${this.lastStderr.slice(-4).join(' | ') || 'No stderr output.'}`,
            ),
          );
        }
      }, 500);
      const onData = (chunk: Buffer) => {
        buffer += chunk.toString('utf8');
        let newline = buffer.indexOf('\n');
        while (newline >= 0) {
          const line = buffer.slice(0, newline).trim();
          buffer = buffer.slice(newline + 1);
          if (line.startsWith('{')) {
            try {
              const parsed = JSON.parse(line) as Handshake;
              if (parsed.event === 'ready') {
                clearInterval(timer);
                this.child?.stdout.off('data', onData);
                resolve(parsed);
                return;
              }
            } catch {
              /* ignore non-handshake output */
            }
          } else if (line) {
            this.log(line);
          }
          newline = buffer.indexOf('\n');
        }
      };
      this.child?.stdout.on('data', onData);
      this.child?.stderr.on('data', (chunk: Buffer) => {
        const text = chunk.toString('utf8').trimEnd();
        if (!text) return;
        this.lastStderr.push(text);
        if (this.lastStderr.length > 40) this.lastStderr.shift();
        this.log(text);
      });
      this.child?.on('error', (error) => {
        clearInterval(timer);
        reject(new Error(`Could not start the local service: ${error.message}`));
      });
      this.child?.on('exit', (code) => {
        if (!this.stopping && code !== 0) {
          clearInterval(timer);
          reject(
            new Error(
              `Local service exited with code ${code}. ${this.lastStderr.slice(-4).join(' | ') || ''}`.trim(),
            ),
          );
        }
      });
    });

    this.handshake = handshake;
    const tokenPath = handshake.token_path;
    if (!existsSync(tokenPath)) {
      throw new Error(`Service token file is missing at ${tokenPath}`);
    }
    this.token = readFileSync(tokenPath, 'utf8').trim();
    if (!this.token) throw new Error('Service token file is empty');
    this.log(`ready on http://${handshake.host}:${handshake.port} (${handshake.session_type})`);
    return handshake;
  }

  async stop(): Promise<void> {
    this.stopping = true;
    this.handshake = null;
    this.token = '';
    if (!this.child) return;
    const child = this.child;
    this.child = null;
    try {
      child.kill('SIGTERM');
    } catch {
      /* already gone */
    }
    await new Promise<void>((resolve) => {
      const timer = setTimeout(() => {
        try {
          child.kill('SIGKILL');
        } catch {
          /* ignore */
        }
        resolve();
      }, 4000);
      child.once('exit', () => {
        clearTimeout(timer);
        resolve();
      });
    });
  }

  request<T = any>(method: string, endpoint: string, body?: unknown, timeoutMs = 60000): Promise<T> {
    const handshake = this.handshake;
    if (!handshake) return Promise.reject(new BackendError({ code: 'backend_down', message: 'The local service is not running.' }));
    const payload = body === undefined ? null : JSON.stringify(body);
    const headers: Record<string, string> = {
      Authorization: `Bearer ${this.token}`,
      Accept: 'application/json',
    };
    if (payload) {
      headers['Content-Type'] = 'application/json';
      headers['Content-Length'] = Buffer.byteLength(payload).toString();
    }
    return new Promise<T>((resolve, reject) => {
      const request = http.request(
        {
          host: handshake.host,
          port: handshake.port,
          path: endpoint,
          method,
          headers,
          timeout: timeoutMs,
        },
        (response) => {
          const chunks: Buffer[] = [];
          response.on('data', (chunk: Buffer) => chunks.push(chunk));
          response.on('end', () => {
            const text = Buffer.concat(chunks).toString('utf8');
            let parsed: any = null;
            try {
              parsed = text ? JSON.parse(text) : null;
            } catch (error) {
              reject(new BackendError({ code: 'bad_response', message: `Invalid JSON from local service: ${String(error)}` }));
              return;
            }
            if (parsed && parsed.ok === false && parsed.error) {
              reject(new BackendError(parsed.error as ApiErrorShape));
              return;
            }
            resolve((parsed?.data ?? parsed) as T);
          });
        },
      );
      request.on('timeout', () => {
        request.destroy(new Error(`Request to ${endpoint} timed out after ${timeoutMs}ms`));
      });
      request.on('error', (error) => {
        reject(new BackendError({ code: 'network', message: error.message }));
      });
      if (payload) request.write(payload);
      request.end();
    });
  }

  get<T = any>(endpoint: string, timeoutMs?: number): Promise<T> {
    return this.request<T>('GET', endpoint, undefined, timeoutMs);
  }

  post<T = any>(endpoint: string, body: unknown = {}, timeoutMs?: number): Promise<T> {
    return this.request<T>('POST', endpoint, body, timeoutMs);
  }

  put<T = any>(endpoint: string, body: unknown = {}, timeoutMs?: number): Promise<T> {
    return this.request<T>('PUT', endpoint, body, timeoutMs);
  }

  del<T = any>(endpoint: string): Promise<T> {
    return this.request<T>('DELETE', endpoint);
  }
}

export function resolvePaths(isPackaged: boolean, resourcesPath: string, dirname: string): { appRoot: string; backendDir: string; rendererDir: string } {
  if (isPackaged) {
    const appRoot = path.join(resourcesPath, 'guidepilot');
    return {
      appRoot,
      backendDir: path.join(appRoot, 'backend'),
      rendererDir: path.join(appRoot, 'desktop', 'dist', 'renderer'),
    };
  }
  // __dirname is desktop/dist/electron when compiled.
  const appRoot = path.resolve(dirname, '..', '..', '..');
  return {
    appRoot,
    backendDir: path.join(appRoot, 'backend'),
    rendererDir: path.resolve(dirname, '..', 'renderer'),
  };
}
