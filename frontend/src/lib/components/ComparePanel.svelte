<script lang="ts">
  import { page } from '$app/state';
  import { onMount } from 'svelte';
  import { fetchJob, fetchJobs, runServerDiff, uploadExternalPair } from '$lib/api';
  import {
    downloadDataUrl,
    renderAbsDiff,
    renderOverlay,
    renderSideBySide,
    urlToFrame
  } from '$lib/client-diff';
  import type { CompareMode, EncodeJob, ServerDiffMode } from '$lib/types';

  const POSITIONS = [0.25, 0.5, 0.75] as const;

  let jobs = $state<EncodeJob[]>([]);
  let selectedId = $state<number | null>(null);
  let job = $state<EncodeJob | null>(null);
  let position = $state(0.5);
  let mode = $state<CompareMode>('overlay');
  let opacityB = $state(0.5);
  let gain = $state(2);
  let previewUrl = $state<string | null>(null);
  let serverMode = $state<ServerDiffMode>('absdiff');
  let serverImage = $state<string | null>(null);
  let mse = $state<number | null>(null);
  let ssim = $state<number | null>(null);
  let psnr = $state<number | null>(null);
  let busy = $state(false);
  let error = $state('');
  let showExternal = $state(false);
  let sourceFile = $state<File | null>(null);
  let destFile = $state<File | null>(null);
  let uploadPct = $state(0);
  let uploadLabel = $state('');

  const currentFrame = $derived(
    job?.frames.find((f) => Math.abs(f.position - position) < 0.01) ?? null
  );

  async function loadJobs() {
    jobs = await fetchJobs(100);
    const q = Number(page.url.searchParams.get('job'));
    if (q && jobs.some((j) => j.id === q)) {
      await selectJob(q);
    } else {
      const withFrames = jobs.find((j) => j.frames?.length);
      if (withFrames) await selectJob(withFrames.id);
    }
  }

  async function selectJob(id: number) {
    selectedId = id;
    error = '';
    serverImage = null;
    mse = null;
    ssim = null;
    psnr = null;
    job = await fetchJob(id);
    if (job.frames.length) {
      const hasPos = job.frames.some((f) => Math.abs(f.position - position) < 0.01);
      if (!hasPos) position = job.frames[0].position;
    }
    await renderClient();
  }

  async function renderClient() {
    previewUrl = null;
    if (!currentFrame?.source_url || !currentFrame?.dest_url) return;
    try {
      const [a, b] = await Promise.all([
        urlToFrame(currentFrame.source_url),
        urlToFrame(currentFrame.dest_url)
      ]);
      if (mode === 'side-by-side') previewUrl = await renderSideBySide(a, b);
      else if (mode === 'overlay') previewUrl = await renderOverlay(a, b, opacityB);
      else previewUrl = await renderAbsDiff(a, b, gain);
    } catch (e) {
      error = (e as Error).message;
    }
  }

  async function runDiff() {
    if (!job) return;
    busy = true;
    error = '';
    try {
      const r = await runServerDiff(job.id, position, serverMode);
      serverImage = `data:image/png;base64,${r.image}`;
      mse = r.mse;
      ssim = r.ssim;
      psnr = r.psnr;
    } catch (e) {
      error = (e as Error).message;
    } finally {
      busy = false;
    }
  }

  async function submitExternal() {
    if (!sourceFile || !destFile) {
      error = 'Select both source and encoded videos';
      return;
    }
    busy = true;
    error = '';
    try {
      const id = await uploadExternalPair(sourceFile, destFile, (side, pct) => {
        uploadLabel = side;
        uploadPct = pct;
      });
      // Poll until frames ready
      for (let i = 0; i < 60; i++) {
        await new Promise((r) => setTimeout(r, 1000));
        const j = await fetchJob(id);
        if (j.status === 'done' && j.frames.length) {
          jobs = await fetchJobs(100);
          await selectJob(id);
          showExternal = false;
          sourceFile = null;
          destFile = null;
          return;
        }
        if (j.status === 'failed') throw new Error(j.error || 'Extract failed');
      }
      throw new Error('Timed out waiting for frames');
    } catch (e) {
      error = (e as Error).message;
    } finally {
      busy = false;
    }
  }

  $effect(() => {
    // re-render when mode / opacity / gain / position / job frame changes
    void mode;
    void opacityB;
    void gain;
    void position;
    void currentFrame;
    renderClient();
  });

  onMount(() => {
    loadJobs().catch((e) => (error = e.message));
  });
</script>

<div class="compare">
  <aside class="sidebar">
    <div class="section-hdr">
      <h2>Jobs</h2>
      <button class="btn btn-ghost" onclick={() => (showExternal = !showExternal)}>
        {showExternal ? 'Hide upload' : 'External'}
      </button>
    </div>

    {#if showExternal}
      <div class="external">
        <p class="hint">Upload source + encoded pair (encoding done elsewhere).</p>
        <label>Source</label>
        <input
          type="file"
          accept="video/*"
          onchange={(e) => {
            sourceFile = (e.currentTarget as HTMLInputElement).files?.[0] ?? null;
          }}
        />
        <label>Encoded</label>
        <input
          type="file"
          accept="video/*"
          onchange={(e) => {
            destFile = (e.currentTarget as HTMLInputElement).files?.[0] ?? null;
          }}
        />
        <button class="btn btn-primary" disabled={busy} onclick={submitExternal}>
          {busy ? `Uploading ${uploadLabel} ${uploadPct}%…` : 'Compare pair'}
        </button>
      </div>
    {/if}

    <div class="job-list">
      {#if !jobs.length}
        <div class="empty">No jobs yet</div>
      {:else}
        {#each jobs as j}
          <button
            class="job-item"
            class:active={selectedId === j.id}
            onclick={() => selectJob(j.id)}
          >
            <div class="job-name">{j.filename}</div>
            <div class="job-meta">
              {j.origin} · {j.status}
              {#if j.frames?.length}
                · {j.frames.length} frames
              {/if}
            </div>
          </button>
        {/each}
      {/if}
    </div>
  </aside>

  <main class="viewer">
    {#if error}
      <div class="err">{error}</div>
    {/if}

    {#if !job}
      <div class="empty big">Select a job with comparison frames</div>
    {:else if !job.frames.length}
      <div class="empty big">
        {#if job.status === 'extracting' || job.status === 'encoding' || job.status === 'queued'}
          Waiting for frames ({job.status})…
        {:else}
          No comparison frames for this job
          {#if job.error}<br /><small>{job.error}</small>{/if}
        {/if}
      </div>
    {:else}
      <div class="toolbar">
        <div class="pos-group">
          {#each POSITIONS as p}
            <button
              class="btn btn-ghost"
              class:active={Math.abs(position - p) < 0.01}
              onclick={() => (position = p)}
            >
              {Math.round(p * 100)}%
            </button>
          {/each}
        </div>
        <div class="mode-group">
          <button
            class="btn btn-ghost"
            class:active={mode === 'side-by-side'}
            onclick={() => (mode = 'side-by-side')}>Side by side</button
          >
          <button
            class="btn btn-ghost"
            class:active={mode === 'overlay'}
            onclick={() => (mode = 'overlay')}>Overlay</button
          >
          <button
            class="btn btn-ghost"
            class:active={mode === 'abs-diff'}
            onclick={() => (mode = 'abs-diff')}>Abs diff</button
          >
        </div>
      </div>

      {#if mode === 'overlay'}
        <label class="slider"
          >Dest opacity
          <input type="range" min="0" max="1" step="0.05" bind:value={opacityB} />
          {opacityB.toFixed(2)}
        </label>
      {/if}
      {#if mode === 'abs-diff'}
        <label class="slider"
          >Gain
          <input type="range" min="1" max="8" step="0.5" bind:value={gain} />
          {gain}
        </label>
      {/if}

      <div class="preview checker">
        {#if previewUrl}
          <img src={previewUrl} alt="Comparison" />
          <button
            class="btn btn-ghost dl"
            onclick={() => downloadDataUrl(previewUrl!, `compare-${position}.png`)}
            >Download</button
          >
        {:else}
          <div class="empty">Rendering…</div>
        {/if}
      </div>

      <div class="advanced">
        <h2>Server diff maps</h2>
        <div class="mode-group">
          <button
            class="btn btn-ghost"
            class:active={serverMode === 'absdiff'}
            onclick={() => (serverMode = 'absdiff')}>Absdiff heatmap</button
          >
          <button
            class="btn btn-ghost"
            class:active={serverMode === 'ssim_map'}
            onclick={() => (serverMode = 'ssim_map')}>SSIM map</button
          >
          <button class="btn btn-primary narrow" disabled={busy} onclick={runDiff}>
            {busy ? 'Running…' : 'Run'}
          </button>
        </div>
        {#if mse != null && ssim != null}
          <div class="metrics">
            <div class="metric"><span class="metric-label">SSIM</span> {ssim.toFixed(4)}</div>
            <div class="metric">
              <span class="metric-label">PSNR</span>
              {psnr != null ? `${psnr.toFixed(2)} dB` : '∞'}
            </div>
            <div class="metric"><span class="metric-label">MSE</span> {mse.toFixed(2)}</div>
          </div>
        {/if}
        {#if serverImage}
          <div class="preview">
            <img src={serverImage} alt="Server diff" />
          </div>
        {/if}
      </div>
    {/if}
  </main>
</div>

<style>
  .compare {
    flex: 1;
    display: grid;
    grid-template-columns: 280px 1fr;
    min-height: 0;
    overflow: hidden;
  }
  .sidebar {
    border-right: 1px solid var(--border);
    padding: 16px;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .viewer {
    padding: 20px;
    overflow-y: auto;
  }
  .section-hdr {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .section-hdr h2,
  .advanced h2 {
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
  }
  .hint {
    font-size: 12px;
    color: var(--muted);
    margin-bottom: 8px;
  }
  .external {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: var(--r);
    padding: 12px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .external label {
    font-size: 11px;
    color: var(--muted);
  }
  .external input[type='file'] {
    font-size: 12px;
    color: var(--text);
  }
  .job-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .job-item {
    text-align: left;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 12px;
    color: var(--text);
    cursor: pointer;
  }
  .job-item.active {
    border-color: var(--accent);
    background: rgba(108, 99, 255, 0.1);
  }
  .job-name {
    font-size: 13px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .job-meta {
    font-size: 11px;
    color: var(--muted);
    margin-top: 2px;
  }
  .toolbar {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    margin-bottom: 14px;
  }
  .pos-group,
  .mode-group {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .btn {
    border: none;
    padding: 8px 12px;
    border-radius: var(--r);
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
  }
  .btn-ghost {
    background: transparent;
    border: 1px solid var(--border);
    color: var(--muted);
  }
  .btn-ghost.active {
    border-color: var(--accent);
    color: var(--accent);
    background: rgba(108, 99, 255, 0.1);
  }
  .btn-primary {
    background: var(--accent);
    color: #fff;
  }
  .btn-primary.narrow {
    width: auto;
  }
  .btn:disabled {
    opacity: 0.4;
  }
  .slider {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 12px;
    color: var(--muted);
    margin-bottom: 14px;
  }
  .preview {
    position: relative;
    border: 1px solid var(--border);
    border-radius: var(--r);
    overflow: hidden;
    margin-bottom: 20px;
  }
  .preview.checker {
    background-image:
      linear-gradient(45deg, #1a1d27 25%, transparent 25%),
      linear-gradient(-45deg, #1a1d27 25%, transparent 25%),
      linear-gradient(45deg, transparent 75%, #1a1d27 75%),
      linear-gradient(-45deg, transparent 75%, #1a1d27 75%);
    background-size: 20px 20px;
    background-position:
      0 0,
      0 10px,
      10px -10px,
      -10px 0;
    background-color: #0a0c12;
  }
  .preview img {
    display: block;
    max-width: 100%;
    margin: 0 auto;
  }
  .preview .dl {
    position: absolute;
    top: 8px;
    right: 8px;
  }
  .advanced {
    border-top: 1px solid var(--border);
    padding-top: 16px;
  }
  .metrics {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin: 14px 0;
    font-size: 18px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    color: var(--text);
    line-height: 1.35;
  }
  .metric-label {
    display: inline-block;
    min-width: 4.5em;
    color: var(--accent2);
    font-weight: 700;
  }
  .empty {
    text-align: center;
    color: var(--muted);
    padding: 24px 0;
  }
  .empty.big {
    padding: 80px 20px;
  }
  .err {
    background: rgba(239, 68, 68, 0.1);
    color: var(--danger);
    border: 1px solid rgba(239, 68, 68, 0.2);
    padding: 10px 12px;
    border-radius: 8px;
    margin-bottom: 12px;
    font-size: 13px;
  }
  @media (max-width: 768px) {
    .compare {
      grid-template-columns: 1fr;
    }
  }
</style>
