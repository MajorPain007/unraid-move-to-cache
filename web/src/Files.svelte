<script lang="ts">
  import { onMount } from 'svelte';
  import Icon from './Icon.svelte';
  import Confirm from './Confirm.svelte';
  import { api } from './lib/api';
  import { refresh } from './lib/state.svelte';
  import { bytes } from './lib/format';

  type Entry = { path: string; name: string; dir: boolean; size: number; tracked: boolean; in_use: boolean; files?: number; cached?: number; busy?: number; capped?: boolean };
  let path = $state('');
  let parent = $state('');
  let root = $state('');
  let entries = $state<Entry[]>([]);
  let picked = $state<Record<string, boolean>>({});
  let error = $state('');
  let note = $state('');
  let asking = $state(false);

  const chosen = $derived(entries.filter((e) => picked[e.path]));
  const shown = $derived(path ? (path.startsWith(root + '/') ? path.slice(root.length + 1) : path) : 'Media folders');

  async function open(p: string) {
    try {
      const r = await api<any>('browse', { path: p });
      ({ path, parent, entries } = r);
      root = r.cache_root;
      picked = {};
      error = '';
    } catch (e) {
      error = (e as Error).message;
    }
  }

  async function move() {
    asking = false;
    try {
      note = (await api<{ message: string }>('uncache', { paths: chosen.map((e) => e.path) })).message;
      picked = {};
      refresh();
    } catch (e) {
      error = (e as Error).message;
    }
  }

  onMount(() => open(''));
</script>

<div class="mtc-where"><Icon name="folder" /><span>{shown}</span></div>
{#if error}<div class="mtc-banner"><Icon name="alert" /><span>{error}</span></div>{/if}
{#if note}<div class="mtc-banner ok"><Icon name="check" /><span>{note}</span></div>{/if}

<div class="mtc-table">
  <div class="mtc-th"><span></span><span>Name</span><span>Size</span><span>State</span></div>
  {#if path}
    <button type="button" class="mtc-tr up" onclick={() => open(parent)}>
      <span></span><span class="mtc-name"><Icon name="up" />../</span><span></span><span></span>
    </button>
  {/if}
  {#each entries as e (e.path)}
    <div class="mtc-tr" class:sel={picked[e.path]}>
      <span><input type="checkbox" aria-label="Select {e.name}" bind:checked={picked[e.path]} /></span>
      {#if e.dir}
        <button type="button" class="mtc-name link" onclick={() => open(e.path)}><Icon name="folder" />{e.name}</button>
      {:else}
        <span class="mtc-name"><Icon name="file" />{e.name}</span>
      {/if}
      <span class="mtc-num">{bytes(e.size)}{e.dir ? ` · ${e.files}${e.capped ? '+' : ''} files` : ''}</span>
      <span class="mtc-tags">
        {#if e.in_use || (e.busy ?? 0) > 0}<span class="mtc-tag t-warn">playing</span>{/if}
        {#if e.dir && e.cached}<span class="mtc-tag t-ok">{e.cached} by plugin</span>{:else if !e.dir && e.tracked}<span class="mtc-tag t-ok">by plugin</span>{:else if !e.dir}<span class="mtc-tag">other</span>{/if}
      </span>
    </div>
  {:else}
    <p class="mtc-empty">{path ? 'No media here.' : 'No mapped media folder on the cache. Check the Docker mappings in Settings.'}</p>
  {/each}
</div>

{#if chosen.length}
  <div class="mtc-selbar">
    <span>{chosen.length} selected</span>
    <button type="button" class="mtc-btn" onclick={() => (picked = {})}>Clear</button>
    <button type="button" class="mtc-btn primary" onclick={() => (asking = true)}><Icon name="send" />Move to array</button>
  </div>
{/if}

{#if asking}
  <Confirm
    title="Move to the array"
    text={`${chosen.map((e) => e.name).join(', ')}. Files that are playing or were written in the last 30 minutes stay.`}
    ok="Move"
    onok={move}
    oncancel={() => (asking = false)}
  />
{/if}
