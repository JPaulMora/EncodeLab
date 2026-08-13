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

  let presets = $state<string[]>([]);
  let selectedFile = $state<File | null>(null);
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
  let watchFiles = $state<
    { folder: string; name: string; display_name?: string; job_id?: number | null; size_human: string }[]
  >([]);
  let currentEncodeFile = $state<string | null>(null);
  let hasLiveProgress = $state(false);

  /** Per-row preset pickers keyed by `lib:id` or `job:id` */
  let rowPreset = $state<Record<string, string>>({});
  let busyKey = $state<string | null>(null);

  let ws: WebSocket | null = null;
  let wsBackoff = 1000;
  let pollTimer: ReturnType<typeof setInterval> | null = null;
  let dragover = $state(false);

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
      case 'watch_update':
        watchFiles = (msg.files as typeof watchFiles) || [];
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
    } catch {
      presets = [];
    }
  }

  async function pollWatch() {
    try {
      const files = await (await fetch('/api/watch_queue')).json();
      watchFiles = files || [];
    } catch {
      /* ignore */
    }
  }

  async function delWatch(folder: string, name: string) {
    if (!confirm(`Remove ${name} from watch queue?`)) return;
    await fetch(
      `/api/watch/${encodeURIComponent(folder)}/${encodeURIComponent(name)}`,
      { method: 'DELETE' }
    );
    pollWatch();
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

  function pick(f: File) {
    selectedFile = f;
    hide();
  }

  async function upload() {
    if (!selectedFile) return;
    const file = selectedFile;
    uploadActive = true;
    uploadName = file.name;
    uploadPct = 0;
    hide();
    try {
      await uploadLibraryFile(file, (pct) => {
        uploadPct = pct;
      });
      show(`✓ ${file.name} added to library`, 'success');
      selectedFile = null;
      await refreshLists();
    } catch (err) {
      show(`Upload failed: ${(err as Error).message}`, 'error');
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
      const job = await createJob(source, preset, kind);
      show(
        kind === 'preview'
          ? `Preview queued (#${job.id}) with [${preset}]`
          : `Encode queued (#${job.id}) with [${preset}]`,
        'success'
      );
      await refreshLists();
      await pollWatch();
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
      await pollWatch();
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
    pollWatch();
    pollLog();
    pollTimer = setInterval(() => {
      refreshLists();
      pollWatch();
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

    <label>Drag &amp; Drop or Click to Browse</label>
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div
      class="dropzone"
      class:dragover
      ondragover={(e) => {
        e.preventDefault();
        dragover = true;
      }}
      ondragleave={() => (dragover = false)}
      ondrop={(e) => {
        e.preventDefault();
        dragover = false;
        if (e.dataTransfer?.files?.length) pick(e.dataTransfer.files[0]);
      }}
    >
      <input
        type="file"
        accept="video/*,.mkv,.mp4,.avi,.mov,.m4v,.ts,.m2ts,.wmv,.mts"
        onchange={(e) => {
          const f = (e.currentTarget as HTMLInputElement).files?.[0];
          if (f) pick(f);
        }}
      />
      <div class="dropzone-icon">📁</div>
      <div class="dropzone-text">
        {#if selectedFile}
          <strong>{selectedFile.name}</strong><br />{hsize(selectedFile.size)}
        {:else}
          <strong>Drop video here</strong><br />or click to browse
        {/if}
      </div>
    </div>

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

    <button class="btn btn-primary" disabled={!selectedFile || uploadActive} onclick={upload}>
      {selectedFile ? 'Upload to library' : 'Select a file to upload'}
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

    {#if watchFiles.length}
      <div class="section">
        <div class="section-hdr"><h2>Watch queue</h2></div>
        <div class="file-list">
          {#each watchFiles as f}
            <div class="file-row">
              <div class="fi-body">
                <div class="fi-name">{f.display_name || f.name}</div>
                <div class="fi-meta">{f.folder} · {f.size_human}</div>
              </div>
              <button class="del-btn" onclick={() => delWatch(f.folder, f.name)}>✕</button>
            </div>
          {/each}
        </div>
      </div>
    {/if}

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
                  value={presetFor(key)}
                  onchange={(e) => setPreset(key, (e.currentTarget as HTMLSelectElement).value)}
                >
                  {#each presets as p}
                    <option value={p}>{p}</option>
                  {/each}
                </select>
                <button
                  class="btn btn-ghost narrow"
                  disabled={busyKey === key + 'encode'}
                  onclick={() => startJob({ type: 'library', id: f.id }, 'encode', key)}
                  >Encode</button
                >
                <button
                  class="btn btn-ghost narrow"
                  disabled={busyKey === key + 'preview'}
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
                  value={presetFor(key)}
                  onchange={(e) => setPreset(key, (e.currentTarget as HTMLSelectElement).value)}
                >
                  {#each presets as p}
                    <option value={p}>{p}</option>
                  {/each}
                </select>
                <button
                  class="btn btn-ghost narrow"
                  disabled={busyKey === key + 'encode'}
                  onclick={() => startJob({ type: 'job', id: f.job_id }, 'encode', key)}
                  >Encode</button
                >
                <button
                  class="btn btn-ghost narrow"
                  disabled={busyKey === key + 'preview'}
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
  label {
    font-size: 0.75rem;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .dropzone {
    position: relative;
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
  .dropzone input {
    position: absolute;
    inset: 0;
    opacity: 0;
    cursor: pointer;
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
