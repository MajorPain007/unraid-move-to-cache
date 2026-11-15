import { api } from './api';

export const app = $state<{ ov: any; error: string }>({ ov: null, error: '' });

let timer: ReturnType<typeof setTimeout> | undefined;

export function busy(ov: any): boolean {
  const st = ov?.status;
  return !!ov?.move?.running || !!st?.copying || (st?.streams?.length ?? 0) > 0 || (st?.queue?.length ?? 0) > 0;
}

export async function refresh(): Promise<void> {
  clearTimeout(timer);
  try {
    app.ov = await api('overview');
    app.error = '';
  } catch (e) {
    app.error = (e as Error).message;
  }
  timer = setTimeout(refresh, document.hidden ? 30000 : busy(app.ov) ? 3000 : 10000);
}
