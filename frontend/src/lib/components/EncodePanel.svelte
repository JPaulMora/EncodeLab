<script lang="ts">
  import { goto } from '$app/navigation';
  import { onDestroy, onMount } from 'svelte';
  import {
    cancelJob,
    createJob,
    deleteLibrary,
    fetchJobs,
    fetchLibrary,
    fetchOutputs,
    uploadLibraryFile
  } from '$lib/api';
  import type { EncodeJob, JobSource, LibraryFile, OutputFile } from '$lib/types';

  type PendingUpload = {
    id: number;
    file: File;
    status: 'queued' | 'uploading' | 'error';
    pct: number;
    error: string;
  };

  let presets = $state<string[]>([]);
  let presetFormats = $state<Record<string, string>>({});
  let keepTracks = $state(false);
  let pending = $state<PendingUpload[]>([]);
  let wsConnected = $state(false);
  let statusMsg = $state('');
  let statusType = $state<'success' | 'error' | 'info'>('info');
  let statusShow = $state(false);

  let uploadActive = $state(false);
  let uploadPct = $state(0);
  let uploadName = $state('—');

  let encodeActive = $state(false);
  let encodeFlash = $state<'green' | 'red' | ''>('');
  let encFname = $state('—');
  let encPct = $state('0%');
  let encBar = $state(0);
  let encSub = $state('');

  let cpuPct = $state<number | null>(null);
  let footerEnc = $state('');
  let logOpen = $state(false);
  let recentLines = $state<string[]>([]);

  let library = $state<LibraryFile[]>([]);
  let outputs = $state<OutputFile[]>([]);
  let jobs = $state<EncodeJob[]>([]);
  let currentEncodeFile = $state<string | null>(null);
  let hasLiveProgress = $state(false);

  /** Per-row preset pickers keyed by `lib:id` or `job:id` */
  let rowPreset = $state<Record<string, string>>({});
  let busyKey = $state<string | null>(null);

  let ws: WebSocket | null = null;
  let wsBackoff = 1000;
  let pollTimer: ReturnType<typeof setInterval> | null = null;
  let dragover = $state(false);
  let pendingId = 0;
  let filePicker: HTMLInputElement | undefined;

  const VIDEO_EXT =
    /\.(mp4|mkv|avi|mov|m4v|mpg|mpeg|wmv|flv|webm|ts|mts|m2ts)$/i;

  function show(msg: string, type: 'success' | 'error' | 'info') {
    statusMsg = msg;
    statusType = type;
    statusShow = true;
  }
  function hide() {
    statusShow = false;
  }

  function hsize(b: number) {
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    let n = b;
    while (n >= 1024 && i < u.length - 1) {
      n /= 1024;
      i++;
    }
    return `${n.toFixed(1)} ${u[i]}`;
  }

  function presetFor(key: string) {
    return rowPreset[key] || presets[0] || '';
  }

  function formatFor(preset: string): string {
    return presetFormats[preset] || '';
  }

  function mp4TrackWarning(preset: string): string | undefined {
    if (!keepTracks || formatFor(preset) !== 'mp4') return undefined;
    return 'MP4 can keep extra AAC/AC3 audio, but bitmap subtitles (PGS, DVD) and some codecs (DTS, TrueHD, FLAC) usually cannot be stored as extra tracks and may be skipped. Use an MKV preset to keep them.';
  }

  const mkvPresets = $derived(presets.filter((p) => formatFor(p) === 'mkv'));
  const mp4Presets = $derived(presets.filter((p) => formatFor(p) === 'mp4'));

  function setPreset(key: string, value: string) {
    rowPreset = { ...rowPreset, [key]: value };
  }

  async function refreshLists() {
    try {
      const [lib, outs, j] = await Promise.all([
        fetchLibrary(),
        fetchOutputs(),
        fetchJobs(100)
      ]);
      library = lib;
      outputs = outs;
      jobs = j;
    } catch {
      /* ignore transient */
    }
  }

  function connectWS() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${proto}//${location.host}/ws`);
    ws.onopen = () => {
      wsBackoff = 1000;
      wsConnected = true;
    };
    ws.onclose = () => {
      wsConnected = false;
      setTimeout(connectWS, wsBackoff);
      wsBackoff = Math.min(wsBackoff * 2, 30000);
    };
    ws.onerror = () => ws?.close();
    ws.onmessage = (ev) => {
      try {
        handleMsg(JSON.parse(ev.data));
      } catch {
        /* ignore */
      }
    };
  }

  function handleMsg(msg: Record<string, unknown>) {
    switch (msg.type) {
      case 'chunk_ack':
        break;
      case 'library_upload_complete':
        uploadPct = 100;
        refreshLists();
        break;
      case 'job_queued':
        refreshLists();
        break;
      case 'encode_progress':
        applyEncodeProgress(msg);
        break;
      case 'encode_done':
      case 'preview_ready':
      case 'compare_ready':
        encodeFlash = 'green';
        encodeActive = true;
        encPct = '100%';
        encBar = 100;
        encSub = msg.type === 'preview_ready' ? 'Preview ready' : 'Done';
        setTimeout(() => (encodeFlash = ''), 1200);
        refreshLists();
        if (msg.type === 'preview_ready' && msg.job_id) {
          goto(`/compare?job=${msg.job_id}`);
        }
        break;
      case 'job_deleted':
        refreshLists();
        break;
      case 'encode_failed':
        encodeFlash = 'red';
        encSub = 'Failed';
        setTimeout(() => (encodeFlash = ''), 1500);
        refreshLists();
        break;
      case 'encode_cancelled':
        encSub = 'Cancelled';
        encodeActive = false;
        refreshLists();
        break;
      case 'system':
        applySystemStatus(msg);
        break;
    }
  }

  function applyEncodeProgress(msg: Record<string, unknown>) {
    encodeActive = true;
    hasLiveProgress = true;
    const pct = Number(msg.pct ?? 0);
    encFname = String(msg.file || '—');
    encPct = `${pct.toFixed(1)}%`;
    encBar = pct;
    currentEncodeFile = String(msg.file || '');
    const fps = msg.fps != null ? `${Number(msg.fps).toFixed(1)} fps` : '';
    const eta =
      msg.eta_seconds != null
        ? `ETA ${Math.floor(Number(msg.eta_seconds) / 60)}m ${Number(msg.eta_seconds) % 60}s`
        : '';
    encSub = [fps, eta].filter(Boolean).join(' · ');
    footerEnc = encFname;
  }

  function applySystemStatus(msg: Record<string, unknown>) {
    if (msg.cpu_pct != null) cpuPct = Number(msg.cpu_pct);
    if (msg.encoding) {
      if (!hasLiveProgress) currentEncodeFile = String(msg.encoding_file || '');
      footerEnc = String(msg.encoding_file || '');
      encodeActive = true;
    } else if (!hasLiveProgress) {
      currentEncodeFile = null;
      encodeActive = false;
      footerEnc = '';
    }
  }

  async function pollLog() {
    try {
      const d = await (await fetch('/api/queue')).json();
      recentLines = (d.lines || []).slice(-4).map((l: { raw: string }) => l.raw);
    } catch {
      /* ignore */
    }
  }

  async function loadPresets() {
    try {
      const d = await (await fetch('/api/presets')).json();
      presets = d.presets || [];
      presetFormats = d.formats || {};
    } catch {
      presets = [];
      presetFormats = {};
    }
  }

  async function delOutput(presetName: string, name: string) {
    if (currentEncodeFile && name === currentEncodeFile) {
      alert('Cannot delete — this file is currently being encoded.');
      return;
    }
    if (!confirm(`Delete output ${name}?`)) return;
    const r = await fetch(
      `/api/delete/${encodeURIComponent(presetName)}/${encodeURIComponent(name)}`,
      { method: 'DELETE' }
    );
    if (!r.ok) {
      const err = await r.json().catch(() => ({ detail: r.statusText }));
      alert(err.detail || 'Delete failed');
      return;
    }
    refreshLists();
  }

  function isVideoFile(f: File): boolean {
    if (f.type.startsWith('video/')) return true;
    return VIDEO_EXT.test(f.name);
  }

  function filesFromList(files: FileList | File[] | null | undefined): File[] {
    return files ? Array.from(files) : [];
  }

  function fileKey(f: File) {
    return `${f.name}:${f.size}:${f.lastModified}`;
  }

  function filesFromDrop(dt: DataTransfer | null): File[] {
    if (!dt) return [];
    const seen = new Set<string>();
    const out: File[] = [];
    const add = (f: File | null) => {
      if (!f) return;
      const key = fileKey(f);
      if (seen.has(key)) return;
      seen.add(key);
      out.push(f);
    };
    // Prefer items: on macOS, dataTransfer.files often contains only the last file.
    for (const item of Array.from(dt.items || [])) {
      if (item.kind === 'file') add(item.getAsFile());
    }
    for (const f of Array.from(dt.files || [])) add(f);
    return out;
  }

  function addFiles(files: FileList | File[]) {
    const list = filesFromList(files).filter(isVideoFile);
    if (!list.length) {
      show('No video files in that selection', 'error');
      return;
    }
    const next = [...pending];
    for (const file of list) {
      const dup = next.some(
        (p) => p.file.name === file.name && p.file.size === file.size
      );
      if (dup) continue;
      next.push({
        id: ++pendingId,
        file,
        status: 'queued',
        pct: 0,
        error: ''
      });
    }
    pending = next;
    hide();
  }

  async function pickWithFilePicker() {
    try {
      const handles = await showOpenFilePicker({ multiple: true });
      const files = await Promise.all(handles.map((h) => h.getFile()));
      addFiles(files);
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') return;
      show((err as Error).message, 'error');
    }
  }

  function onBrowseClick(e: MouseEvent) {
    // Chromium's hidden-input .click() opens a single-file NSOpenPanel on macOS.
    if (typeof showOpenFilePicker !== 'function') return;
    e.preventDefault();
    void pickWithFilePicker();
  }

  function removePending(id: number) {
    const item = pending.find((p) => p.id === id);
    if (!item || item.status === 'uploading') return;
    pending = pending.filter((p) => p.id !== id);
  }

  function clearPending() {
    pending = pending.filter((p) => p.status === 'uploading');
  }

  const queuedCount = $derived(pending.filter((p) => p.status === 'queued').length);
  const pendingBytes = $derived(
    pending.filter((p) => p.status !== 'error').reduce((n, p) => n + p.file.size, 0)
  );

  async function upload() {
    if (uploadActive || !queuedCount) return;
    uploadActive = true;
    hide();
    let ok = 0;
    let fail = 0;
    try {
      while (true) {
        const item = pending.find((p) => p.status === 'queued');
        if (!item) break;
        item.status = 'uploading';
        item.pct = 0;
        item.error = '';
        pending = pending;
        uploadName = item.file.name;
        uploadPct = 0;
        try {
          await uploadLibraryFile(item.file, (pct) => {
            item.pct = pct;
            uploadPct = pct;
            pending = pending;
          });
          ok += 1;
          pending = pending.filter((p) => p.id !== item.id);
          await refreshLists();
        } catch (err) {
          fail += 1;
          item.status = 'error';
          item.error = (err as Error).message;
          pending = pending;
        }
      }
      if (ok && fail) show(`✓ ${ok} uploaded, ${fail} failed`, 'error');
      else if (fail) show(`Upload failed: ${pending.find((p) => p.error)?.error || 'error'}`, 'error');
      else if (ok) show(`✓ ${ok} file${ok === 1 ? '' : 's'} added to library`, 'success');
    } finally {
      uploadActive = false;
    }
  }

  async function startJob(source: JobSource, kind: 'encode' | 'preview', key: string) {
    const preset = presetFor(key);
    if (!preset) {
      show('Select a preset first', 'error');
      return;
    }
    busyKey = key + kind;
    try {
      const job = await createJob(source, preset, kind, keepTracks);
      show(
        kind === 'preview'
          ? `Preview queued (#${job.id}) with [${preset}]`
          : `Encode queued (#${job.id}) with [${preset}]${keepTracks ? ' · extra tracks' : ''}`,
        'success'
      );
      await refreshLists();
    } catch (err) {
      show((err as Error).message, 'error');
    } finally {
      busyKey = null;
    }
  }

  async function onCancel(id: number) {
    if (!confirm(`Cancel job #${id}?`)) return;
    try {
      await cancelJob(id);
      show(`Cancelled #${id}`, 'info');
      await refreshLists();
    } catch (err) {
      show((err as Error).message, 'error');
    }
  }

  async function onDeleteLibrary(id: number, name: string) {
    if (!confirm(`Delete library file ${name}?`)) return;
    try {
      await deleteLibrary(id);
      await refreshLists();
    } catch (err) {
      alert((err as Error).message);
    }
  }

  const activeJobs = $derived(
    jobs.filter((j) =>
      ['queued', 'encoding', 'previewing', 'extracting'].includes(j.status)
    )
  );

  onMount(() => {
    connectWS();
    loadPresets();
    refreshLists();
    pollLog();
    pollTimer = setInterval(() => {
      refreshLists();
      pollLog();
    }, 5000);
    fetch('/api/status')
      .then((r) => r.json())
      .then((d) => applySystemStatus(d))
      .catch(() => {});
  });

  onDestroy(() => {
    if (pollTimer) clearInterval(pollTimer);
    ws?.close();
  });

  const cpuColor = $derived(
    cpuPct == null
      ? 'var(--accent2)'
      : cpuPct >= 80
        ? 'var(--danger)'
        : cpuPct >= 50
          ? 'var(--warn)'
          : 'var(--accent2)'
  );
</script>

<div class="layout">
  <div class="panel panel-left">
    <h2 class="side-title">Library upload</h2>

    <span class="field-label">Drag &amp; drop or click to browse</span>
    <label
      class="dropzone"
      class:dragover
      for="library-files"
      tabindex="0"
      onclick={onBrowseClick}
      onkeydown={(e) => {
        if (e.key !== 'Enter' && e.key !== ' ') return;
        e.preventDefault();
        if (typeof showOpenFilePicker === 'function') void pickWithFilePicker();
        else filePicker?.click();
      }}
      ondragenter={(e) => {
        e.preventDefault();
        dragover = true;
      }}
      ondragover={(e) => {
        e.preventDefault();
        if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
        dragover = true;
      }}
      ondragleave={() => (dragover = false)}
      ondrop={(e) => {
        e.preventDefault();
        e.stopPropagation();
        dragover = false;
        const dropped = filesFromDrop(e.dataTransfer);
        if (dropped.length) addFiles(dropped);
      }}
    >
      <input
        id="library-files"
        bind:this={filePicker}
        class="file-picker"
        type="file"
        multiple={true}
        onchange={(e) => {
          const input = e.currentTarget as HTMLInputElement;
          if (input.files?.length) addFiles(input.files);
          input.value = '';
        }}
      />
      <div class="dropzone-icon">📁</div>
      <div class="dropzone-text">
        {#if pending.length}
          <strong>{pending.length} video{pending.length === 1 ? '' : 's'} queued</strong><br />
          drop or click to add more
        {:else}
          <strong>Drop videos here</strong><br />or click to browse (Shift/Cmd-click several)
        {/if}
      </div>
    </label>

    {#if pending.length}
      <div class="pending">
        <div class="pending-head">
          <span>Pending · {hsize(pendingBytes)}</span>
          {#if pending.some((p) => p.status !== 'uploading')}
            <button type="button" class="btn btn-ghost narrow" onclick={clearPending}>Clear</button>
          {/if}
        </div>
        <ul class="pending-list">
          {#each pending as p (p.id)}
            <li class="pending-row" class:error={p.status === 'error'} class:uploading={p.status === 'uploading'}>
              <div class="pending-body">
                <div class="pending-name" title={p.file.name}>{p.file.name}</div>
                <div class="pending-meta">
                  {#if p.status === 'uploading'}
                    {p.pct}% · {hsize(p.file.size)}
                  {:else if p.status === 'error'}
                    {p.error || 'Failed'}
                  {:else}
                    {hsize(p.file.size)}
                  {/if}
                </div>
                {#if p.status === 'uploading'}
                  <div class="bar-track pending-bar">
                    <div class="bar-fill bar-upload" style="width:{p.pct}%"></div>
                  </div>
                {/if}
              </div>
              {#if p.status !== 'uploading'}
                <button
                  type="button"
                  class="del-btn"
                  aria-label="Remove {p.file.name}"
                  onclick={() => removePending(p.id)}>✕</button
                >
              {/if}
            </li>
          {/each}
        </ul>
      </div>
    {/if}

    <div class="progress-container" class:active={uploadActive}>
      <div class="progress-label">
        <span class="fn">{uploadName}</span>
        <span>{uploadPct}%</span>
      </div>
      <div class="bar-track"><div class="bar-fill bar-upload" style="width:{uploadPct}%"></div></div>
    </div>

    <div
      class="encode-panel"
      class:active={encodeActive}
      class:flash-green={encodeFlash === 'green'}
      class:flash-red={encodeFlash === 'red'}
    >
      <div class="progress-label" style="margin-bottom:6px;">
        <span class="fn">{encFname}</span>
        <span style="color:var(--warn);font-weight:700;">{encPct}</span>
      </div>
      <div class="bar-track"><div class="bar-fill bar-encode" style="width:{encBar}%"></div></div>
      <div class="prog-sub">{encSub}</div>
    </div>

    <button class="btn btn-primary" disabled={!queuedCount || uploadActive} onclick={upload}>
      {#if uploadActive}
        Uploading…
      {:else if queuedCount}
        Upload {queuedCount} file{queuedCount === 1 ? '' : 's'}
      {:else}
        Select files to upload
      {/if}
    </button>

    {#if statusShow}
      <div class="status-msg show {statusType}">{statusMsg}</div>
    {/if}

    <div class="cpu-footer">
      <div class="cpu-row">
        <span class="cpu-label">CPU</span>
        <div class="cpu-track">
          <div class="cpu-fill" style="width:{cpuPct ?? 0}%;background:{cpuColor}"></div>
        </div>
        <span class="cpu-pct">{cpuPct != null ? `${cpuPct}%` : '—'}</span>
      </div>
      <span class="enc-status">{footerEnc || (wsConnected ? 'Idle' : 'Offline')}</span>
    </div>

    <div class="log-mini">
      <button class="btn btn-ghost log-toggle" onclick={() => (logOpen = !logOpen)}>
        {logOpen ? 'Hide log' : 'Log'}
      </button>
      {#if logOpen}
        <div class="log-lines">
          {#each recentLines as line}
            <div class="log-line">{line}</div>
          {:else}
            <div class="empty">No recent activity</div>
          {/each}
        </div>
      {/if}
    </div>
  </div>

  <div class="panel-right">
    {#if activeJobs.length}
      <div class="section">
        <div class="section-hdr"><h2>In progress</h2></div>
        <div class="file-list">
          {#each activeJobs as j}
            <div class="file-row">
              <div class="fi-body">
                <div class="fi-name">
                  {j.filename}
                  <span class="badge">{j.kind}</span>
                  <span class="badge muted">{j.status}</span>
                </div>
                <div class="fi-meta">
                  #{j.id} · {j.preset} · {j.source_label}
                  {#if j.keep_tracks}
                    · extra tracks
                  {/if}
                  {#if j.progress}
                    · {j.progress.toFixed(0)}%
                  {/if}
                </div>
              </div>
              <div class="fi-actions">
                <button class="btn btn-ghost narrow" onclick={() => onCancel(j.id)}>Cancel</button>
              </div>
            </div>
          {/each}
        </div>
      </div>
    {/if}

    <div class="section encode-opts">
      <label class="keep-tracks">
        <input type="checkbox" bind:checked={keepTracks} />
        Keep extra audio &amp; subtitles
      </label>
      {#if keepTracks}
        <p class="tracks-warn">
          {#if mkvPresets.length}
            <strong>MKV</strong> ({mkvPresets.join(', ')}) can keep extra audio and subtitle
            tracks.
          {/if}
          {#if mp4Presets.length}
            {' '}
            <strong>MP4</strong> ({mp4Presets.join(', ')}) can keep extra AAC/AC3 audio, but
            bitmap subtitles (PGS, DVD) and some codecs (DTS, TrueHD, FLAC) usually cannot be
            stored as extra tracks and may be skipped.
          {/if}
        </p>
      {/if}
    </div>

    <div class="section">
      <div class="section-hdr"><h2>Library</h2></div>
      <div class="file-list">
        {#if !library.length}
          <div class="empty">No library files — upload a video</div>
        {:else}
          {#each library as f}
            {@const key = `lib:${f.id}`}
            <div class="file-row">
              <div class="fi-body">
                <div class="fi-name">{f.original_filename}</div>
                <div class="fi-meta">{f.size_human} · library #{f.id}</div>
              </div>
              <div class="fi-actions">
                <select
                  class="preset-mini"
                  class:preset-warn={keepTracks && formatFor(presetFor(key)) === 'mp4'}
                  value={presetFor(key)}
                  title={mp4TrackWarning(presetFor(key)) ?? undefined}
                  onchange={(e) => setPreset(key, (e.currentTarget as HTMLSelectElement).value)}
                >
                  {#each presets as p}
                    <option value={p}>{p}</option>
                  {/each}
                </select>
                {#if keepTracks && formatFor(presetFor(key)) === 'mp4'}
                  <span class="tracks-row-warn" title={mp4TrackWarning(presetFor(key))}>MP4</span>
                {/if}
                <button
                  class="btn btn-ghost narrow"
                  disabled={busyKey === key + 'encode'}
                  title={mp4TrackWarning(presetFor(key))}
                  onclick={() => startJob({ type: 'library', id: f.id }, 'encode', key)}
                  >Encode</button
                >
                <button
                  class="btn btn-ghost narrow"
                  disabled={busyKey === key + 'preview'}
                  title={mp4TrackWarning(presetFor(key))}
                  onclick={() => startJob({ type: 'library', id: f.id }, 'preview', key)}
                  >Preview</button
                >
                <a class="dl-btn" href={f.download_url} download>↓</a>
                <button class="del-btn" onclick={() => onDeleteLibrary(f.id, f.original_filename)}
                  >✕</button
                >
              </div>
            </div>
          {/each}
        {/if}
      </div>
    </div>

    <div class="section">
      <div class="section-hdr">
        <h2>Encoded outputs</h2>
        <button class="btn btn-ghost" onclick={() => goto('/compare')}>Compare</button>
      </div>
      <div class="file-list">
        {#if !outputs.length}
          <div class="empty">No encoded outputs yet</div>
        {:else}
          {#each outputs as f}
            {@const key = `job:${f.job_id}`}
            <div class="file-row">
              <div class="fi-body">
                <div class="fi-name">
                  {f.display_name}
                  {#if currentEncodeFile === f.name}
                    <span class="enc-mark">⚙</span>
                  {/if}
                </div>
                <div class="fi-meta">
                  {f.preset} · {f.size_human} · {f.source_label} · job #{f.job_id}
                </div>
              </div>
              <div class="fi-actions">
                <select
                  class="preset-mini"
                  class:preset-warn={keepTracks && formatFor(presetFor(key)) === 'mp4'}
                  value={presetFor(key)}
                  title={mp4TrackWarning(presetFor(key)) ?? undefined}
                  onchange={(e) => setPreset(key, (e.currentTarget as HTMLSelectElement).value)}
                >
                  {#each presets as p}
                    <option value={p}>{p}</option>
                  {/each}
                </select>
                {#if keepTracks && formatFor(presetFor(key)) === 'mp4'}
                  <span class="tracks-row-warn" title={mp4TrackWarning(presetFor(key))}>MP4</span>
                {/if}
                <button
                  class="btn btn-ghost narrow"
                  disabled={busyKey === key + 'encode'}
                  title={mp4TrackWarning(presetFor(key))}
                  onclick={() => startJob({ type: 'job', id: f.job_id }, 'encode', key)}
                  >Encode</button
                >
                <button
                  class="btn btn-ghost narrow"
                  disabled={busyKey === key + 'preview'}
                  title={mp4TrackWarning(presetFor(key))}
                  onclick={() => startJob({ type: 'job', id: f.job_id }, 'preview', key)}
                  >Preview</button
                >
                <a class="dl-btn" href={f.download_url} download>↓</a>
                <button
                  class="del-btn"
                  disabled={currentEncodeFile === f.name}
                  onclick={() => delOutput(f.preset, f.name)}>✕</button
                >
              </div>
            </div>
          {/each}
        {/if}
      </div>
    </div>
  </div>
</div>

<style>
  .layout {
    flex: 1;
    display: grid;
    grid-template-columns: 340px 1fr;
    overflow: hidden;
    min-height: 0;
  }
  .panel {
    padding: 16px 18px;
    border-right: 1px solid var(--border);
    overflow: auto;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .panel-right {
    overflow: auto;
    padding: 12px 16px 24px;
    display: flex;
    flex-direction: column;
    gap: 14px;
    min-height: 0;
  }
  .side-title {
    margin: 0 0 4px;
    font-size: 1rem;
  }
  label:not(.dropzone),
  .field-label {
    font-size: 0.75rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .dropzone {
    position: relative;
    display: block;
    border: 2px dashed var(--border);
    border-radius: 10px;
    padding: 28px 12px;
    text-align: center;
    cursor: pointer;
    background: var(--panel-2, rgba(255, 255, 255, 0.02));
  }
  .dropzone.dragover {
    border-color: var(--accent);
  }
  .file-picker {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    opacity: 0;
    /* Drops must hit the label, not the input — macOS file inputs keep only the last file. */
    pointer-events: none;
  }
  .dropzone-icon {
    font-size: 1.6rem;
    margin-bottom: 6px;
  }
  .dropzone-text {
    font-size: 0.85rem;
    color: var(--muted);
    line-height: 1.4;
  }
  .pending {
    display: flex;
    flex-direction: column;
    gap: 6px;
    min-height: 0;
  }
  .pending-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 0.72rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .pending-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
    max-height: 168px;
    overflow: auto;
  }
  .pending-row {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 6px 8px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--panel-2, rgba(255, 255, 255, 0.02));
  }
  .pending-row.uploading {
    border-color: var(--accent);
  }
  .pending-row.error {
    border-color: var(--danger);
  }
  .pending-body {
    flex: 1;
    min-width: 0;
  }
  .pending-name {
    font-size: 0.75rem;
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .pending-meta {
    font-size: 0.68rem;
    color: var(--muted);
    margin-top: 1px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .pending-row.error .pending-meta {
    color: var(--danger);
  }
  .pending-bar {
    margin-top: 4px;
  }
  .progress-container {
    display: none;
  }
  .progress-container.active {
    display: block;
  }
  .progress-label {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    font-size: 0.8rem;
    gap: 8px;
  }
  .fn {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-weight: 600;
  }
  .bar-track {
    height: 6px;
    background: rgba(255, 255, 255, 0.08);
    border-radius: 4px;
    overflow: hidden;
    margin-top: 4px;
  }
  .bar-fill {
    height: 100%;
    border-radius: 4px;
    transition: width 0.2s;
  }
  .bar-upload {
    background: var(--accent);
  }
  .bar-encode {
    background: var(--warn);
  }
  .encode-panel {
    display: none;
    padding: 8px;
    border-radius: 8px;
    border: 1px solid var(--border);
  }
  .encode-panel.active {
    display: block;
  }
  .encode-panel.flash-green {
    border-color: var(--ok, #3dd68c);
  }
  .encode-panel.flash-red {
    border-color: var(--danger);
  }
  .prog-sub {
    font-size: 0.72rem;
    color: var(--muted);
    margin-top: 4px;
  }
  .btn {
    border: 1px solid var(--border);
    background: transparent;
    color: inherit;
    border-radius: 6px;
    padding: 6px 10px;
    cursor: pointer;
    font-size: 0.8rem;
  }
  .btn-primary {
    background: var(--accent);
    border-color: var(--accent);
    color: #111;
    font-weight: 600;
    padding: 10px;
  }
  .btn-primary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .btn-ghost {
    background: transparent;
  }
  .btn.narrow {
    padding: 4px 8px;
    font-size: 0.72rem;
  }
  .status-msg {
    font-size: 0.8rem;
    padding: 8px;
    border-radius: 6px;
  }
  .status-msg.success {
    background: rgba(61, 214, 140, 0.12);
  }
  .status-msg.error {
    background: rgba(255, 90, 90, 0.12);
  }
  .status-msg.info {
    background: rgba(100, 160, 255, 0.12);
  }
  .cpu-footer {
    margin-top: auto;
    padding-top: 8px;
    border-top: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .cpu-row {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 0.75rem;
  }
  .cpu-track {
    flex: 1;
    height: 4px;
    background: rgba(255, 255, 255, 0.08);
    border-radius: 2px;
    overflow: hidden;
  }
  .cpu-fill {
    height: 100%;
  }
  .enc-status {
    font-size: 0.7rem;
    color: var(--muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .log-mini {
    font-size: 0.7rem;
  }
  .log-toggle {
    width: 100%;
    text-align: left;
    opacity: 0.7;
  }
  .log-lines {
    max-height: 72px;
    overflow: auto;
    margin-top: 4px;
    font-family: ui-monospace, monospace;
    color: var(--muted);
  }
  .log-line {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .section-hdr {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 8px;
  }
  .section-hdr h2 {
    margin: 0;
    font-size: 0.95rem;
  }
  .file-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .file-row {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 12px;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--panel-2, rgba(255, 255, 255, 0.02));
  }
  .fi-body {
    flex: 1;
    min-width: 0;
  }
  .fi-name {
    font-weight: 600;
    font-size: 0.88rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .fi-meta {
    font-size: 0.72rem;
    color: var(--muted);
    margin-top: 2px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .fi-actions {
    display: flex;
    align-items: center;
    gap: 4px;
    flex-shrink: 0;
  }
  .preset-mini {
    max-width: 110px;
    font-size: 0.7rem;
    padding: 3px 4px;
    border-radius: 4px;
    border: 1px solid var(--border);
    background: transparent;
    color: inherit;
  }
  .preset-mini.preset-warn {
    border-color: var(--warn, #f59e0b);
  }
  .encode-opts {
    padding-bottom: 2px;
  }
  .keep-tracks {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 0.82rem;
    font-weight: 500;
    color: var(--text);
    text-transform: none;
    letter-spacing: 0;
    cursor: pointer;
    user-select: none;
  }
  .keep-tracks input {
    margin: 0;
    accent-color: var(--accent);
  }
  .tracks-warn {
    margin: 8px 0 0;
    font-size: 0.75rem;
    line-height: 1.45;
    color: var(--warn, #f59e0b);
  }
  .tracks-row-warn {
    font-size: 0.62rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    color: var(--warn, #f59e0b);
    border: 1px solid var(--warn, #f59e0b);
    border-radius: 4px;
    padding: 1px 4px;
    cursor: help;
  }
  .dl-btn,
  .del-btn {
    border: none;
    background: transparent;
    color: var(--muted);
    cursor: pointer;
    font-size: 0.9rem;
    padding: 4px 6px;
    text-decoration: none;
  }
  .del-btn:hover {
    color: var(--danger);
  }
  .badge {
    display: inline-block;
    font-size: 0.65rem;
    padding: 1px 6px;
    border-radius: 999px;
    background: rgba(100, 160, 255, 0.2);
    margin-left: 6px;
    font-weight: 500;
    vertical-align: middle;
  }
  .badge.muted {
    background: rgba(255, 255, 255, 0.08);
  }
  .enc-mark {
    margin-left: 6px;
  }
  .empty {
    color: var(--muted);
    font-size: 0.85rem;
    padding: 12px;
    text-align: center;
  }
</style>
