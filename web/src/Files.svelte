<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from './lib/api';
  import { bytes } from './lib/format';
  import Confirm from './Confirm.svelte';

  type Entry = { path: string; name: string; dir: boolean; size: number; tracked: boolean; in_use: boolean; files?: number; cached?: number; busy?: number; capped?: boolean };
  let path = $state('');
  let parent = $state('');
  let cacheRoot = $state('');
  let entries = $state<Entry[]>([]);
  let picked = $state<Record<string, boolean>>({});
  let error = $state('');
  let note = $state('');
  let asking = $state(false);
  let loading = $state(false);

  const chosen = $derived(entries.filter((e) => picked[e.path]));

  async function open(p: string) {
    loading = true;
    try {
      const r = await api<any>('browse', { path: p });
      path = r.path;
      parent = r.parent;
      cacheRoot = r.cache_root;
      entries = r.entries;
      picked = {};
      error = '';
    } catch (e) {
      error = (e as Error).message;
    }
    loading = false;
  }

  async function move() {
    asking = false;
    try {
      note = (await api<{ message: string }>('uncache', { paths: chosen.map((e) => e.path) })).message;
      picked = {};
    } catch (e) {
      error = (e as Error).message;
    }
  }

  function crumbs(): { label: string; path: string }[] {
    if (!path) return [];
    const rel = path.startsWith(cacheRoot + '/') ? path.slice(cacheRoot.length + 1) : path;
    const parts = rel.split('/');
    return parts.map((p, i) => ({ label: p, path: cacheRoot + '/' + parts.slice(0, i + 1).join('/') }));
  }

  function summary(e: Entry): string {
    if (!e.dir) return bytes(e.size);
    return `${e.files}${e.capped ? '+' : ''} files · ${bytes(e.size)}`;
  }

  onMount(() => open(''));
</script>

<div class="mtc-crumbs">
  <button type="button" class="mtc-link" onclick={() => open('')}>Media folders</button>
  {#each crumbs() as c, i}
    <span>/</span>
    {#if i < crumbs().length - 1}<button type="button" class="mtc-link" onclick={() => open(c.path)}>{c.label}</button>{:else}<strong>{c.label}</strong>{/if}
  {/each}
</div>

{#if error}<div class="mtc-msg mtc-err">{error}</div>{/if}
{#if note}<div class="mtc-msg mtc-ok">{note}</div>{/if}

<div class="mtc-toolbar">
  {#if path}<button type="button" class="mtc-btn" onclick={() => open(parent)}>Up</button>{/if}
  <span class="mtc-grow"></span>
  <button type="button" class="mtc-btn primary" disabled={chosen.length === 0} onclick={() => (asking = true)}>Move back to array ({chosen.length})</button>
</div>

{#if loading && entries.length === 0}
  <p class="mtc-sub">Loading…</p>
{:else if entries.length === 0}
  <p class="mtc-sub">{path ? 'No media here.' : 'No mapped media folder exists on the cache yet. Check the Docker mappings in Settings.'}</p>
{:else}
  <ul class="mtc-files">
    {#each entries as e (e.path)}
      <li class:busy={e.in_use || (e.busy ?? 0) > 0}>
        <input type="checkbox" aria-label="Select {e.name}" bind:checked={picked[e.path]} />
        <div class="mtc-fname">
          {#if e.dir}<button type="button" class="mtc-link" onclick={() => open(e.path)}>{e.name}/</button>{:else}<span>{e.name}</span>{/if}
        </div>
        <div class="mtc-fmeta">
          <span>{summary(e)}</span>
          {#if e.dir && e.cached}<span class="mtc-ok">{e.cached} by the plugin</span>{/if}
          {#if !e.dir && e.tracked}<span class="mtc-ok">by the plugin</span>{/if}
          {#if e.in_use || (e.busy ?? 0) > 0}<span class="mtc-warn">playing</span>{/if}
        </div>
      </li>
    {/each}
  </ul>
{/if}

{#if asking}
  <Confirm
    title="Move back to the array"
    text={`${chosen.length} item(s) go back to the array: ${chosen.map((e) => e.name).join(', ')}. Files that are playing or were written in the last 30 minutes stay on the cache.`}
    ok="Move"
    onok={move}
    oncancel={() => (asking = false)}
  />
{/if}
