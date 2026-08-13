<script lang="ts">
  import '../app.css';
  import { page } from '$app/state';

  let { children } = $props();

  const tab = $derived(page.url.pathname.startsWith('/compare') ? 'compare' : 'encode');
</script>

<header>
  <h1>Encoder</h1>
  <span class="badge">HandBrake</span>
  <nav>
    <a href="/" class:active={tab === 'encode'}>Encode</a>
    <a href="/compare" class:active={tab === 'compare'}>Compare</a>
  </nav>
</header>

<main class="shell">
  {@render children()}
</main>

<style>
  header {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 14px 24px;
    display: flex;
    align-items: center;
    gap: 10px;
    flex-shrink: 0;
  }
  header h1 {
    font-size: 20px;
    font-weight: 600;
  }
  .badge {
    background: var(--accent);
    color: #fff;
    font-size: 10px;
    padding: 2px 8px;
    border-radius: 99px;
    font-weight: 700;
    text-transform: uppercase;
  }
  nav {
    margin-left: auto;
    display: flex;
    gap: 6px;
  }
  nav a {
    padding: 6px 14px;
    border-radius: 8px;
    font-size: 13px;
    font-weight: 600;
    color: var(--muted);
    border: 1px solid transparent;
  }
  nav a:hover {
    color: var(--text);
  }
  nav a.active {
    color: var(--accent);
    border-color: var(--accent);
    background: rgba(108, 99, 255, 0.1);
  }
  .shell {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-height: 0;
    overflow: hidden;
  }
</style>
