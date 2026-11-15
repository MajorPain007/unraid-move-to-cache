<script lang="ts">
  import Icon from './Icon.svelte';
  import { api } from './lib/api';
  import { app, refresh } from './lib/state.svelte';
  import { bytes, duration, pct } from './lib/format';

  const st = $derived(app.ov?.status);
  const move = $derived(app.ov?.move);
  const job = $derived(move?.job);
  let stopping = $state(false);

  function rate(p: any): string {
    return p?.rate ? `${bytes(p.rate)}/s · ${duration(p.eta)} left` : '';
  }

  async function stopMove() {
    stopping = true;
    try {
      await api('stop_move');
    } catch (e) {
      app.error = (e as Error).message;
    }
    stopping = false;
    refresh();
  }
</script>

<section class="mtc-panel">
  <h3>Playing</h3>
  {#if !st?.streams?.length}
    <p class="mtc-empty">Nothing is playing.</p>
  {:else}
    {#each st.streams as s}
      <div class="mtc-row">
        <span class="mtc-ic" class:ok={s.on_cache} class:warn={!!s.skipped}><Icon name="play" /></span>
        <div class="mtc-main">
          <div class="mtc-title">{s.title}</div>
          <div class="mtc-dim mtc-path">{s.host}</div>
        </div>
        <div class="mtc-side">
          {#if s.skipped}<span class="mtc-tag t-warn" title={s.skipped}>not cached</span>{:else if s.on_cache}<span class="mtc-tag t-ok">on cache</span>{:else}<span class="mtc-tag">on array</span>{/if}
          <span class="mtc-dim">{s.service}{s.user ? ' · ' + s.user : ''}{s.progress != null ? ' · ' + Math.round(s.progress * 100) + ' %' : ''}</span>
        </div>
      </div>
    {/each}
  {/if}
</section>

<section class="mtc-panel">
  <h3>Transfers</h3>
  {#if !st?.copying && !move?.running && !st?.queue?.length}
    <p class="mtc-empty">No transfer running.{job?.finished ? ` Last move back: ${job.done} of ${job.total} file(s), ${bytes(job.bytes)}${job.failed ? `, ${job.failed} failed` : ''}.` : ''}</p>
  {/if}
  {#if st?.copying}
    {@const c = st.copying}
    <div class="mtc-row">
      <span class="mtc-ic run"><Icon name="download" /></span>
      <div class="mtc-main">
        <div class="mtc-title">{c.name}</div>
        <div class="mtc-bar"><i style:width="{pct(c.progress.done, c.progress.size)}%"></i></div>
      </div>
      <div class="mtc-side">
        <span class="mtc-tag t-run">to cache</span>
        <span class="mtc-dim">{bytes(c.progress.done)} / {bytes(c.progress.size)} {rate(c.progress)}</span>
      </div>
    </div>
  {/if}
  {#if move?.running && job}
    <div class="mtc-row">
      <span class="mtc-ic run"><Icon name="send" /></span>
      <div class="mtc-main">
        <div class="mtc-title">{job.current || 'Preparing…'}</div>
        <div class="mtc-bar"><i style:width="{job.progress ? pct(job.progress.done, job.progress.size) : 0}%"></i></div>
      </div>
      <div class="mtc-side">
        <span class="mtc-tag t-run">to array · {job.done + 1}/{job.total}</span>
        <span class="mtc-dim">{job.progress ? `${bytes(job.progress.done)} / ${bytes(job.progress.size)} ${rate(job.progress)}` : ''}</span>
        <button type="button" class="mtc-btn small" disabled={stopping} onclick={stopMove}>Stop</button>
      </div>
    </div>
  {/if}
  {#each st?.queue ?? [] as q}
    <div class="mtc-row">
      <span class="mtc-ic"><Icon name="clock" /></span>
      <div class="mtc-main"><div class="mtc-title">{q}</div></div>
      <div class="mtc-side"><span class="mtc-tag">waiting</span></div>
    </div>
  {/each}
</section>
