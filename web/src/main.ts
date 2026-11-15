import { mount } from 'svelte';
import App from './App.svelte';
import './app.css';

function applyTheme(root: HTMLElement): void {
  let el = root.parentElement;
  let bg: string | null = null;
  while (el && el !== document.documentElement) {
    const c = getComputedStyle(el).backgroundColor;
    if (c && c !== 'transparent' && !/rgba\(.*,\s*0\)$/.test(c)) {
      bg = c;
      break;
    }
    el = el.parentElement;
  }
  bg = bg || getComputedStyle(document.body).backgroundColor || 'rgb(255,255,255)';
  const m = (bg.match(/\d+(\.\d+)?/g) || ['255', '255', '255']).map(Number);
  const lum = (0.2126 * m[0] + 0.7152 * m[1] + 0.0722 * m[2]) / 255;
  root.classList.toggle('mtc-light', lum > 0.55);
}

function boot(): void {
  const root = document.getElementById('mtc-app');
  if (!root) return;
  root.textContent = '';
  applyTheme(root);
  mount(App, { target: root });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
else boot();
