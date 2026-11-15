<script lang="ts">
  import { ICONS } from './lib/icons';

  let { name }: { name: string } = $props();
</script>

<svg viewBox="0 0 24 24" class="mtc-i" aria-hidden="true">{@html ICONS[name] ?? ''}</svg>
