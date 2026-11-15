<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from './lib/api';
  import { bytes, duration, pct } from './lib/format';

  type Progress = { done: number; size: number; rate?: number; eta?: number };
  let ov = $state<any>(null);
  let error = $state('');
  let busy = $state(false);
  let note = $state('');

  const st = $derived(ov?.status);
  const job = $derived(ov?.move?.job);
  const moving = $derived(!!ov?.move?.running);
  const copying = $derived(st?.copying);
  const active = $derived(moving || !!copying || (st?.streams?.length ?? 0) > 0);
  const servers = $derived(Object.entries((st?.servers ?? {}) as Record<string, any>));

  let timer: ReturnType<typeof setTimeout> | undefined;
  async function load() {
    clearTimeout(timer);
    try {
      ov = await api('overview');
      error = '';
    } catch (e) {
      error = (e as Error).message;
    }
    timer = setTimeout(load, active ? 3000 : 10000);
  }

  async function service(cmd: string) {
    busy = true;
    try {
      note = (await api<{ message: string }>('service', { cmd })).message;
    } catch (e) {
      note = (e as Error).message;
    }
    busy = false;
    load();
  }

  async function stopMove() {
    busy = true;
    try {
      note = (await api<{ message: string }>('stop_move')).message;
    } catch (e) {
      note = (e as Error).message;
    }
    busy = false;
    load();
  }

  function rate(p?: Progress) {
    if (!p?.rate) return '';
    return `${bytes(p.rate)}/s · ${duration(p.eta)} left`;
  }

  onMount(() => {
    load();
    return () => clearTimeout(timer);
  });
</script>

{#if error}<div class="mtc-msg mtc-err">{error}</div>{/if}
{#if ov}
  <section class="mtc-card">
    <div class="mtc-head">
      <span class="mtc-dot" class:ok={ov.running}></span>
      <strong>{ov.running ? 'Service running' : ov.disabled ? 'Service stopped (autostart off)' : 'Service stopped'}</strong>
      <span class="mtc-grow"></span>
      {#if ov.running}
        <button type="button" class="mtc-btn" disabled={busy} onclick={() => service('restart')}>Restart</button>
        <button type="button" class="mtc-btn" disabled={busy} onclick={() => service('stop')}>Stop</button>
      {:else}
        <button type="button" class="mtc-btn primary" disabled={busy} onclick={() => service('start')}>Start</button>
      {/if}
    </div>
    {#if note}<div class="mtc-sub">{note}</div>{/if}
    {#if ov.problem}<div class="mtc-msg mtc-err">{ov.problem}</div>{/if}
    {#if ov.running && st && !st.storage_ready}<div class="mtc-msg mtc-warn">Waiting for the cache pool and the array to be mounted.</div>{/if}
    {#if ov.servers_enabled?.length === 0}<div class="mtc-msg mtc-warn">No media server is switched on. Open Settings.</div>{/if}
    {#each servers as [key, s]}
      {#if !s.ok}<div class="mtc-msg mtc-err"><strong>{key}:</strong> {s.message}</div>{/if}
    {/each}
    <div class="mtc-stats">
      <div><span>Cached by the plugin</span><b>{ov.cached.files} files · {bytes(ov.cached.bytes)}</b></div>
      {#if ov.pool}
        <div class="mtc-wide">
          <span>Cache pool</span>
          <b>{bytes(ov.pool.used)} of {bytes(ov.pool.total)} ({pct(ov.pool.used, ov.pool.total)} %, limit {ov.pool.limit} %)</b>
          <div class="mtc-bar"><i style:width="{pct(ov.pool.used, ov.pool.total)}%" class:full={pct(ov.pool.used, ov.pool.total) >= ov.pool.limit}></i></div>
        </div>
      {/if}
    </div>
  </section>

  <section class="mtc-card">
    <h3>Streams</h3>
    {#if !st?.streams?.length}
      <p class="mtc-sub">Nothing is playing.</p>
    {:else}
      <ul class="mtc-list">
        {#each st.streams as s}
          <li>
            <div class="mtc-name">{s.title}</div>
            <div class="mtc-meta">
              <span>{s.service}{s.user ? ' · ' + s.user : ''}</span>
              {#if s.progress != null}<span>{Math.round(s.progress * 100)} %</span>{/if}
              {#if s.skipped}<span class="mtc-warn">not cached: {s.skipped}</span>{:else if s.on_cache}<span class="mtc-ok">on cache</span>{:else}<span>on array</span>{/if}
            </div>
            <div class="mtc-path">{s.host}</div>
          </li>
        {/each}
      </ul>
    {/if}
  </section>

  <section class="mtc-card">
    <h3>Copy to cache</h3>
    {#if copying}
      <div class="mtc-name">{copying.name}</div>
      <div class="mtc-bar"><i style:width="{pct(copying.progress.done, copying.progress.size)}%"></i></div>
      <div class="mtc-meta"><span>{bytes(copying.progress.done)} of {bytes(copying.progress.size)}</span><span>{rate(copying.progress)}</span></div>
    {:else}
      <p class="mtc-sub">No copy running.</p>
    {/if}
    {#if st?.queue?.length}
      <h4>Waiting ({st.queue.length})</h4>
      <ul class="mtc-list small">
        {#each st.queue.slice(0, 20) as q}<li><div class="mtc-name">{q}</div></li>{/each}
      </ul>
    {/if}
  </section>

  <section class="mtc-card">
    <div class="mtc-head">
      <h3>Move to array</h3>
      <span class="mtc-grow"></span>
      {#if moving}<button type="button" class="mtc-btn" disabled={busy} onclick={stopMove}>Stop move</button>{/if}
    </div>
    {#if moving && job}
      <div class="mtc-sub">{job.done} of {job.total} file(s) · {bytes(job.bytes)}</div>
      <div class="mtc-bar"><i style:width="{pct(job.done, job.total)}%"></i></div>
      {#if job.current}
        <div class="mtc-name">{job.current}</div>
        {#if job.progress}
          <div class="mtc-bar thin"><i style:width="{pct(job.progress.done, job.progress.size)}%"></i></div>
          <div class="mtc-meta"><span>{bytes(job.progress.done)} of {bytes(job.progress.size)}</span><span>{rate(job.progress)}</span></div>
        {/if}
      {/if}
    {:else if job?.finished}
      <p class="mtc-sub">
        Last move: {job.done} of {job.total} file(s), {bytes(job.bytes)}{job.stopped ? ', stopped' : ''}{job.failed ? `, ${job.failed} failed` : ''}{job.skipped ? `, ${job.skipped} in use` : ''}.
      </p>
    {:else}
      <p class="mtc-sub">No move running.</p>
    {/if}
  </section>
{:else if !error}
  <p class="mtc-sub">Loading…</p>
{/if}
