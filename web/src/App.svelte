<script lang="ts">
  import Activity from './Activity.svelte';
  import Files from './Files.svelte';
  import Settings from './Settings.svelte';
  import Log from './Log.svelte';

  const tabs = ['Activity', 'Cached files', 'Settings', 'Log'] as const;
  let tab = $state<(typeof tabs)[number]>(tabs[0]);
</script>

<div class="mtc-tabs" role="tablist">
  {#each tabs as t}
    <button type="button" role="tab" aria-selected={tab === t} class:on={tab === t} onclick={() => (tab = t)}>{t}</button>
  {/each}
</div>

{#if tab === 'Activity'}
  <Activity />
{:else if tab === 'Cached files'}
  <Files />
{:else if tab === 'Settings'}
  <Settings />
{:else}
  <Log />
{/if}
