<script lang="ts">
  import { onMount } from 'svelte';
  import Icon from './Icon.svelte';
  import Activity from './Activity.svelte';
  import Files from './Files.svelte';
  import Settings from './Settings.svelte';
  import Log from './Log.svelte';
  import { api } from './lib/api';
  import { app, refresh } from './lib/state.svelte';
  import { bytes, pct } from './lib/format';

  const tabs = [
    ['activity', 'Activity', 'list'],
    ['files', 'Cache', 'database'],
    ['settings', 'Settings', 'settings'],
    ['log', 'Log', 'file']
  ] as const;
  let tab = $state<string>('activity');
  let switching = $state(false);

  const ov = $derived(app.ov);
  const st = $derived(ov?.status);
  const problems = $derived.by(() => {
    const out: string[] = [];
    if (!ov) return out;
    if (ov.problem) out.push(ov.problem);
    if (ov.running && st && !st.storage_ready) out.push('Waiting for the cache pool and the array.');
    if (ov.servers_enabled?.length === 0) out.push('No media server is switched on.');
    for (const [k, s] of Object.entries((st?.servers ?? {}) as Record<string, any>)) if (!s.ok) out.push(`${k}: ${s.message}`);
    return out;
  });
  const counts = $derived<Record<string, number>>({
    activity: (st?.streams?.length ?? 0) + (st?.copying ? 1 : 0) + (ov?.move?.running ? 1 : 0),
    files: ov?.cached?.files ?? 0
  });
  const used = $derived(ov?.pool ? pct(ov.pool.used, ov.pool.total) : 0);

  async function toggle() {
    switching = true;
    try {
      await api('service', { cmd: ov?.running ? 'stop' : 'start' });
    } catch (e) {
      app.error = (e as Error).message;
    }
    switching = false;
    refresh();
  }

  onMount(() => {
    refresh();
    const vis = () => !document.hidden && refresh();
    document.addEventListener('visibilitychange', vis);
    return () => document.removeEventListener('visibilitychange', vis);
  });
</script>

{#if !ov}
  <p class="mtc-dim">{app.error || 'Loading…'}</p>
{:else}
  <header class="mtc-header">
    <div class="mtc-state">
      <span class="mtc-dot" class:on={ov.running} class:bad={problems.length > 0}></span>
      <div>
        <b>{ov.running ? 'Running' : 'Stopped'}</b>
        <span class="mtc-dim">{ov.running ? ov.servers_enabled.join(', ') || 'no server' : ov.disabled ? 'autostart off' : ''}</span>
      </div>
      <button type="button" class="mtc-btn icon" title={ov.running ? 'Stop the service' : 'Start the service'} disabled={switching} onclick={toggle}>
        <Icon name={ov.running ? 'stop' : 'play'} />{ov.running ? 'Stop' : 'Start'}
      </button>
    </div>
    <div class="mtc-kpis">
      <div><span>Streams</span><b>{st?.streams?.length ?? 0}</b></div>
      <div><span>On cache</span><b>{ov.cached.files} · {bytes(ov.cached.bytes)}</b></div>
      {#if ov.pool}
        <div class="mtc-pool" title="{bytes(ov.pool.free)} free, copies stop at {ov.pool.limit} %">
          <span>Pool {used} %</span>
          <i><em style:width="{used}%" class:full={used >= ov.pool.limit}></em><s style:left="{ov.pool.limit}%"></s></i>
        </div>
      {/if}
    </div>
  </header>

  {#each problems as p}<div class="mtc-banner"><Icon name="alert" /><span>{p}</span></div>{/each}
  {#if app.error}<div class="mtc-banner"><Icon name="alert" /><span>{app.error}</span></div>{/if}

  <div class="mtc-tabs" role="tablist">
    {#each tabs as [id, label, icon]}
      <button type="button" role="tab" aria-selected={tab === id} class:on={tab === id} onclick={() => (tab = id)}>
        <Icon name={icon} /><span>{label}</span>{#if counts[id]}<small>{counts[id]}</small>{/if}
      </button>
    {/each}
  </div>

  {#if tab === 'activity'}<Activity />{:else if tab === 'files'}<Files />{:else if tab === 'settings'}<Settings />{:else}<Log />{/if}
{/if}
