<script lang="ts">
  import { onMount } from 'svelte';
  import Icon from './Icon.svelte';
  import { api, ApiError } from './lib/api';
  import { app, refresh } from './lib/state.svelte';

  const servers = [
    { key: 'PLEX', label: 'Plex', token: 'PLEX_TOKEN', tokenLabel: 'Token', hint: 'http://192.168.1.10:32400' },
    { key: 'EMBY', label: 'Emby', token: 'EMBY_API_KEY', tokenLabel: 'API key', hint: 'http://192.168.1.10:8096' },
    { key: 'JELLYFIN', label: 'Jellyfin', token: 'JELLYFIN_API_KEY', tokenLabel: 'API key', hint: 'http://192.168.1.10:8096' }
  ];

  let s = $state<Record<string, any>>({});
  let stored = $state<Record<string, boolean>>({});
  let tokens = $state<Record<string, string>>({});
  let root = $state<any>(null);
  let maps = $state<{ d: string; h: string }[]>([]);
  let open = $state<Record<string, boolean>>({});
  let saveState = $state<'' | 'saving' | 'saved' | 'error'>('');
  let error = $state('');
  let field = $state('');
  let tests = $state<Record<string, any>>({});
  let testing = $state<Record<string, boolean>>({});
  let timer: ReturnType<typeof setTimeout> | undefined;

  const srv = $derived((app.ov?.status?.servers ?? {}) as Record<string, any>);

  function mappingString() {
    return maps.filter((m) => m.d.trim() && m.h.trim()).map((m) => `${m.d.trim()}:${m.h.trim()}`).join(';');
  }

  async function load() {
    const r = await api<any>('settings_get');
    s = r.settings;
    stored = r.stored;
    root = r.cache_root;
    maps = (s.DOCKER_MAPPINGS || '').split(';').filter((x: string) => x.includes(':')).map((x: string) => {
      const i = x.indexOf(':');
      return { d: x.slice(0, i), h: x.slice(i + 1) };
    });
    maps.push({ d: '', h: '' });
  }

  function save(extra: Record<string, unknown> = {}) {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      saveState = 'saving';
      const ch: Record<string, unknown> = { ...s, DOCKER_MAPPINGS: mappingString(), ...extra };
      for (const sv of servers) if (tokens[sv.token]) ch[sv.token] = tokens[sv.token];
      try {
        await api('settings_save', { settings: ch });
        const r = await api<any>('settings_get');
        stored = r.stored;
        root = r.cache_root;
        tokens = {};
        saveState = 'saved';
        error = field = '';
        if (!maps.length || maps[maps.length - 1].d || maps[maps.length - 1].h) maps.push({ d: '', h: '' });
        setTimeout(refresh, 1500);
      } catch (e) {
        saveState = 'error';
        error = (e as Error).message;
        field = e instanceof ApiError ? e.field : '';
      }
    }, 500);
  }

  function flip(k: string) {
    s[k] = s[k] === 'yes' ? 'no' : 'yes';
    save();
  }

  async function test(sv: (typeof servers)[number]) {
    testing[sv.key] = true;
    try {
      tests[sv.key] = await api('test', {
        service: sv.key, url: s[sv.key + '_URL'], token: tokens[sv.token] || '',
        verify: s[sv.key + '_VERIFY_TLS'] === 'yes', pin: s[sv.key + '_CERT_SHA256'] || '', mappings: mappingString()
      });
    } catch (e) {
      tests[sv.key] = { success: false, message: (e as Error).message };
    }
    testing[sv.key] = false;
  }

  async function trust(key: string, sha: string) {
    try {
      await api('trust_cert', { service: key, sha256: sha });
      s[key + '_CERT_SHA256'] = sha;
      s[key + '_VERIFY_TLS'] = 'yes';
      tests[key] = { success: true, message: 'Certificate trusted.' };
    } catch (e) {
      tests[key] = { success: false, message: (e as Error).message };
    }
  }

  function chip(key: string): [string, string] {
    if (s['ENABLE_' + key] !== 'yes') return ['', 'off'];
    const x = srv[key];
    if (!x) return ['', 'not checked yet'];
    return x.ok ? ['ok', 'connected'] : ['err', x.kind === 'certificate' ? 'certificate' : x.kind === 'token' ? 'token refused' : 'not reachable'];
  }

  onMount(() => {
    load().catch((e) => (error = e.message));
  });
</script>

<div class="mtc-savestate {saveState}">
  {#if saveState === 'saving'}Saving…{:else if saveState === 'saved'}<Icon name="check" />Saved{:else if saveState === 'error'}<Icon name="alert" />Not saved{/if}
</div>
{#if error}<div class="mtc-banner"><Icon name="alert" /><span>{error}</span></div>{/if}

{#if root}
  <section class="mtc-panel">
    <h3>Media servers</h3>
    {#each servers as sv}
      {@const c = chip(sv.key)}
      <div class="mtc-srv">
        <label class="mtc-switch" title="Use {sv.label}">
          <input type="checkbox" checked={s['ENABLE_' + sv.key] === 'yes'} onchange={() => flip('ENABLE_' + sv.key)} /><i></i>
        </label>
        <button type="button" class="mtc-srv-name" onclick={() => (open[sv.key] = !open[sv.key])}>
          <Icon name="server" /><b>{sv.label}</b>
          <span class="mtc-tag {c[0] ? 't-' + c[0] : ''}">{c[1]}</span>
          <span class="mtc-dim mtc-url">{s[sv.key + '_URL']}</span>
        </button>
      </div>
      {#if open[sv.key] || (s['ENABLE_' + sv.key] === 'yes' && !srv[sv.key]?.ok && srv[sv.key])}
        <div class="mtc-sub">
          <div class="mtc-field" class:bad={field === sv.key + '_URL'}>
            <label for="{sv.key}-url">Address</label>
            <input id="{sv.key}-url" type="text" bind:value={s[sv.key + '_URL']} placeholder={sv.hint} onchange={() => save()} />
          </div>
          <div class="mtc-field">
            <label for="{sv.key}-tok">{sv.tokenLabel}</label>
            <input id="{sv.key}-tok" type="password" autocomplete="off" bind:value={tokens[sv.token]} placeholder={stored[sv.token] ? 'saved' : 'not set'} onchange={() => save()} />
            {#if stored[sv.token]}<button type="button" class="mtc-btn small" onclick={() => save({ [sv.token + '_CLEAR']: true })}>Remove</button>{/if}
          </div>
          <div class="mtc-field">
            <span class="mtc-label">Certificate</span>
            <label class="mtc-check"><input type="checkbox" checked={s[sv.key + '_VERIFY_TLS'] === 'yes'} onchange={() => flip(sv.key + '_VERIFY_TLS')} />verify (https)</label>
            {#if s[sv.key + '_CERT_SHA256']}<span class="mtc-dim" title={s[sv.key + '_CERT_SHA256']}>· own certificate trusted</span>{/if}
          </div>
          <div class="mtc-field">
            <span class="mtc-label"></span>
            <button type="button" class="mtc-btn" disabled={testing[sv.key]} onclick={() => test(sv)}><Icon name="refresh" />{testing[sv.key] ? 'Testing…' : 'Test connection'}</button>
          </div>
          {#if tests[sv.key]}
            {@const r = tests[sv.key]}
            <div class="mtc-result" class:ok={r.success}>
              <div><Icon name={r.success ? 'check' : 'alert'} />{r.message}</div>
              {#if r.advice}<div class="mtc-dim">{r.advice}</div>{/if}
              {#if r.certificate}
                <div class="mtc-dim mtc-fp">SHA-256 {r.certificate.sha256}<br />{r.certificate.subject} · until {r.certificate.not_after}</div>
                <button type="button" class="mtc-btn primary" onclick={() => trust(sv.key, r.certificate.sha256)}><Icon name="shield" />Trust this certificate</button>
              {/if}
              {#each [...(r.streams ?? []), ...(r.libraries ?? [])] as row}
                <div class="mtc-map-row">
                  <span class="mtc-tag {row.state === 'ok' ? 't-ok' : 't-warn'}">{row.state === 'ok' ? 'found' : row.state}</span>
                  <span class="mtc-path">{row.container} → {row.host}</span>
                  {#if row.state !== 'ok'}<span class="mtc-dim">{row.text}</span>{/if}
                </div>
              {/each}
            </div>
          {/if}
        </div>
      {/if}
    {/each}
  </section>

  <section class="mtc-panel">
    <h3>Paths</h3>
    <div class="mtc-field" class:bad={field === 'CACHE_ROOT' || (!!root.problem && s.CACHE_ROOT === root.current)}>
      <label for="mtc-cache">Cache pool</label>
      <select id="mtc-cache" bind:value={s.CACHE_ROOT} onchange={() => save()}>
        {#if !root.listed}<option value={root.current}>{root.current} (not valid)</option>{/if}
        {#each root.pools as p}<option value={p}>{p}</option>{/each}
      </select>
    </div>
    {#if root.problem && s.CACHE_ROOT === root.current}<p class="mtc-hint err">{root.problem}</p>{/if}
    <div class="mtc-field" class:bad={field === 'ARRAY_ROOT'}>
      <label for="mtc-array">Array</label>
      <input id="mtc-array" type="text" bind:value={s.ARRAY_ROOT} placeholder="/mnt/user" onchange={() => save()} />
    </div>
    <div class="mtc-maps" class:bad={field === 'DOCKER_MAPPINGS'}>
      <div class="mtc-maps-head"><span>Path in the container</span><span>Path on the host</span><span></span></div>
      {#each maps as m, i}
        <div class="mtc-maps-row">
          <input type="text" aria-label="Container path" bind:value={m.d} placeholder="/media" onchange={() => m.d && m.h && save()} />
          <input type="text" aria-label="Host path" bind:value={m.h} placeholder="/mnt/user/Media" onchange={() => m.d && m.h && save()} />
          {#if m.d || m.h}<button type="button" class="mtc-btn icon-only" aria-label="Remove mapping" onclick={() => { maps = maps.filter((_, j) => j !== i); save(); }}><Icon name="x" /></button>{:else}<span></span>{/if}
        </div>
      {/each}
    </div>
  </section>

  <section class="mtc-panel">
    <h3>Caching</h3>
    {#each [['COPY_DELAY', 'Copy after', 's of playback'], ['CHECK_INTERVAL', 'Check every', 's'], ['CACHE_MAX_USAGE', 'Pool limit', '%']] as [k, label, unit]}
      <div class="mtc-field" class:bad={field === k}>
        <label for="mtc-{k}">{label}</label>
        <input id="mtc-{k}" class="num" type="number" bind:value={s[k]} onchange={() => save()} /><span class="mtc-dim">{unit}</span>
      </div>
    {/each}
    <div class="mtc-field">
      <span class="mtc-label">Series</span>
      <label class="mtc-check"><input type="checkbox" checked={s.ENABLE_EPISODE_BATCHING === 'yes'} onchange={() => flip('ENABLE_EPISODE_BATCHING')} />long seasons in batches</label>
    </div>
    {#if s.ENABLE_EPISODE_BATCHING === 'yes'}
      <div class="mtc-field indent">
        <label for="mtc-bs">Batch</label>
        <input id="mtc-bs" class="num" type="number" bind:value={s.EPISODE_BATCH_SIZE} onchange={() => save()} /><span class="mtc-dim">episodes, +</span>
        <input class="num" type="number" aria-label="Tolerance" bind:value={s.EPISODE_BATCH_TOLERANCE} onchange={() => save()} /><span class="mtc-dim">tolerance</span>
      </div>
    {/if}
    <div class="mtc-field">
      <span class="mtc-label"></span>
      <label class="mtc-check"><input type="checkbox" checked={s.ENABLE_NEXT_SEASON_PREFETCH === 'yes'} onchange={() => flip('ENABLE_NEXT_SEASON_PREFETCH')} />start the next season early</label>
    </div>
    {#if s.ENABLE_EPISODE_BATCHING === 'yes' || s.ENABLE_NEXT_SEASON_PREFETCH === 'yes'}
      <div class="mtc-field indent">
        <label for="mtc-bp">Ahead</label>
        <input id="mtc-bp" class="num" type="number" bind:value={s.EPISODE_BATCH_PREFETCH} onchange={() => save()} /><span class="mtc-dim">episodes before the end</span>
      </div>
    {/if}
    <div class="mtc-field" class:bad={field === 'MEDIA_FILETYPES'}>
      <label for="mtc-types">File types</label>
      <input id="mtc-types" type="text" bind:value={s.MEDIA_FILETYPES} placeholder=".mkv .mp4" onchange={() => save()} />
    </div>
    <div class="mtc-field" class:bad={field === 'EXCLUDE_DIRS'}>
      <label for="mtc-excl">Skip folders</label>
      <input id="mtc-excl" type="text" bind:value={s.EXCLUDE_DIRS} placeholder="Kids, Extras" onchange={() => save()} />
    </div>
  </section>

  <section class="mtc-panel">
    <h3>Cleanup</h3>
    <div class="mtc-field" class:bad={field === 'CLEANUP_MODE'}>
      <label for="mtc-clean">Move back</label>
      <select id="mtc-clean" bind:value={s.CLEANUP_MODE} onchange={() => save()}>
        <option value="none">never</option>
        <option value="smart">when watched</option>
        <option value="days">after days</option>
      </select>
    </div>
    {#if s.CLEANUP_MODE === 'smart'}
      <div class="mtc-field indent">
        <label for="mtc-mdd">Movies after</label>
        <input id="mtc-mdd" class="num" type="number" bind:value={s.MOVIE_DELETE_DELAY} onchange={() => save()} /><span class="mtc-dim">s</span>
      </div>
      <div class="mtc-field indent">
        <label for="mtc-keep">Keep</label>
        <input id="mtc-keep" class="num" type="number" bind:value={s.EPISODE_KEEP_PREVIOUS} onchange={() => save()} /><span class="mtc-dim">previous episodes</span>
      </div>
    {:else if s.CLEANUP_MODE === 'days'}
      <div class="mtc-field indent">
        <label for="mtc-days">After</label>
        <input id="mtc-days" class="num" type="number" bind:value={s.CACHE_MAX_DAYS} onchange={() => save()} /><span class="mtc-dim">days</span>
      </div>
    {/if}
    <div class="mtc-field">
      <span class="mtc-label">Pool full</span>
      <label class="mtc-check"><input type="checkbox" checked={s.ENABLE_CACHE_EVICTION === 'yes'} onchange={() => flip('ENABLE_CACHE_EVICTION')} />move the oldest copies back</label>
    </div>
    <div class="mtc-field" class:bad={field === 'NOTIFY_OFFLINE_MINUTES'}>
      <label for="mtc-off">Notify after</label>
      <input id="mtc-off" class="num" type="number" bind:value={s.NOTIFY_OFFLINE_MINUTES} onchange={() => save()} /><span class="mtc-dim">min without server</span>
    </div>
  </section>
{/if}
