<script lang="ts">
  import { page } from '$app/state';
  import { onMount } from 'svelte';
  import {
    fetchJob,
    fetchJobs,
    fetchPreviewFrame,
    fetchPreviewNoise,
    runPreviewDiff,
    runServerDiff,
    setFrameOffset,
    uploadExternalPair
  } from '$lib/api';
  import {
    downloadDataUrl,
    renderAbsDiff,
    renderOverlay,
    renderSideBySide,
    urlToFrame
  } from '$lib/client-diff';
  import type { CompareMode, EncodeJob } from '$lib/types';

  const POSITIONS = [0.25, 0.5, 0.75] as const;

  let jobs = $state<EncodeJob[]>([]);
  let selectedId = $state<number | null>(null);
  let job = $state<EncodeJob | null>(null);
  let position = $state(0.5);
  let mode = $state<CompareMode>('overlay');
  let opacityB = $state(0.5);
  let gain = $state(2);
  let previewUrl = $state<string | null>(null);
  let absdiffImage = $state<string | null>(null);
  let ssimImage = $state<string | null>(null);
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

  // Preview scrub state
  let frameIndex = $state(0);
  let frameOffset = $state(0);
  let offsetInput = $state('0');
  let maxFrames = $state(150);
  let previewSourceUrl = $state<string | null>(null);
  let previewDestUrl = $state<string | null>(null);
  let frameInput = $state('0');
  let offsetLocked = $state(false);
  let sourceOob = $state(false);
  let sourceShortfall = $state(0);
  let usedFullSource = $state(false);
  let rawSourceIndex = $state(0);

  // Noise graph: source-center vs every dest frame
  let noiseValues = $state<number[]>([]);
  let noiseBestIndex = $state<number | null>(null);
  let noiseSuggestedOffset = $state<number | null>(null);
  let noiseBusy = $state(false);
  let noiseError = $state('');
  let sceneCuts = $state<number[]>([]);
  let selectionStart = $state(0);
  let selectionEnd = $state(0);
  let noiseScaleMax = $state(1);

  const isPreview = $derived(job?.kind === 'preview' && job?.status === 'preview_ready');

  const currentFrame = $derived(
    job?.frames.find((f) => Math.abs(f.position - position) < 0.01) ?? null
  );

  const hasContent = $derived(
    isPreview ? Boolean(previewSourceUrl && previewDestUrl) : Boolean(currentFrame)
  );

  const noiseMax = $derived(Math.max(1, noiseScaleMax));

  const offsetNote = $derived.by(() => {
    if (!offsetLocked) return '';
    if (sourceOob) {
      if (sourceShortfall < 0) {
        return `Source is short by ${Math.abs(sourceShortfall)} frame(s) at the start — showing first available source frame.`;
      }
      return `Source is short by ${sourceShortfall} frame(s) at the end — showing last available source frame.`;
    }
    if (usedFullSource) {
      return `Browsing with offset ${frameOffset >= 0 ? '+' : ''}${frameOffset}: source frame ${rawSourceIndex} (from original file).`;
    }
    return `Browsing with offset ${frameOffset >= 0 ? '+' : ''}${frameOffset}: source index = dest index + offset.`;
  });

  async function loadJobs() {
    jobs = await fetchJobs(100);
    const q = Number(page.url.searchParams.get('job'));
    if (q && jobs.some((j) => j.id === q)) {
      await selectJob(q);
    } else {
      const ready =
        jobs.find((j) => j.kind === 'preview' && j.status === 'preview_ready') ||
        jobs.find((j) => j.frames?.length);
      if (ready) await selectJob(ready.id);
    }
  }

  async function selectJob(id: number) {
    selectedId = id;
    error = '';
    absdiffImage = null;
    ssimImage = null;
    mse = null;
    ssim = null;
    psnr = null;
    previewSourceUrl = null;
    previewDestUrl = null;
    noiseValues = [];
    noiseBestIndex = null;
    noiseSuggestedOffset = null;
    noiseError = '';
    offsetLocked = false;
    job = await fetchJob(id);
    frameOffset = job.frame_offset ?? 0;
    frameIndex = 0;
    frameInput = '0';
    if (job.kind === 'preview' && job.status === 'preview_ready') {
      await loadPreviewPair();
      await loadNoiseGraph();
      await runDiff();
    } else if (job.frames.length) {
      const hasPos = job.frames.some((f) => Math.abs(f.position - position) < 0.01);
      if (!hasPos) position = job.frames[0].position;
      await renderClient();
      await runDiff();
    }
  }

  async function loadPreviewPair() {
    if (!job) return;
    try {
      const r = await fetchPreviewFrame(job.id, frameIndex, frameOffset);
      maxFrames = Math.max(1, r.usable_frame_count - 1);
      frameInput = String(frameIndex);
      previewSourceUrl = `data:image/png;base64,${r.source}`;
      previewDestUrl = `data:image/png;base64,${r.dest}`;
      await renderFromUrls(previewSourceUrl, previewDestUrl);
    } catch (e) {
      error = (e as Error).message;
    }
  }

  async function loadNoiseGraph() {
    if (!job) return;
    noiseBusy = true;
    noiseError = '';
    try {
      const r = await fetchPreviewNoise(job.id);
      noiseValues = r.values;
      noiseBestIndex = r.best_index;
      noiseSuggestedOffset = r.suggested_offset;
      maxFrames = Math.max(maxFrames, r.frame_count - 1);
    } catch (e) {
      noiseError = (e as Error).message;
      noiseValues = [];
    } finally {
      noiseBusy = false;
    }
  }

  async function renderFromUrls(src: string, dst: string) {
    const [a, b] = await Promise.all([urlToFrame(src), urlToFrame(dst)]);
    if (mode === 'side-by-side') previewUrl = await renderSideBySide(a, b);
    else if (mode === 'overlay') previewUrl = await renderOverlay(a, b, opacityB);
    else previewUrl = await renderAbsDiff(a, b, gain);
  }

  async function renderClient() {
    previewUrl = null;
    if (isPreview) {
      if (previewSourceUrl && previewDestUrl) {
        try {
          await renderFromUrls(previewSourceUrl, previewDestUrl);
        } catch (e) {
          error = (e as Error).message;
        }
      }
      return;
    }
    if (!currentFrame?.source_url || !currentFrame?.dest_url) return;
    try {
      await renderFromUrls(currentFrame.source_url, currentFrame.dest_url);
    } catch (e) {
      error = (e as Error).message;
    }
  }

  async function goToFrame(n: number) {
    const capped = Math.max(0, Math.min(maxFrames, Math.round(n)));
    frameIndex = capped;
    frameInput = String(capped);
    await loadPreviewPair();
  }

  async function onFrameScrub(v: number) {
    await goToFrame(v);
  }

  async function onFrameInputCommit() {
    const n = Number(frameInput);
    if (!Number.isFinite(n)) {
      frameInput = String(frameIndex);
      return;
    }
    await goToFrame(n);
  }

  async function onOffsetChange(v: number) {
    if (offsetLocked) return;
    frameOffset = v;
    if (job) {
      try {
        await setFrameOffset(job.id, v);
      } catch {
        /* keep local */
      }
    }
    await loadPreviewPair();
  }

  async function jumpToBestMatch() {
    if (noiseBestIndex == null) return;
    await goToFrame(noiseBestIndex);
  }

  async function lockOffsetFromNoise() {
    if (!job || noiseSuggestedOffset == null || noiseBestIndex == null) return;
    frameOffset = noiseSuggestedOffset;
    offsetLocked = true;
    try {
      await setFrameOffset(job.id, frameOffset);
    } catch {
      /* keep local */
    }
    await goToFrame(noiseBestIndex);
  }

  function unlockOffset() {
    offsetLocked = false;
  }

  async function runDiff() {
    if (!job) return;
    if (isPreview) {
      if (!previewSourceUrl || !previewDestUrl) return;
    } else if (!currentFrame?.source_url || !currentFrame?.dest_url) {
      return;
    }
    busy = true;
    error = '';
    try {
      const [abs, ssimR] = await Promise.all([
        isPreview
          ? runPreviewDiff(job.id, frameIndex, 'absdiff', frameOffset)
          : runServerDiff(job.id, position, 'absdiff'),
        isPreview
          ? runPreviewDiff(job.id, frameIndex, 'ssim_map', frameOffset)
          : runServerDiff(job.id, position, 'ssim_map')
      ]);
      absdiffImage = `data:image/png;base64,${abs.image}`;
      ssimImage = `data:image/png;base64,${ssimR.image}`;
      mse = abs.mse;
      ssim = abs.ssim;
      psnr = abs.psnr;
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
    void mode;
    void opacityB;
    void gain;
    void position;
    void currentFrame;
    void previewSourceUrl;
    void previewDestUrl;
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
              {j.kind} · {j.preset} · {j.status}
              {#if j.source_label}
                · {j.source_label}
              {/if}
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
    {:else if !hasContent && !isPreview}
      <div class="empty big">
        {#if ['extracting', 'encoding', 'queued', 'previewing'].includes(job.status)}
          Waiting for frames ({job.status})…
        {:else}
          No comparison frames for this job
          {#if job.error}<br /><small>{job.error}</small>{/if}
        {/if}
      </div>
    {:else if isPreview && !previewSourceUrl}
      <div class="empty big">
        {#if job.status === 'previewing' || job.status === 'queued'}
          Waiting for preview ({job.status})…
        {:else}
          Loading preview frames…
          {#if job.error}<br /><small>{job.error}</small>{/if}
        {/if}
      </div>
    {:else}
      {#if isPreview}
        <section class="preview-controls">
          <div class="control-block">
            <div class="control-head">
              <h3>Frame</h3>
              <p class="help">
                Scrub through the encoded preview clip. Type a frame number or drag the wide
                slider. Overlay / abs-diff use this frame against the source at the current
                offset.
              </p>
            </div>
            <div class="frame-row">
              <input
                class="frame-range"
                type="range"
                min="0"
                max={maxFrames}
                value={frameIndex}
                oninput={(e) => onFrameScrub(Number((e.currentTarget as HTMLInputElement).value))}
              />
              <div class="frame-num">
                <input
                  type="number"
                  min="0"
                  max={maxFrames}
                  bind:value={frameInput}
                  onchange={onFrameInputCommit}
                  onkeydown={(e) => {
                    if (e.key === 'Enter') onFrameInputCommit();
                  }}
                />
                <span class="frame-max">/ {maxFrames}</span>
              </div>
            </div>
          </div>

          <div class="control-block noise-block">
            <div class="control-head">
              <h3>Match noise</h3>
              <p class="help">
                One fixed frame from the <strong>center of the source snippet</strong> is
                compared to <strong>every frame</strong> of the encoded preview. The valley
                (lowest noise) is the matching encoded frame — lock that to set the offset for
                the whole clip.
              </p>
            </div>
            {#if noiseBusy}
              <div class="noise-loading">Computing noise graph…</div>
            {:else if noiseError}
              <div class="err">{noiseError}</div>
            {:else if noiseValues.length}
              <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
              <div
                class="noise-graph"
                onclick={(e) => {
                  const el = e.currentTarget as HTMLElement;
                  const rect = el.getBoundingClientRect();
                  const x = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
                  const idx = Math.round(x * (noiseValues.length - 1));
                  goToFrame(idx);
                }}
              >
                <svg viewBox={`0 0 ${Math.max(1, noiseValues.length - 1)} 100`} preserveAspectRatio="none">
                  <polyline
                    fill="none"
                    stroke="currentColor"
                    stroke-width="1.5"
                    vector-effect="non-scaling-stroke"
                    points={noiseValues
                      .map((v, i) => {
                        const y = Number.isFinite(v) ? 100 - (v / noiseMax) * 92 - 4 : 100;
                        return `${i},${y}`;
                      })
                      .join(' ')}
                  />
                  {#if noiseBestIndex != null}
                    <line
                      class="best-line"
                      x1={noiseBestIndex}
                      x2={noiseBestIndex}
                      y1="0"
                      y2="100"
                    />
                  {/if}
                  <line
                    class="cursor-line"
                    x1={frameIndex}
                    x2={frameIndex}
                    y1="0"
                    y2="100"
                  />
                </svg>
                <div class="noise-legend">
                  <span>Low noise = match</span>
                  {#if noiseBestIndex != null}
                    <span>Best: frame {noiseBestIndex}</span>
                  {/if}
                  <span>Click graph to jump</span>
                </div>
              </div>
              <div class="noise-actions">
                <button class="btn btn-ghost" disabled={noiseBestIndex == null} onclick={jumpToBestMatch}
                  >Jump to best match</button
                >
                <button
                  class="btn btn-primary narrow"
                  disabled={noiseSuggestedOffset == null}
                  onclick={lockOffsetFromNoise}
                >
                  Lock offset
                  {#if noiseSuggestedOffset != null}
                    ({noiseSuggestedOffset >= 0 ? `+${noiseSuggestedOffset}` : noiseSuggestedOffset})
                  {/if}
                </button>
                {#if offsetLocked}
                  <span class="align-badge">offset locked</span>
                  <button class="btn btn-ghost" onclick={unlockOffset}>Unlock</button>
                {/if}
              </div>
            {:else}
              <button class="btn btn-ghost" onclick={loadNoiseGraph}>Load noise graph</button>
            {/if}
          </div>

          <div class="control-block">
            <div class="control-head">
              <h3>Source offset</h3>
              <p class="help">
                Shifts which source frame pairs with the current encoded frame (±12). Use
                overlay at ~50% — when edges stop ghosting, you’re synced. Prefer
                <strong>Lock offset</strong> from the noise graph when the valley is clear.
              </p>
            </div>
            <div class="offset-row">
              <input
                class="offset-range"
                type="range"
                min="-12"
                max="12"
                value={frameOffset}
                disabled={offsetLocked}
                oninput={(e) =>
                  onOffsetChange(Number((e.currentTarget as HTMLInputElement).value))}
              />
              <span class="offset-val"
                >{frameOffset >= 0 ? `+${frameOffset}` : frameOffset} frames</span
              >
            </div>
          </div>
        </section>
      {/if}

      <div class="toolbar">
        {#if !isPreview}
          <div class="pos-group">
            {#each POSITIONS as p}
              <button
                class="btn btn-ghost"
                class:active={Math.abs(position - p) < 0.01}
                onclick={async () => {
                  position = p;
                  await runDiff();
                }}
              >
                {Math.round(p * 100)}%
              </button>
            {/each}
          </div>
        {/if}
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
            onclick={() =>
              downloadDataUrl(
                previewUrl!,
                isPreview ? `preview-${frameIndex}.png` : `compare-${position}.png`
              )}>Download</button
          >
        {:else}
          <div class="empty">Rendering…</div>
        {/if}
      </div>

      <div class="advanced">
        <div class="section-hdr">
          <h2>Server diff maps</h2>
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
        <div class="server-maps">
          <figure class="map-pane">
            <figcaption>Absdiff heatmap</figcaption>
            {#if absdiffImage}
              <img src={absdiffImage} alt="Absdiff heatmap" />
            {:else}
              <div class="empty">{busy ? 'Running…' : 'No map yet'}</div>
            {/if}
          </figure>
          <figure class="map-pane">
            <figcaption>SSIM map</figcaption>
            {#if ssimImage}
              <img src={ssimImage} alt="SSIM disparity map" />
            {:else}
              <div class="empty">{busy ? 'Running…' : 'No map yet'}</div>
            {/if}
          </figure>
        </div>
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
  .preview-controls {
    display: flex;
    flex-direction: column;
    gap: 16px;
    margin-bottom: 18px;
    padding: 14px 16px;
    border: 1px solid var(--border);
    border-radius: var(--r);
    background: var(--bg);
  }
  .control-block {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .control-head h3 {
    margin: 0 0 4px;
    font-size: 13px;
    font-weight: 700;
  }
  .help {
    margin: 0;
    font-size: 12px;
    line-height: 1.45;
    color: var(--muted);
  }
  .frame-row {
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 12px;
    align-items: center;
  }
  .frame-range {
    width: 100%;
    height: 28px;
    cursor: pointer;
  }
  .frame-num {
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .frame-num input {
    width: 72px;
    padding: 6px 8px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: transparent;
    color: inherit;
    font-size: 14px;
    font-variant-numeric: tabular-nums;
  }
  .frame-max {
    font-size: 12px;
    color: var(--muted);
  }
  .offset-row {
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 12px;
    align-items: center;
  }
  .offset-range {
    width: 100%;
    height: 24px;
  }
  .offset-val {
    font-size: 13px;
    font-variant-numeric: tabular-nums;
    min-width: 6.5em;
    text-align: right;
  }
  .noise-graph {
    position: relative;
    height: 88px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: rgba(0, 0, 0, 0.25);
    color: var(--accent2, #7dd3fc);
    cursor: crosshair;
    overflow: hidden;
  }
  .noise-graph svg {
    display: block;
    width: 100%;
    height: 100%;
  }
  .noise-graph .best-line {
    stroke: var(--ok, #3dd68c);
    stroke-width: 1;
    stroke-dasharray: 3 2;
    vector-effect: non-scaling-stroke;
  }
  .noise-graph .cursor-line {
    stroke: var(--warn, #f5a524);
    stroke-width: 1.5;
    vector-effect: non-scaling-stroke;
  }
  .noise-legend {
    position: absolute;
    left: 8px;
    bottom: 4px;
    right: 8px;
    display: flex;
    justify-content: space-between;
    gap: 8px;
    font-size: 10px;
    color: var(--muted);
    pointer-events: none;
  }
  .noise-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }
  .noise-loading {
    font-size: 12px;
    color: var(--muted);
    padding: 12px 0;
  }
  .align-badge {
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 999px;
    background: rgba(61, 214, 140, 0.15);
    color: var(--ok, #3dd68c);
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
  .advanced .section-hdr {
    margin-bottom: 8px;
  }
  .server-maps {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
    margin-top: 12px;
  }
  .map-pane {
    margin: 0;
    border: 1px solid var(--border);
    border-radius: var(--r);
    overflow: hidden;
    background: var(--bg);
  }
  .map-pane figcaption {
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--muted);
    padding: 8px 10px;
    border-bottom: 1px solid var(--border);
  }
  .map-pane img {
    display: block;
    width: 100%;
  }
  .map-pane .empty {
    padding: 40px 12px;
  }
  @media (max-width: 900px) {
    .server-maps {
      grid-template-columns: 1fr;
    }
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
    .frame-row {
      grid-template-columns: 1fr;
    }
  }
</style>
