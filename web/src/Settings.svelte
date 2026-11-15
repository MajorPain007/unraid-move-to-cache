<script lang="ts">
  import { onMount } from 'svelte';
  import { api, ApiError } from './lib/api';

  const servers = [
    { key: 'PLEX', label: 'Plex', token: 'PLEX_TOKEN', tokenLabel: 'Token' },
    { key: 'EMBY', label: 'Emby', token: 'EMBY_API_KEY', tokenLabel: 'API key' },
    { key: 'JELLYFIN', label: 'Jellyfin', token: 'JELLYFIN_API_KEY', tokenLabel: 'API key' }
  ];
  const numbers: [string, string, string][] = [
    ['CHECK_INTERVAL', 'Check interval', 'seconds between two questions to the media servers'],
    ['COPY_DELAY', 'Copy delay', 'seconds a stream must run before it is copied'],
    ['CACHE_MAX_USAGE', 'Cache limit', '% of the pool; above this nothing new is copied'],
    ['NOTIFY_OFFLINE_MINUTES', 'Notify after', 'minutes a media server has not answered']
  ];

  let s = $state<Record<string, any>>({});
  let stored = $state<Record<string, boolean>>({});
  let tokens = $state<Record<string, string>>({});
  let clear = $state<Record<string, boolean>>({});
  let root = $state<any>(null);
  let maps = $state<{ d: string; h: string }[]>([]);
  let error = $state('');
  let field = $state('');
  let saved = $state('');
  let saving = $state(false);
  let tests = $state<Record<string, any>>({});
  let testing = $state<Record<string, boolean>>({});

  function mappingString() {
    return maps.filter((m) => m.d.trim() && m.h.trim()).map((m) => `${m.d.trim()}:${m.h.trim()}`).join(';');
  }

  async function load() {
    try {
      const r = await api<any>('settings_get');
      s = r.settings;
      stored = r.stored;
      root = r.cache_root;
      maps = (s.DOCKER_MAPPINGS || '').split(';').filter((x: string) => x.includes(':')).map((x: string) => {
        const i = x.indexOf(':');
        return { d: x.slice(0, i), h: x.slice(i + 1) };
      });
      if (maps.length === 0) maps = [{ d: '', h: '' }];
      tokens = {};
      clear = {};
    } catch (e) {
      error = (e as Error).message;
    }
  }

  async function save() {
    saving = true;
    error = field = saved = '';
    const ch: Record<string, unknown> = { ...s, DOCKER_MAPPINGS: mappingString() };
    for (const sv of servers) {
      ch[sv.token] = tokens[sv.token] || '';
      if (clear[sv.token]) ch[sv.token + '_CLEAR'] = true;
    }
    try {
      await api('settings_save', { settings: ch });
      saved = 'Saved. The service restarts with the new settings.';
      await load();
    } catch (e) {
      error = (e as Error).message;
      field = e instanceof ApiError ? e.field : '';
    }
    saving = false;
  }

  async function test(key: string, token: string) {
    testing[key] = true;
    try {
      tests[key] = await api('test', {
        service: key,
        url: s[key + '_URL'],
        token: tokens[token] || '',
        verify: s[key + '_VERIFY_TLS'] === 'yes',
        pin: s[key + '_CERT_SHA256'] || '',
        mappings: mappingString()
      });
    } catch (e) {
      tests[key] = { success: false, message: (e as Error).message };
    }
    testing[key] = false;
  }

  async function trust(key: string, sha: string) {
    try {
      await api('trust_cert', { service: key, sha256: sha });
      s[key + '_CERT_SHA256'] = sha;
      s[key + '_VERIFY_TLS'] = 'yes';
      tests[key] = { success: true, message: 'Certificate trusted. Test again to check the connection.' };
    } catch (e) {
      tests[key] = { success: false, message: (e as Error).message };
    }
  }

  function toggle(k: string) {
    s[k] = s[k] === 'yes' ? 'no' : 'yes';
  }

  onMount(load);
</script>

{#if error}<div class="mtc-msg mtc-err">{error}</div>{/if}
{#if saved}<div class="mtc-msg mtc-ok">{saved}</div>{/if}

{#each servers as sv}
  <section class="mtc-card">
    <div class="mtc-head">
      <h3>{sv.label}</h3>
      <span class="mtc-grow"></span>
      <label class="mtc-switch"><input type="checkbox" checked={s['ENABLE_' + sv.key] === 'yes'} onchange={() => toggle('ENABLE_' + sv.key)} /> On</label>
    </div>
    {#if s['ENABLE_' + sv.key] === 'yes'}
      <div class="mtc-grid">
        <label for="{sv.key}-url">Address</label>
        <div>
          <input id="{sv.key}-url" type="text" class:bad={field === sv.key + '_URL'} bind:value={s[sv.key + '_URL']} placeholder="http://192.168.1.10:32400" />
        </div>
        <label for="{sv.key}-tok">{sv.tokenLabel}</label>
        <div>
          <input id="{sv.key}-tok" type="password" autocomplete="off" bind:value={tokens[sv.token]} placeholder={stored[sv.token] && !clear[sv.token] ? 'saved - leave empty to keep' : 'not set'} />
          {#if stored[sv.token]}<label class="mtc-check"><input type="checkbox" bind:checked={clear[sv.token]} /> remove saved {sv.tokenLabel.toLowerCase()}</label>{/if}
        </div>
        <label for="{sv.key}-tls">Verify certificate</label>
        <div>
          <label class="mtc-check"><input id="{sv.key}-tls" type="checkbox" checked={s[sv.key + '_VERIFY_TLS'] === 'yes'} onchange={() => toggle(sv.key + '_VERIFY_TLS')} /> only for https; off lets anyone on the network pose as the server</label>
          {#if s[sv.key + '_CERT_SHA256']}
            <div class="mtc-sub">Trusted certificate: <code class="mtc-fp">{s[sv.key + '_CERT_SHA256']}</code> <button type="button" class="mtc-link" onclick={() => (s[sv.key + '_CERT_SHA256'] = '')}>forget</button></div>
          {/if}
        </div>
      </div>
      <button type="button" class="mtc-btn" disabled={testing[sv.key]} onclick={() => test(sv.key, sv.token)}>{testing[sv.key] ? 'Testing…' : 'Test connection'}</button>
      {#if tests[sv.key]}
        {@const r = tests[sv.key]}
        <div class="mtc-msg {r.success ? 'mtc-ok' : 'mtc-err'}">
          {r.message}
          {#if r.advice}<div class="mtc-sub">{r.advice}</div>{/if}
          {#if r.certificate}
            <div class="mtc-cert">
              <div>Fingerprint (SHA-256): <code class="mtc-fp">{r.certificate.sha256}</code></div>
              <div>Issued to {r.certificate.subject}{r.certificate.names?.length ? ' (' + r.certificate.names.join(', ') + ')' : ''}, by {r.certificate.issuer}, valid until {r.certificate.not_after}</div>
              <button type="button" class="mtc-btn primary" onclick={() => trust(sv.key, r.certificate.sha256)}>Trust this certificate</button>
            </div>
          {/if}
        </div>
        {#if r.streams?.length || r.libraries?.length}
          <ul class="mtc-list small">
            {#each [...(r.streams ?? []), ...(r.libraries ?? [])] as row}
              <li>
                <div class="mtc-name">{row.title ? row.title + ': ' : ''}{row.container}</div>
                <div class="mtc-path">→ {row.host}</div>
                <div class="mtc-meta"><span class={row.state === 'ok' ? 'mtc-ok' : 'mtc-warn'}>{row.text}</span></div>
              </li>
            {/each}
          </ul>
        {/if}
      {/if}
    {/if}
  </section>
{/each}

<section class="mtc-card">
  <h3>Storage</h3>
  <div class="mtc-grid">
    <label for="mtc-cache">Cache Root</label>
    <div>
      {#if root}
        <select id="mtc-cache" class:bad={field === 'CACHE_ROOT' || !!root.problem} bind:value={s.CACHE_ROOT}>
          {#if !root.listed}<option value={root.current}>{root.current} (not valid)</option>{/if}
          {#each root.pools as p}<option value={p}>{p}</option>{/each}
        </select>
        {#if root.problem && s.CACHE_ROOT === root.current}<div class="mtc-sub mtc-err">{root.problem}</div>{/if}
        {#if root.pools.length === 0}<div class="mtc-sub mtc-warn">No pool is mounted right now - is the array started?</div>{/if}
      {/if}
    </div>
    <label for="mtc-array">Array Root</label>
    <div><input id="mtc-array" type="text" class:bad={field === 'ARRAY_ROOT'} bind:value={s.ARRAY_ROOT} placeholder="/mnt/user" /></div>
  </div>
</section>

<section class="mtc-card">
  <h3>Docker path mappings</h3>
  <p class="mtc-sub">The path inside the media server's container, and the same folder on the host.</p>
  {#each maps as m, i}
    <div class="mtc-map">
      <input type="text" aria-label="Container path" class:bad={field === 'DOCKER_MAPPINGS'} bind:value={m.d} placeholder="/media" />
      <span>→</span>
      <input type="text" aria-label="Host path" class:bad={field === 'DOCKER_MAPPINGS'} bind:value={m.h} placeholder="/mnt/user/Media" />
      <button type="button" class="mtc-link" aria-label="Remove mapping" onclick={() => (maps = maps.filter((_, j) => j !== i))}>remove</button>
    </div>
  {/each}
  <button type="button" class="mtc-btn" onclick={() => (maps = [...maps, { d: '', h: '' }])}>Add mapping</button>
</section>

<section class="mtc-card">
  <h3>Behaviour</h3>
  <div class="mtc-grid">
    {#each numbers as [k, label, hint]}
      <label for="mtc-{k}">{label}</label>
      <div><input id="mtc-{k}" type="number" class="short" class:bad={field === k} bind:value={s[k]} /> <span class="mtc-sub">{hint}</span></div>
    {/each}
    <label for="mtc-types">Media file types</label>
    <div><input id="mtc-types" type="text" class:bad={field === 'MEDIA_FILETYPES'} bind:value={s.MEDIA_FILETYPES} placeholder=".mkv .mp4 .avi" /></div>
    <label for="mtc-excl">Excluded folders</label>
    <div><input id="mtc-excl" type="text" class:bad={field === 'EXCLUDE_DIRS'} bind:value={s.EXCLUDE_DIRS} placeholder="Kids, Extras" /></div>
    <label for="mtc-clean">Auto cleanup</label>
    <div>
      <select id="mtc-clean" class:bad={field === 'CLEANUP_MODE'} bind:value={s.CLEANUP_MODE}>
        <option value="none">Off</option>
        <option value="smart">Smart: when watched</option>
        <option value="days">After a number of days</option>
      </select>
    </div>
    {#if s.CLEANUP_MODE === 'smart'}
      <label for="mtc-mdd">Movie delay</label>
      <div><input id="mtc-mdd" type="number" class="short" class:bad={field === 'MOVIE_DELETE_DELAY'} bind:value={s.MOVIE_DELETE_DELAY} /> <span class="mtc-sub">seconds after a movie was watched</span></div>
      <label for="mtc-keep">Keep episodes</label>
      <div><input id="mtc-keep" type="number" class="short" class:bad={field === 'EPISODE_KEEP_PREVIOUS'} bind:value={s.EPISODE_KEEP_PREVIOUS} /> <span class="mtc-sub">previous episodes stay on the cache</span></div>
    {/if}
    {#if s.CLEANUP_MODE === 'days'}
      <label for="mtc-days">Days</label>
      <div><input id="mtc-days" type="number" class="short" class:bad={field === 'CACHE_MAX_DAYS'} bind:value={s.CACHE_MAX_DAYS} /></div>
    {/if}
    <span class="mtc-label">Options</span>
    <div>
      <label class="mtc-check"><input type="checkbox" checked={s.ENABLE_CACHE_EVICTION === 'yes'} onchange={() => toggle('ENABLE_CACHE_EVICTION')} /> make room when the cache is full (oldest first)</label>
      <label class="mtc-check"><input type="checkbox" checked={s.ENABLE_EPISODE_BATCHING === 'yes'} onchange={() => toggle('ENABLE_EPISODE_BATCHING')} /> cache long seasons in batches</label>
      <label class="mtc-check"><input type="checkbox" checked={s.ENABLE_NEXT_SEASON_PREFETCH === 'yes'} onchange={() => toggle('ENABLE_NEXT_SEASON_PREFETCH')} /> start the next season near the end of one</label>
    </div>
    {#if s.ENABLE_EPISODE_BATCHING === 'yes'}
      <label for="mtc-bs">Batch size</label>
      <div><input id="mtc-bs" type="number" class="short" class:bad={field === 'EPISODE_BATCH_SIZE'} bind:value={s.EPISODE_BATCH_SIZE} /> <span class="mtc-sub">episodes</span></div>
      <label for="mtc-bt">Tolerance</label>
      <div><input id="mtc-bt" type="number" class="short" class:bad={field === 'EPISODE_BATCH_TOLERANCE'} bind:value={s.EPISODE_BATCH_TOLERANCE} /> <span class="mtc-sub">seasons up to size + this are cached whole</span></div>
    {/if}
    {#if s.ENABLE_EPISODE_BATCHING === 'yes' || s.ENABLE_NEXT_SEASON_PREFETCH === 'yes'}
      <label for="mtc-bp">Prefetch</label>
      <div><input id="mtc-bp" type="number" class="short" class:bad={field === 'EPISODE_BATCH_PREFETCH'} bind:value={s.EPISODE_BATCH_PREFETCH} /> <span class="mtc-sub">start the next part when this many episodes remain</span></div>
    {/if}
  </div>
</section>

<div class="mtc-toolbar">
  <span class="mtc-grow"></span>
  <button type="button" class="mtc-btn" onclick={load}>Reset</button>
  <button type="button" class="mtc-btn primary" disabled={saving} onclick={save}>{saving ? 'Saving…' : 'Save'}</button>
</div>
