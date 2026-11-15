<script lang="ts">
  import type { Snippet } from 'svelte';
  import Icon from './Icon.svelte';
  import { shown } from './lib/help.svelte';

  let { k, children }: { k: string; children: Snippet } = $props();
</script>

<button type="button" class="mtc-label mtc-tip" class:on={shown[k]} aria-expanded={!!shown[k]} onclick={() => (shown[k] = !shown[k])}>
  {@render children()}<Icon name="info" />
</button>
