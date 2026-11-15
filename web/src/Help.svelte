<script lang="ts">
  import { HELP, shown } from './lib/help.svelte';

  let { k }: { k: string } = $props();
</script>

{#if shown[k]}<p class="mtc-help">{HELP[k]}</p>{/if}
