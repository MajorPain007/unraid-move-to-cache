<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from './lib/api';

  let text = $state('');
  let error = $state('');
  let box: HTMLPreElement | undefined = $state();

  async function load() {
    try {
      const r = await api<{ text: string }>('log');
      const atEnd = !box || box.scrollTop + box.clientHeight >= box.scrollHeight - 20;
      text = r.text || 'The log is empty.';
      error = '';
      if (atEnd) requestAnimationFrame(() => box && (box.scrollTop = box.scrollHeight));
    } catch (e) {
      error = (e as Error).message;
    }
  }

  onMount(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  });
</script>

{#if error}<div class="mtc-msg mtc-err">{error}</div>{/if}
<pre class="mtc-log" bind:this={box}>{text}</pre>
<button type="button" class="mtc-btn" onclick={load}>Refresh</button>
