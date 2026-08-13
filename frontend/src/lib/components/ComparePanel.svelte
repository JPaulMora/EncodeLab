<script lang="ts">
  import { page } from '$app/state';
  import { onMount } from 'svelte';
  import {
    computePreviewNoiseScore,
    deleteJob,
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
    renderSubtract,
    urlToFrame
  } from '$lib/client-diff';
  import type { CompareMode, EncodeJob } from '$lib/types';

  const POSITIONS = [0.25, 0.5, 0.75] as const;

  const METRIC_HINTS = {
    ssim:
      'Structural similarity of this frame pair. Range 0–1 (1 = identical). Higher is better. ≥0.98 excellent · 0.95–0.98 typical good encode · 0.90–0.95 visible loss · <0.90 often misaligned or a very aggressive preset.',
    psnr:
      'Peak signal-to-noise from MSE, in dB. Range 0–∞ (∞ = identical). Higher is better. ≥40 dB excellent · 35–40 good · 30–35 typical lossy · <30 visible damage or frames not aligned.',
    mse:
      'Mean squared pixel error on 8-bit color (0–65025). 0 = identical. Lower is better. <10 excellent · 10–40 typical lossy · 40–200 noticeable · hundreds+ usually means the frames are not aligned.',
    compression:
      'Output size ÷ source size. 1/10 means the encode is ~10× smaller. Lower (higher 1/N) is more compression; “good” depends on the preset.',
    noise:
      'Mean SSIM across scored frames (0–1, 1 = identical). ≥0.98 excellent · 0.95–0.98 typical good encode. ± is frame-to-frame spread — a large ± often means leftover misalignment.',
    encode: 'HandBrake wall-clock time for this job. Not a quality metric.',
    offset: 'Locked source↔dest frame offset for this comparison. Restored when you reopen the job.'
  } as const;

  let jobs = $state<EncodeJob[]>([]);
  let selectedId = $state<number | null>(null);
  let job = $state<EncodeJob | null>(null);
  let position = $state(0.5);
  let mode = $state<CompareMode>('overlay');
  let opacityB = $state(0.5);
  let gain = $state(2);
  let previewUrl = $state<string | null>(null);
  let incomingUrl = $state<string | null>(null);
  let incomingSeq = $state(0);
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
  let noiseRequestId = 0;
  let sceneCuts = $state<number[]>([]);
  let selectionStart = $state(0);
  let selectionEnd = $state(0);
  let noiseScaleMax = $state(1);
  let scoreBusy = $state(false);
  let scoreError = $state('');
  let scoreRequestId = 0;
  let deletingId = $state<number | null>(null);
  let previewRequestId = 0;
  let renderGen = 0;
  let diffRequestId = 0;

  const isPreview = $derived(job?.kind === 'preview' && job?.status === 'preview_ready');

  function truncate(s: string, max: number): string {
    if (s.length <= max) return s;
    return `${s.slice(0, Math.max(0, max - 1))}…`;
  }

  function formatDuration(secs: number | null | undefined): string {
    if (secs == null || !Number.isFinite(secs)) return '—';
    const s = Math.max(0, Math.round(secs));
    const m = Math.floor(s / 60);
    const r = s % 60;
    if (m >= 60) {
      const h = Math.floor(m / 60);
      return `${h}h ${m % 60}m`;
    }
    if (m > 0) return `${m}m ${r}s`;
    return `${r}s`;
  }

  function formatNoise(j: EncodeJob): string {
    if (j.noise_ssim_mean == null) return '—';
    const m = j.noise_ssim_mean;
    const std = j.noise_ssim_std;
    if (std != null && std > 0) return `${m.toFixed(3)}±${std.toFixed(3)}`;
    return m.toFixed(3);
  }

  function formatCompression(j: EncodeJob): string {
    if (j.compression_ratio == null || j.compression_ratio <= 0) return '—';
    const r = j.compression_ratio; // output / source
    const inv = 1 / r;
    if (inv >= 2) return `1/${Math.round(inv)}`;
    return r.toFixed(3);
  }

  function formatOffset(j: EncodeJob): string {
    const n = j.frame_offset ?? 0;
    const sign = n > 0 ? '+' : '';
    return j.offset_locked ? `${sign}${n} locked` : `${sign}${n}`;
  }

  function jobMetaLine(j: EncodeJob): string {
    const parts = [`${j.kind}`, j.preset, j.status];
    if (j.source_label) parts.push(j.source_label);
    if (j.frames?.length) parts.push(`${j.frames.length} frames`);
    return parts.join(' · ');
  }

  function isRunning(j: EncodeJob): boolean {
    return ['queued', 'encoding', 'previewing', 'extracting'].includes(j.status);
  }

  async function removeJob(j: EncodeJob) {
    if (isRunning(j) || deletingId != null) return;
    const extra =
      j.kind === 'encode' && j.origin !== 'external'
        ? ' This also deletes the encoded output.'
        : '';
    if (!confirm(`Delete “${j.filename}”?${extra}`)) return;
    deletingId = j.id;
    error = '';
    try {
      await deleteJob(j.id);
      const idx = jobs.findIndex((x) => x.id === j.id);
      const remaining = jobs.filter((x) => x.id !== j.id);
      jobs = remaining;
      if (selectedId === j.id) {
        const next = remaining[idx] ?? remaining[Math.max(0, idx - 1)] ?? null;
        if (next) await selectJob(next.id);
        else {
          noiseRequestId += 1;
          scoreRequestId += 1;
          selectedId = null;
          job = null;
        }
      }
    } catch (e) {
      error = (e as Error).message;
    } finally {
      deletingId = null;
    }
  }

  const currentFrame = $derived(
    job?.frames.find((f) => Math.abs(f.position - position) < 0.01) ?? null
  );

  const hasContent = $derived(
    isPreview
      ? Boolean((previewSourceUrl && previewDestUrl) || previewUrl)
      : Boolean(currentFrame)
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
      return `Browsing with offset ${frameOffset >= 0 ? '+' : ''}${frameOffset}: source past clip pad — frame from original file.`;
    }
    return `Browsing with offset ${frameOffset >= 0 ? '+' : ''}${frameOffset}: source = pad + dest + offset.`;
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
    // Invalidate any in-flight work from a previous job
    noiseRequestId += 1;
    scoreRequestId += 1;
    previewRequestId += 1;
    renderGen += 1;
    diffRequestId += 1;
    selectedId = id;
    error = '';
    incomingUrl = null;
    mse = null;
    ssim = null;
    psnr = null;
    noiseValues = [];
    noiseBestIndex = null;
    noiseSuggestedOffset = null;
    noiseError = '';
    noiseBusy = false;
    scoreBusy = false;
    scoreError = '';
    sceneCuts = [];
    selectionStart = 0;
    selectionEnd = 0;
    noiseScaleMax = 1;
    offsetLocked = false;
    sourceOob = false;
    sourceShortfall = 0;
    usedFullSource = false;
    job = await fetchJob(id);
    frameOffset = job.frame_offset ?? 0;
    offsetInput = String(frameOffset);
    offsetLocked = Boolean(job.offset_locked);
    frameIndex = 0;
    frameInput = '0';
    if (job.kind === 'preview' && job.status === 'preview_ready') {
      // Show frames immediately; noise graph/score run in the background
      await loadPreviewPair();
      void runDiff();
      void loadNoiseGraph({ autoJump: true });
    } else if (job.frames.length) {
      const hasPos = job.frames.some((f) => Math.abs(f.position - position) < 0.01);
      if (!hasPos) position = job.frames[0].position;
      await renderClient();
      void runDiff();
    }
  }

  async function loadPreviewPair() {
    if (!job) return;
    const jobId = job.id;
    const req = ++previewRequestId;
    try {
      const r = await fetchPreviewFrame(job.id, frameIndex, frameOffset);
      if (req !== previewRequestId || selectedId !== jobId) return;
      maxFrames = Math.max(1, r.usable_frame_count - 1);
      frameInput = String(frameIndex);
      sourceOob = r.source_oob;
      sourceShortfall = r.source_shortfall;
      usedFullSource = r.used_full_source;
      rawSourceIndex = r.raw_source_index;
      const src = `data:image/png;base64,${r.source}`;
      const dst = `data:image/png;base64,${r.dest}`;
      previewSourceUrl = src;
      previewDestUrl = dst;
      await renderFromUrls(src, dst, req);
    } catch (e) {
      if (req !== previewRequestId || selectedId !== jobId) return;
      error = (e as Error).message;
    }
  }

  async function loadNoiseGraph(opts: { autoJump?: boolean } = {}) {
    if (!job) return;
    const jobId = job.id;
    const req = ++noiseRequestId;
    const startFrame = frameIndex;
    const startOffset = frameOffset;
    noiseBusy = true;
    noiseError = '';
    try {
      const r = await fetchPreviewNoise(jobId);
      if (req !== noiseRequestId || selectedId !== jobId) return;

      noiseValues = r.values;
      noiseBestIndex = r.best_index;
      noiseSuggestedOffset = r.suggested_offset;
      sceneCuts = r.scene_cuts ?? [];
      selectionStart = r.selection_start ?? 0;
      selectionEnd = r.selection_end ?? Math.max(0, r.frame_count - 1);
      noiseScaleMax = r.scale_max || 1;
      maxFrames = Math.max(maxFrames, r.frame_count - 1);

      const stillAtStart = frameIndex === startFrame && frameOffset === startOffset;
      if (opts.autoJump && stillAtStart && r.best_index != null) {
        await goToFrame(r.best_index);
      }
    } catch (e) {
      if (req !== noiseRequestId || selectedId !== jobId) return;
      noiseError = (e as Error).message;
      noiseValues = [];
    } finally {
      if (req === noiseRequestId) noiseBusy = false;
    }
  }

  async function renderFromUrls(src: string, dst: string, req = previewRequestId) {
    const gen = ++renderGen;
    const [a, b] = await Promise.all([urlToFrame(src), urlToFrame(dst)]);
    if (gen !== renderGen || req !== previewRequestId) return;
    let url: string;
    if (mode === 'side-by-side') url = await renderSideBySide(a, b);
    else if (mode === 'overlay') url = await renderOverlay(a, b, opacityB);
    else if (mode === 'subtract') url = await renderSubtract(a, b, gain);
    else url = await renderAbsDiff(a, b, gain);
    if (gen !== renderGen || req !== previewRequestId) return;
    presentPreview(url);
  }

  function presentPreview(url: string) {
    if (url === previewUrl) {
      incomingUrl = null;
      return;
    }
    incomingSeq += 1;
    incomingUrl = url;
  }

  function commitIncoming(seq: number) {
    if (seq !== incomingSeq || !incomingUrl) return;
    previewUrl = incomingUrl;
    incomingUrl = null;
  }

  async function renderClient() {
    const src = isPreview ? previewSourceUrl : currentFrame?.source_url;
    const dst = isPreview ? previewDestUrl : currentFrame?.dest_url;
    if (!src || !dst) return;
    try {
      await renderFromUrls(src, dst);
    } catch (e) {
      error = (e as Error).message;
    }
  }

  async function goToFrame(n: number) {
    const capped = Math.max(0, Math.min(maxFrames, Math.round(n)));
    frameIndex = capped;
    frameInput = String(capped);
    await loadPreviewPair();
    void runDiff();
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

  async function persistOffset(locked?: boolean) {
    if (!job) return;
    try {
      const updated = await setFrameOffset(job.id, frameOffset, locked);
      job = updated;
      jobs = jobs.map((j) => (j.id === updated.id ? updated : j));
    } catch {
      /* keep local */
    }
  }

  async function onOffsetInputCommit() {
    if (offsetLocked) {
      offsetInput = String(frameOffset);
      return;
    }
    const n = Number(offsetInput);
    if (!Number.isFinite(n)) {
      offsetInput = String(frameOffset);
      return;
    }
    frameOffset = Math.round(n);
    offsetInput = String(frameOffset);
    await persistOffset();
    await loadPreviewPair();
  }

  async function applySuggestedOffset() {
    if (noiseSuggestedOffset == null) return;
    frameOffset = noiseSuggestedOffset;
    offsetInput = String(frameOffset);
    await persistOffset();
    if (noiseBestIndex != null) await goToFrame(noiseBestIndex);
    else await loadPreviewPair();
  }

  async function runNoiseScore() {
    if (!job || !isPreview) return;
    const jobId = job.id;
    const req = ++scoreRequestId;
    scoreBusy = true;
    scoreError = '';
    try {
      const r = await computePreviewNoiseScore(jobId, frameOffset);
      if (req !== scoreRequestId || selectedId !== jobId) return;
      job = r.job;
      jobs = jobs.map((j) => (j.id === r.job.id ? r.job : j));
    } catch (e) {
      if (req !== scoreRequestId || selectedId !== jobId) return;
      scoreError = (e as Error).message;
    } finally {
      if (req === scoreRequestId) scoreBusy = false;
    }
  }

  async function lockOffset() {
    if (!job) return;
    const n = Number(offsetInput);
    if (Number.isFinite(n)) {
      frameOffset = Math.round(n);
      offsetInput = String(frameOffset);
    }
    offsetLocked = true;
    await persistOffset(true);
    if (noiseBestIndex != null) await goToFrame(noiseBestIndex);
    else await loadPreviewPair();
  }

  async function unlockOffset() {
    offsetLocked = false;
    await persistOffset(false);
  }

  async function jumpToBestMatch() {
    if (noiseBestIndex == null) return;
    await goToFrame(noiseBestIndex);
  }

  async function runDiff() {
    if (!job) return;
    if (isPreview) {
      if (!previewSourceUrl || !previewDestUrl) return;
    } else if (!currentFrame?.source_url || !currentFrame?.dest_url) {
      return;
    }
    const jobId = job.id;
    const req = ++diffRequestId;
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
      if (req !== diffRequestId || selectedId !== jobId) return;
      absdiffImage = `data:image/png;base64,${abs.image}`;
      ssimImage = `data:image/png;base64,${ssimR.image}`;
      mse = abs.mse;
      ssim = abs.ssim;
      psnr = abs.psnr;
    } catch (e) {
      if (req !== diffRequestId || selectedId !== jobId) return;
      error = (e as Error).message;
    } finally {
      if (req === diffRequestId) busy = false;
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
        {#each jobs as j (j.id)}
          <div class="job-item" class:active={selectedId === j.id}>
            <button type="button" class="job-item-select" onclick={() => selectJob(j.id)}>
              <div class="job-item-main">
                <div class="job-name" title={j.filename}>{truncate(j.filename, 30)}</div>
                <div class="job-meta" title={jobMetaLine(j)}>{truncate(jobMetaLine(j), 100)}</div>
              </div>
              <table class="job-stats">
                <tbody>
                  <tr>
                    <th title={METRIC_HINTS.compression}>Compression</th>
                    <td title={METRIC_HINTS.compression}>{formatCompression(j)}</td>
                  </tr>
                  <tr>
                    <th title={METRIC_HINTS.noise}>Noise</th>
                    <td title={METRIC_HINTS.noise}>{formatNoise(j)}</td>
                  </tr>
                  <tr>
                    <th title={METRIC_HINTS.encode}>Encode</th>
                    <td title={METRIC_HINTS.encode}>{formatDuration(j.encode_duration_seconds)}</td>
                  </tr>
                  <tr>
                    <th title={METRIC_HINTS.offset}>Offset</th>
                    <td title={METRIC_HINTS.offset}>{formatOffset(j)}</td>
                  </tr>
                </tbody>
              </table>
            </button>
            <div class="job-item-actions">
              <button
                type="button"
                class="job-del"
                aria-label="Delete compare run {j.filename}"
                title={isRunning(j) ? 'Cancel the running job first' : 'Delete compare run'}
                disabled={isRunning(j) || deletingId === j.id}
                onclick={() => removeJob(j)}
              >
                <svg
                  xmlns="http://www.w3.org/2000/svg"
                  width="15"
                  height="15"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="2"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                  aria-hidden="true"
                >
                  <polyline points="3 6 5 6 21 6" />
                  <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" />
                  <path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                  <line x1="10" y1="11" x2="10" y2="17" />
                  <line x1="14" y1="11" x2="14" y2="17" />
                </svg>
              </button>
            </div>
          </div>
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
    {:else if isPreview && !previewSourceUrl && !previewUrl}
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
          <div class="control-block noise-block">
            <div class="control-head">
              <h3>1. Match noise</h3>
              <p class="help">
                Source <strong>center frame</strong> vs every encoded frame. Lowest point =
                best match. Scene changes (big jumps) are detected and the selection window
                is cut there so outliers don’t blow the scale or pick the wrong valley.
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
                <svg
                  viewBox={`0 0 ${Math.max(1, noiseValues.length - 1)} 100`}
                  preserveAspectRatio="none"
                >
                  {#if selectionEnd > selectionStart}
                    <rect
                      class="sel-band"
                      x={selectionStart}
                      y="0"
                      width={Math.max(1, selectionEnd - selectionStart)}
                      height="100"
                    />
                  {/if}
                  <polyline
                    class="noise-out"
                    fill="none"
                    stroke-width="1.2"
                    vector-effect="non-scaling-stroke"
                    points={noiseValues
                      .map((v, i) => {
                        const clipped = Number.isFinite(v)
                          ? Math.min(v, noiseMax * 1.05)
                          : noiseMax;
                        const y = 100 - (clipped / noiseMax) * 92 - 4;
                        return `${i},${y}`;
                      })
                      .join(' ')}
                  />
                  {#each sceneCuts as cut}
                    <line class="cut-line" x1={cut} x2={cut} y1="0" y2="100" />
                  {/each}
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
                  <span>Green band = selection (same scene)</span>
                  {#if noiseBestIndex != null}
                    <span>Best: frame {noiseBestIndex}</span>
                  {/if}
                  {#if sceneCuts.length}
                    <span>{sceneCuts.length} scene cut(s)</span>
                  {/if}
                </div>
              </div>
              <div class="noise-actions">
                <button
                  class="btn btn-ghost"
                  disabled={noiseBestIndex == null}
                  onclick={jumpToBestMatch}>Jump to best match</button
                >
                <button
                  class="btn btn-ghost"
                  disabled={noiseSuggestedOffset == null}
                  onclick={applySuggestedOffset}
                >
                  Use suggested offset
                  {#if noiseSuggestedOffset != null}
                    ({noiseSuggestedOffset >= 0 ? `+${noiseSuggestedOffset}` : noiseSuggestedOffset})
                  {/if}
                </button>
              </div>
            {:else}
              <button class="btn btn-ghost" onclick={() => loadNoiseGraph()}>Load noise graph</button>
            {/if}
          </div>

          <div class="control-block">
            <div class="control-head">
              <h3>2. Offset</h3>
              <p class="help">
                <code>source = pad + dest + offset</code>. Aligned ≈ 0; type any integer for
                residual drift (missing frames). Suggested value pairs source center with the
                best-match encoded frame. Lock when happy — then scrub with that pairing.
              </p>
            </div>
            <div class="offset-row">
              <input
                class="offset-num"
                type="number"
                step="1"
                bind:value={offsetInput}
                disabled={offsetLocked}
                onchange={onOffsetInputCommit}
                onkeydown={(e) => {
                  if (e.key === 'Enter') onOffsetInputCommit();
                }}
              />
              <span class="offset-val">frames</span>
              {#if !offsetLocked}
                <button class="btn btn-primary narrow" onclick={lockOffset}>Lock offset</button>
              {:else}
                <span class="align-badge">locked</span>
                <button class="btn btn-ghost" onclick={unlockOffset}>Unlock</button>
              {/if}
            </div>
            {#if offsetLocked && offsetNote}
              <p class="help oob-note">{offsetNote}</p>
            {/if}
            <div class="score-row">
              <button
                class="btn btn-primary narrow"
                disabled={!offsetLocked || scoreBusy}
                onclick={runNoiseScore}
              >
                {scoreBusy ? 'Scoring…' : 'Compute noise score for this preview'}
              </button>
              {#if job?.noise_ssim_mean != null}
                <span class="score-summary">
                  SSIM {job.noise_ssim_mean.toFixed(4)}±{(job.noise_ssim_std ?? 0).toFixed(4)}
                  · PSNR {job.noise_psnr_mean != null ? `${job.noise_psnr_mean.toFixed(2)} dB` : '—'}
                  · MSE {job.noise_mse_mean != null ? job.noise_mse_mean.toFixed(1) : '—'}
                  · n={job.noise_frame_count ?? '—'}
                </span>
              {/if}
            </div>
            {#if job?.noise_ssim_mean != null}
              <p class="help">
                Mean ± std across {job.noise_frame_count ?? 'n'} aligned preview frames — same
                SSIM / PSNR / MSE scales as below. Low std means consistent quality; a large
                ± often means leftover misalignment.
              </p>
            {/if}
            {#if scoreError}
              <div class="err">{scoreError}</div>
            {/if}
            {#if !offsetLocked}
              <p class="help">Lock offset first, then score every usable frame at that pairing.</p>
            {/if}
          </div>

          <div class="control-block">
            <div class="control-head">
              <h3>3. Browse frames</h3>
              <p class="help">
                {#if offsetLocked}
                  Offset is locked — scrub the full encoded preview. Source follows as
                  <code>pad + dest + {frameOffset >= 0 ? '+' : ''}{frameOffset}</code>. If one
                  side runs out of frames at the start/end, we clamp and show a note above.
                {:else}
                  Scrub to inspect pairs before locking. Prefer locking first so browsing
                  stays aligned across the clip.
                {/if}
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
          <button
            class="btn btn-ghost"
            class:active={mode === 'subtract'}
            onclick={() => (mode = 'subtract')}>Subtract</button
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
      {#if mode === 'abs-diff' || mode === 'subtract'}
        <label class="slider"
          >{mode === 'subtract' ? 'Subtract gain' : 'Gain'}
          <input type="range" min="1" max="8" step="0.5" bind:value={gain} />
          {gain}
        </label>
      {/if}

      <div class="preview checker" aria-busy={incomingUrl != null}>
        {#if previewUrl}
          <img class="preview-img" src={previewUrl} alt="Comparison" />
        {/if}
        {#if incomingUrl}
          {#key incomingSeq}
            {@const seq = incomingSeq}
            <img
              class="preview-img"
              class:incoming={previewUrl != null}
              src={incomingUrl}
              alt={previewUrl ? '' : 'Comparison'}
              onload={() => commitIncoming(seq)}
              onerror={() => commitIncoming(seq)}
            />
          {/key}
        {/if}
        {#if previewUrl || incomingUrl}
          <button
            class="btn btn-ghost dl"
            onclick={() =>
              downloadDataUrl(
                incomingUrl ?? previewUrl!,
                isPreview ? `preview-${frameIndex}.png` : `compare-${position}.png`
              )}>Download</button
          >
        {:else}
          <div class="empty">Rendering…</div>
        {/if}
        {#if previewUrl && incomingUrl}
          <div class="preview-pending" aria-live="polite">Loading frame…</div>
        {/if}
      </div>

      <div class="advanced">
        <div class="section-hdr">
          <h2>Server diff maps</h2>
          <button class="btn btn-primary narrow" disabled={busy} onclick={runDiff}>
            {busy ? 'Running…' : 'Run'}
          </button>
        </div>
        <div class="metrics">
          <div class="metric">
            <div class="metric-value">
              <span class="metric-label">SSIM</span>
              {ssim != null ? ssim.toFixed(4) : '—'}
            </div>
            <p class="metric-hint">{METRIC_HINTS.ssim}</p>
          </div>
          <div class="metric">
            <div class="metric-value">
              <span class="metric-label">PSNR</span>
              {#if mse == null}
                —
              {:else if psnr != null}
                {psnr.toFixed(2)} dB
              {:else}
                ∞
              {/if}
            </div>
            <p class="metric-hint">{METRIC_HINTS.psnr}</p>
          </div>
          <div class="metric">
            <div class="metric-value">
              <span class="metric-label">MSE</span>
              {mse != null ? mse.toFixed(2) : '—'}
            </div>
            <p class="metric-hint">{METRIC_HINTS.mse}</p>
          </div>
        </div>
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
    grid-template-columns: 340px 1fr;
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
    display: flex;
    gap: 4px;
    align-items: flex-start;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 8px 6px 8px 12px;
    color: var(--text);
  }
  .job-item.active {
    border-color: var(--accent);
    background: rgba(108, 99, 255, 0.1);
  }
  .job-item-select {
    flex: 1;
    min-width: 0;
    display: flex;
    gap: 10px;
    align-items: flex-start;
    text-align: left;
    background: transparent;
    border: none;
    color: inherit;
    cursor: pointer;
    padding: 2px 0;
  }
  .job-item-select:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
    border-radius: 6px;
  }
  .job-item-main {
    flex: 1;
    min-width: 0;
  }
  .job-item-actions {
    flex: 0 0 32px;
    display: flex;
    align-items: flex-start;
    opacity: 0;
    pointer-events: none;
    transition: opacity 160ms ease;
  }
  .job-item:hover .job-item-actions,
  .job-item-actions:focus-within {
    opacity: 1;
    pointer-events: auto;
  }
  .job-del {
    width: 32px;
    height: 32px;
    display: grid;
    place-items: center;
    border: none;
    border-radius: 6px;
    background: transparent;
    color: var(--muted);
    cursor: pointer;
  }
  .job-del:hover,
  .job-del:focus-visible {
    color: var(--danger);
    background: rgba(239, 68, 68, 0.12);
  }
  .job-del:focus-visible {
    outline: 2px solid var(--danger);
    outline-offset: 1px;
  }
  .job-del:disabled {
    opacity: 0.35;
    cursor: not-allowed;
  }
  @media (hover: none) {
    .job-item-actions {
      opacity: 1;
      pointer-events: auto;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .job-item-actions {
      transition: none;
    }
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
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .job-stats {
    flex: 0 0 auto;
    border-collapse: collapse;
    font-size: 10px;
    line-height: 1.35;
    color: var(--muted);
  }
  .job-stats th {
    font-weight: 500;
    text-align: right;
    padding: 0 6px 0 0;
    white-space: nowrap;
  }
  .job-stats td {
    text-align: left;
    color: var(--text);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  .score-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 10px;
  }
  .score-summary {
    font-size: 12px;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
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
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    align-items: center;
  }
  .offset-num {
    width: 96px;
    padding: 6px 8px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: transparent;
    color: inherit;
    font-size: 14px;
    font-variant-numeric: tabular-nums;
  }
  .offset-val {
    font-size: 13px;
    font-variant-numeric: tabular-nums;
    color: var(--muted);
  }
  .oob-note {
    color: var(--warn, #f5a524) !important;
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
  .noise-graph .sel-band {
    fill: rgba(61, 214, 140, 0.12);
  }
  .noise-graph .noise-out {
    stroke: currentColor;
    opacity: 0.95;
  }
  .noise-graph .cut-line {
    stroke: var(--danger, #ef4444);
    stroke-width: 1;
    stroke-dasharray: 2 2;
    vector-effect: non-scaling-stroke;
    opacity: 0.7;
  }
  .noise-graph .best-line {
    stroke: var(--ok, #3dd68c);
    stroke-width: 1.5;
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
  .preview-img {
    display: block;
    max-width: 100%;
    margin: 0 auto;
  }
  .preview-img.incoming {
    position: absolute;
    left: 50%;
    top: 0;
    transform: translateX(-50%);
    opacity: 0;
    pointer-events: none;
  }
  .preview-pending {
    position: absolute;
    left: 8px;
    top: 8px;
    font-size: 11px;
    font-weight: 600;
    color: var(--muted);
    background: rgba(15, 17, 23, 0.72);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 4px 8px;
    pointer-events: none;
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
    gap: 14px;
    margin: 14px 0;
    color: var(--text);
  }
  .metric-value {
    font-size: 18px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    line-height: 1.35;
  }
  .metric-label {
    display: inline-block;
    min-width: 4.5em;
    color: var(--accent2);
    font-weight: 700;
  }
  .metric-hint {
    margin: 4px 0 0;
    font-size: 12px;
    font-weight: 400;
    line-height: 1.45;
    color: var(--muted);
    max-width: 52em;
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
