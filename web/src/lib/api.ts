export class ApiError extends Error {
  field: string;

  constructor(message: string, field = '') {
    super(message);
    this.field = field;
  }
}

function root(): HTMLElement {
  return document.getElementById('mtc-app') as HTMLElement;
}

function csrf(): string {
  const w = window as unknown as { csrf_token?: string };
  return w.csrf_token || root().dataset.csrf || '';
}

export async function api<T = Record<string, any>>(action: string, params: Record<string, unknown> = {}): Promise<T> {
  const body = new URLSearchParams();
  body.append('action', action);
  body.append('csrf_token', csrf());
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null) continue;
    body.append(k, typeof v === 'object' ? JSON.stringify(v) : String(v));
  }
  const res = await fetch(root().dataset.api || '', { method: 'POST', body, credentials: 'same-origin' });
  const text = await res.text();
  let data: any;
  try {
    data = JSON.parse(text);
  } catch {
    throw new ApiError(
      text === ''
        ? `The server sent an empty answer (HTTP ${res.status}). Reload the page - the security token may have expired.`
        : `The server sent something unexpected (HTTP ${res.status}).`
    );
  }
  if (!data.ok) throw new ApiError(data.error || 'Something went wrong.', data.field || '');
  return data as T;
}
