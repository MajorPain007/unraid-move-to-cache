<script lang="ts">
  let { title, text, ok, onok, oncancel }: { title: string; text: string; ok: string; onok: () => void; oncancel: () => void } = $props();
</script>

<div class="mtc-overlay" role="presentation" onclick={(e) => e.target === e.currentTarget && oncancel()}>
  <div class="mtc-dialog" role="dialog" aria-modal="true" aria-label={title}>
    <h3>{title}</h3>
    <p>{text}</p>
    <div class="mtc-actions">
      <button type="button" class="mtc-btn" onclick={oncancel}>Cancel</button>
      <button type="button" class="mtc-btn primary" onclick={onok}>{ok}</button>
    </div>
  </div>
</div>
