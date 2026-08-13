<script lang="ts">
  import { goto } from '$app/navigation';
  import { onDestroy, onMount } from 'svelte';

  const CHUNK = 16 * 1024 * 1024;

  let presets = $state<string[]>([]);
  let preset = $state('');
  let selectedFile = $state<File | null>(null);
  let wsConnected = $state(false);
  let statusMsg = $state('');
  let statusType = $state<'success' | 'error' | 'info'>('info');
  let statusShow = $state(false);

  let uploadActive = $state(false);
  let uploadPct = $state(0);
  let uploadName = $state('—');
  let uploadSpd = $state('');
  let uploadEff = $state('');
  let uploadEta = $state('');

  let encodeActive = $state(false);
  let encodeFlash = $state<'green' | 'red' | ''>('');
  let encFname = $state('—');
  let encPct = $state('0%');
  let encBar = $state(0);
  let encSub = $state('');

  let cpuPct = $state<number | null>(null);
  let footerEnc = $state('');

  let queueLines = $state<{ raw: string; status: string; progress: number | null }[]>([]);
  let watchFiles = $state<{ folder: string; name: string; size_human: string }[]>([]);
  let outputs = $state<
    { name: string; preset: string; size_human: string; download_url: string }[]
  >([]);
  let currentEncodeFile = $state<string | null>(null);
  let hasLiveProgress = $state(false);

  let ws: WebSocket | null = null;
  let wsBackoff = 1000;
  let uploadTotalBytes = 0;
  let uploadConfirmedBytes = 0;
  let uploadChunkTimes: { bytes: number; ms: number }[] = [];
  let queueTimer: ReturnType<typeof setInterval> | null = null;
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
    ws.onmessage = (evt) => {
      try {
        handleWS(JSON.parse(evt.data));
      } catch {
        /* ignore */
      }
    };
  }

  function handleWS(msg: Record<string, unknown>) {
    switch (msg.type) {
      case 'chunk_ack':
        onChunkAck(msg as { index: number; total: number; server_bytes: number; decompressed_bytes: number });
        break;
      case 'upload_complete':
        uploadPct = 100;
        uploadEta = '✓ Upload complete';
        break;
      case 'encode_progress':
        onEncodeProgress(msg as { file?: string; pct?: number; fps?: number; eta_seconds?: number });
        break;
      case 'encode_done':
        onEncodeDone(msg as { file?: string });
        break;
      case 'encode_failed':
        onEncodeFailed(msg as { file?: string; exit_code?: number });
        break;
      case 'watch_update':
        watchFiles = (msg.files as typeof watchFiles) || [];
        break;
      case 'system':
        applySystemStatus(msg as Record<string, unknown>);
        break;
      case 'compare_ready':
        pollOutputs();
        break;
    }
  }

  function onChunkAck(msg: {
    index: number;
    total: number;
    server_bytes: number;
    decompressed_bytes: number;
  }) {
    uploadConfirmedBytes += msg.decompressed_bytes;
    uploadPct =
      uploadTotalBytes > 0
        ? Math.min(100, Math.round((uploadConfirmedBytes / uploadTotalBytes) * 100))
        : Math.round(((msg.index + 1) / msg.total) * 100);

    const now = Date.now();
    uploadChunkTimes.push({ bytes: msg.decompressed_bytes, ms: now });
    if (uploadChunkTimes.length > 6) uploadChunkTimes.shift();

    uploadSpd = '';
    uploadEff = '';
    uploadEta = '';
    if (uploadChunkTimes.length >= 2) {
      const oldest = uploadChunkTimes[0];
      const newest = uploadChunkTimes[uploadChunkTimes.length - 1];
      const elapsed = (newest.ms - oldest.ms) / 1000;
      const bytes = uploadChunkTimes.slice(1).reduce((s, c) => s + c.bytes, 0);
      if (elapsed > 0) {
        const bps = bytes / elapsed;
        const remaining = uploadTotalBytes - uploadConfirmedBytes;
        const etaSec = remaining > 0 ? Math.round(remaining / bps) : 0;
        const dt =
          uploadChunkTimes[uploadChunkTimes.length - 1].ms -
          uploadChunkTimes[uploadChunkTimes.length - 2].ms;
        if (dt > 0) {
          const wireMBs = ((msg.server_bytes / dt) * 1000) / 1024 / 1024;
          uploadSpd = `Wire: ${wireMBs.toFixed(1)} MB/s`;
        }
        if (msg.server_bytes < msg.decompressed_bytes) {
          const effMBs = (bps / 1024 / 1024).toFixed(1);
          const pctSaved = Math.round(100 * (1 - msg.server_bytes / msg.decompressed_bytes));
          uploadEff = `Effective: ${effMBs} MB/s (${pctSaved}% saved)`;
        }
        if (etaSec > 0) {
          const m = Math.floor(etaSec / 60);
          const s = etaSec % 60;
          uploadEta = `ETA: ${m > 0 ? m + 'm ' : ''}${s}s`;
        }
      }
    }
  }

  function onEncodeProgress(msg: {
    file?: string;
    pct?: number;
    fps?: number;
    eta_seconds?: number;
  }) {
    hasLiveProgress = true;
    currentEncodeFile = msg.file || null;
    encodeActive = true;
    encodeFlash = '';
    encFname = msg.file || '—';
    encPct = `${(msg.pct || 0).toFixed(1)}%`;
    encBar = msg.pct || 0;
    let sub = '';
    if (msg.fps) sub += `${msg.fps.toFixed(1)} fps`;
    if (msg.eta_seconds != null) {
      const h = Math.floor(msg.eta_seconds / 3600);
      const m = Math.floor((msg.eta_seconds % 3600) / 60);
      const s = msg.eta_seconds % 60;
      sub += (sub ? '  ·  ' : '') + 'ETA: ';
      if (h) sub += `${h}h `;
      if (m) sub += `${m}m `;
      sub += `${s}s`;
    }
    encSub = sub;
  }

  function onEncodeDone(msg: { file?: string }) {
    hasLiveProgress = false;
    currentEncodeFile = null;
    encodeActive = true;
    encodeFlash = 'green';
    encFname = msg.file || '—';
    encPct = '100%';
    encBar = 100;
    encSub = '✓ Encode complete';
    setTimeout(() => {
      encodeActive = false;
    }, 8000);
    pollOutputs();
  }

  function onEncodeFailed(msg: { file?: string; exit_code?: number }) {
    hasLiveProgress = false;
    currentEncodeFile = null;
    encodeActive = true;
    encodeFlash = 'red';
    encFname = msg.file || '—';
    encPct = '✗';
    encBar = 0;
    encSub = `Encode failed (exit ${msg.exit_code})`;
  }

  function applySystemStatus(d: Record<string, unknown>) {
    if (typeof d.cpu_pct === 'number') cpuPct = d.cpu_pct;
    if (d.encoding_file) {
      footerEnc = `⚙ ${d.encoding_file}`;
      if (!currentEncodeFile) currentEncodeFile = String(d.encoding_file);
      if (encodeFlash !== 'green' && encodeFlash !== 'red') {
        encodeActive = true;
        encFname = String(d.encoding_file);
        if (!hasLiveProgress) {
          encPct = 'Encoding…';
          encBar = 50;
          if (d.encoding_size_human) encSub = `${d.encoding_size_human} written`;
        }
      }
    } else {
      footerEnc = '';
      if (!hasLiveProgress) currentEncodeFile = null;
    }
  }

  async function loadPresets() {
    try {
      const d = await (await fetch('/api/presets')).json();
      presets = d.presets || [];
      if (presets.length && !preset) preset = presets[0];
    } catch {
      presets = [];
    }
  }

  async function pollQueue() {
    try {
      const d = await (await fetch('/api/queue')).json();
      queueLines = d.lines || [];
    } catch {
      /* ignore */
    }
  }

  async function pollOutputs() {
    try {
      const d = await (await fetch('/api/outputs')).json();
      outputs = d.files || [];
    } catch {
      /* ignore */
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

  async function clearQueue() {
    await fetch('/api/queue/clear', { method: 'POST' });
    pollQueue();
  }

  async function delWatch(folder: string, name: string) {
    if (!confirm(`Remove ${name} from watch queue?`)) return;
    const r = await fetch(
      `/api/watch/${encodeURIComponent(folder)}/${encodeURIComponent(name)}`,
      { method: 'DELETE' }
    );
    if (r.ok) pollWatch();
  }

  async function delOutput(presetName: string, name: string) {
    if (currentEncodeFile && name === currentEncodeFile) {
      alert('Cannot delete — this file is currently being encoded.');
      return;
    }
    if (!confirm(`Delete ${name}?`)) return;
    const r = await fetch(
      `/api/delete/${encodeURIComponent(presetName)}/${encodeURIComponent(name)}`,
      { method: 'DELETE' }
    );
    if (r.ok) pollOutputs();
  }

  function pick(f: File) {
    selectedFile = f;
    hide();
  }

  async function upload() {
    if (!selectedFile) return;
    if (!preset) {
      show('Please select a preset.', 'error');
      return;
    }

    const file = selectedFile;
    uploadActive = true;
    uploadName = file.name;
    uploadPct = 0;
    uploadSpd = '';
    uploadEff = '';
    uploadEta = '';
    uploadTotalBytes = file.size;
    uploadConfirmedBytes = 0;
    uploadChunkTimes = [];
    hide();

    const totalChunks = Math.ceil(file.size / CHUNK) || 1;
    try {
      for (let i = 0; i < totalChunks; i++) {
        const rawBlob = file.slice(i * CHUNK, Math.min((i + 1) * CHUNK, file.size));
        const form = new FormData();
        form.append('chunk', rawBlob, file.name);
        form.append('chunk_index', String(i));
        form.append('total_chunks', String(totalChunks));
        form.append('filename', file.name);
        form.append('preset', preset);
        form.append('compression', 'none');
        const r = await fetch('/api/upload/chunk', { method: 'POST', body: form });
        if (!r.ok) {
          const err = await r.json().catch(() => ({ detail: r.statusText }));
          throw new Error(err.detail || `HTTP ${r.status}`);
        }
        if (!ws || ws.readyState !== WebSocket.OPEN) {
          uploadPct = Math.round(((i + 1) / totalChunks) * 100);
        }
      }
      show(`✓ ${file.name} queued under [${preset}]`, 'success');
      selectedFile = null;
    } catch (err) {
      show(`Upload failed: ${(err as Error).message}`, 'error');
    }
  }

  function openCompare() {
    goto('/compare');
  }

  onMount(() => {
    connectWS();
    loadPresets();
    pollQueue();
    pollOutputs();
    pollWatch();
    queueTimer = setInterval(pollQueue, 5000);
    fetch('/api/status')
      .then((r) => r.json())
      .then((d) => applySystemStatus(d))
      .catch(() => {});
  });

  onDestroy(() => {
    if (queueTimer) clearInterval(queueTimer);
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
    <h2 class="side-title">Upload File</h2>

    <label for="preset-sel">Preset</label>
    <select id="preset-sel" bind:value={preset}>
      {#if !presets.length}
        <option value="">Loading…</option>
      {:else}
        {#each presets as p}
          <option value={p}>{p}</option>
        {/each}
      {/if}
    </select>

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
        <div class="spd-block">
          <span class="spd-wire">{uploadSpd}</span>
          <span class="spd-eff">{uploadEff}</span>
        </div>
        <span>{uploadPct}%</span>
      </div>
      <div class="bar-track"><div class="bar-fill bar-upload" style="width:{uploadPct}%"></div></div>
      <div class="prog-sub">{uploadEta}</div>
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

    <button class="btn btn-primary" disabled={!selectedFile} onclick={upload}>
      {selectedFile ? 'Upload' : 'Select a file to upload'}
    </button>

    {#if statusShow}
      <div class="status-msg show {statusType}">{statusMsg}</div>
    {/if}

    <div class="cpu-footer">
      <div class="cpu-row">
        <span class="cpu-label">CPU</span>
        <div class="cpu-track">
          <div
            class="cpu-fill"
            style="width:{cpuPct ?? 0}%;background:{cpuColor}"
          ></div>
        </div>
        <span class="cpu-pct">{cpuPct != null ? `${cpuPct}%` : '—'}</span>
      </div>
      <span class="enc-status">{footerEnc}</span>
    </div>
  </div>

  <div class="panel-right">
    <div class="section log-section">
      <div class="section-hdr">
        <h2>Activity Log</h2>
        <div class="hdr-right">
          <button class="btn btn-ghost" onclick={clearQueue}>Clear</button>
        </div>
      </div>
      <div class="queue-list">
        {#if !queueLines.length}
          <div class="empty">No recent activity</div>
        {:else}
          {#each queueLines as l}
            <div class="queue-item">
              <div class="dot dot-{l.status}"></div>
              <div>
                <div class="qi-text">{l.raw}</div>
                {#if l.progress != null}
                  <div class="qi-prog">{l.progress.toFixed(1)}%</div>
                {/if}
              </div>
            </div>
          {/each}
        {/if}
      </div>
    </div>

    <div class="section">
      <div class="section-hdr"><h2>Watch Queue</h2></div>
      <div class="watch-list">
        {#if !watchFiles.length}
          <div class="empty">No files pending</div>
        {:else}
          {#each watchFiles as f}
            <div class="watch-item">
              <div class="wi-body">
                <div class="wi-name">{f.name}</div>
                <div class="wi-meta">{f.folder} · {f.size_human}</div>
              </div>
              <button class="del-btn" onclick={() => delWatch(f.folder, f.name)}>✕</button>
            </div>
          {/each}
        {/if}
      </div>
    </div>

    <div class="section">
      <div class="section-hdr">
        <h2>Output Files</h2>
        <button class="btn btn-ghost" onclick={openCompare}>Compare</button>
      </div>
      <div class="outputs-list">
        {#if !outputs.length}
          <div class="empty">No output files yet</div>
        {:else}
          {#each outputs as f}
            <div class="output-item">
              <div class="oi-body">
                <div class="oi-name">
                  {f.name}
                  {#if currentEncodeFile === f.name}
                    <span class="enc-mark">⚙</span>
                  {/if}
                </div>
                <div class="oi-meta">{f.preset} · {f.size_human}</div>
              </div>
              <div class="oi-actions">
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
    grid-template-columns: 400px 1fr;
    overflow: hidden;
    min-height: 0;
  }
  .panel {
    padding: 20px;
    overflow-y: auto;
  }
  .panel-left {
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
  }
  .panel-right {
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .side-title {
    margin-bottom: 14px;
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
  }
  label {
    display: block;
    font-size: 12px;
    color: var(--muted);
    margin-bottom: 5px;
    font-weight: 500;
  }
  select {
    width: 100%;
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 9px 12px;
    border-radius: var(--r);
    font-size: 14px;
    margin-bottom: 14px;
    outline: none;
  }
  select:focus {
    border-color: var(--accent);
  }
  .dropzone {
    border: 2px dashed var(--border);
    border-radius: var(--r);
    padding: 32px 20px;
    text-align: center;
    cursor: pointer;
    position: relative;
    margin-bottom: 14px;
  }
  .dropzone.dragover,
  .dropzone:hover {
    border-color: var(--accent);
    background: rgba(108, 99, 255, 0.05);
  }
  .dropzone input {
    position: absolute;
    inset: 0;
    opacity: 0;
    cursor: pointer;
    width: 100%;
    height: 100%;
  }
  .dropzone-icon {
    font-size: 32px;
    margin-bottom: 8px;
  }
  .dropzone-text {
    color: var(--muted);
    font-size: 13px;
  }
  .dropzone-text :global(strong) {
    color: var(--text);
  }
  .progress-container {
    margin-bottom: 10px;
    display: none;
  }
  .progress-container.active {
    display: block;
  }
  .progress-label {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    font-size: 12px;
    color: var(--muted);
    margin-bottom: 4px;
    gap: 6px;
  }
  .progress-label .fn {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text);
  }
  .spd-block {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
  }
  .spd-wire {
    color: var(--accent2);
  }
  .spd-eff {
    font-size: 10px;
  }
  .bar-track {
    background: var(--border);
    border-radius: 99px;
    height: 6px;
    overflow: hidden;
  }
  .bar-fill {
    height: 100%;
    border-radius: 99px;
    transition: width 0.3s;
  }
  .bar-upload {
    background: linear-gradient(90deg, var(--accent), var(--accent2));
  }
  .bar-encode {
    background: linear-gradient(90deg, var(--warn), var(--success));
  }
  .prog-sub {
    font-size: 11px;
    color: var(--muted);
    margin-top: 2px;
  }
  .encode-panel {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: var(--r);
    padding: 12px 14px;
    margin-bottom: 14px;
    display: none;
  }
  .encode-panel.active {
    display: block;
  }
  .encode-panel.flash-green {
    border-color: var(--success);
    background: rgba(34, 197, 94, 0.07);
  }
  .encode-panel.flash-red {
    border-color: var(--danger);
    background: rgba(239, 68, 68, 0.07);
  }
  .btn {
    border: none;
    padding: 10px 16px;
    border-radius: var(--r);
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
  }
  .btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }
  .btn-primary {
    background: var(--accent);
    color: #fff;
    width: 100%;
    margin-bottom: 8px;
  }
  .btn-ghost {
    background: transparent;
    border: 1px solid var(--border);
    color: var(--muted);
    padding: 5px 12px;
    font-size: 12px;
  }
  .status-msg {
    margin-top: 8px;
    font-size: 12px;
    padding: 8px 12px;
    border-radius: 6px;
  }
  .status-msg.success {
    background: rgba(34, 197, 94, 0.1);
    color: var(--success);
    border: 1px solid rgba(34, 197, 94, 0.2);
  }
  .status-msg.error {
    background: rgba(239, 68, 68, 0.1);
    color: var(--danger);
    border: 1px solid rgba(239, 68, 68, 0.2);
  }
  .status-msg.info {
    background: rgba(108, 99, 255, 0.1);
    color: #a5b4fc;
    border: 1px solid rgba(108, 99, 255, 0.2);
  }
  .cpu-footer {
    margin-top: auto;
    padding-top: 20px;
    border-top: 1px solid var(--border);
  }
  .cpu-row {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .cpu-label {
    font-size: 11px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  .cpu-track {
    flex: 1;
    background: var(--border);
    border-radius: 99px;
    height: 6px;
    overflow: hidden;
  }
  .cpu-fill {
    height: 100%;
    border-radius: 99px;
    transition: width 0.6s;
  }
  .cpu-pct {
    font-size: 12px;
    color: var(--accent2);
    min-width: 38px;
    text-align: right;
  }
  .enc-status {
    font-size: 11px;
    color: var(--muted);
    display: block;
    margin-top: 6px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .section {
    padding: 20px;
    border-bottom: 1px solid var(--border);
    overflow-y: auto;
  }
  .log-section {
    max-height: 30vh;
  }
  .section-hdr {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 14px;
  }
  .section-hdr h2 {
    margin: 0;
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
  }
  .hdr-right {
    display: flex;
    gap: 10px;
  }
  .queue-list,
  .outputs-list,
  .watch-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .queue-item {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 12px;
    display: flex;
    gap: 10px;
  }
  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
    margin-top: 4px;
    background: var(--muted);
  }
  .dot-encoding {
    background: var(--warn);
  }
  .dot-done {
    background: var(--success);
  }
  .dot-error {
    background: var(--danger);
  }
  .dot-queued {
    background: var(--accent);
  }
  .qi-text {
    font-size: 12px;
    word-break: break-all;
  }
  .qi-prog {
    font-size: 11px;
    color: var(--accent2);
  }
  .watch-item,
  .output-item {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 12px;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .wi-body,
  .oi-body {
    flex: 1;
    min-width: 0;
  }
  .wi-name,
  .oi-name {
    font-size: 13px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .wi-meta,
  .oi-meta {
    font-size: 11px;
    color: var(--muted);
    margin-top: 2px;
  }
  .oi-actions {
    display: flex;
    gap: 6px;
  }
  .dl-btn {
    border: 1px solid var(--accent);
    color: var(--accent);
    padding: 5px 12px;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 600;
  }
  .del-btn {
    background: transparent;
    border: 1px solid var(--border);
    color: var(--muted);
    padding: 5px 10px;
    border-radius: 6px;
    font-size: 12px;
    cursor: pointer;
  }
  .del-btn:hover:not(:disabled) {
    border-color: var(--danger);
    color: var(--danger);
  }
  .del-btn:disabled {
    opacity: 0.35;
    cursor: not-allowed;
  }
  .empty {
    text-align: center;
    color: var(--muted);
    font-size: 13px;
    padding: 24px 0;
  }
  .enc-mark {
    font-size: 10px;
    color: var(--warn);
  }
  @media (max-width: 768px) {
    .layout {
      grid-template-columns: 1fr;
    }
    .panel-left {
      border-right: none;
      border-bottom: 1px solid var(--border);
    }
  }
</style>
