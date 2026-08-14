import type {
  EncodeJob,
  JobSource,
  LibraryFile,
  OutputFile,
  ServerDiffMode
} from './types';

export async function fetchJobs(limit = 50): Promise<EncodeJob[]> {
  const r = await fetch(`/api/jobs?limit=${limit}`);
  if (!r.ok) throw new Error('Failed to load jobs');
  const d = await r.json();
  return d.jobs ?? [];
}

export async function fetchJob(id: number): Promise<EncodeJob> {
  const r = await fetch(`/api/jobs/${id}`);
  if (!r.ok) throw new Error('Job not found');
  return r.json();
}

export async function fetchLibrary(): Promise<LibraryFile[]> {
  const r = await fetch('/api/library');
  if (!r.ok) throw new Error('Failed to load library');
  const d = await r.json();
  return d.files ?? [];
}

export async function fetchOutputs(): Promise<OutputFile[]> {
  const r = await fetch('/api/outputs');
  if (!r.ok) throw new Error('Failed to load outputs');
  const d = await r.json();
  return d.files ?? [];
}

export async function createJob(
  source: JobSource,
  preset: string,
  kind: 'encode' | 'preview' = 'encode',
  keepTracks = false
): Promise<EncodeJob> {
  const r = await fetch('/api/jobs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ source, preset, kind, keep_tracks: keepTracks })
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(err.detail || 'Failed to create job');
  }
  return r.json();
}

export async function deleteJob(id: number): Promise<void> {
  const r = await fetch(`/api/jobs/${id}`, { method: 'DELETE' });
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(err.detail || 'Delete failed');
  }
}

export async function cancelJob(id: number): Promise<void> {
  const r = await fetch(`/api/jobs/${id}/cancel`, { method: 'POST' });
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(err.detail || 'Cancel failed');
  }
}

export async function pauseEncodeQueue(): Promise<{ queue_paused: boolean }> {
  const r = await fetch('/api/encode/pause', { method: 'POST' });
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(err.detail || 'Pause failed');
  }
  return r.json();
}

export async function resumeEncodeQueue(): Promise<{ queue_paused: boolean }> {
  const r = await fetch('/api/encode/resume', { method: 'POST' });
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(err.detail || 'Resume failed');
  }
  return r.json();
}

export async function deleteLibrary(id: number): Promise<void> {
  const r = await fetch(`/api/library/${id}`, { method: 'DELETE' });
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(err.detail || 'Delete failed');
  }
}

export async function runServerDiff(
  jobId: number,
  position: number,
  mode: ServerDiffMode
): Promise<{ image: string; mse: number; ssim: number; psnr: number | null }> {
  const params = new URLSearchParams({
    position: String(position),
    mode
  });
  const r = await fetch(`/api/comparisons/${jobId}/diff?${params}`, {
    method: 'POST'
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(err.detail || 'Diff failed');
  }
  return r.json();
}

export async function fetchPreviewFrame(
  jobId: number,
  index: number,
  offset?: number
): Promise<{
  index: number;
  offset: number;
  source_index: number;
  raw_source_index: number;
  usable_frame_count: number;
  source_frame_count: number;
  source_oob: boolean;
  source_shortfall: number;
  used_full_source: boolean;
  source: string;
  dest: string;
}> {
  const params = new URLSearchParams({ index: String(index) });
  if (offset != null) params.set('offset', String(offset));
  const r = await fetch(`/api/jobs/${jobId}/preview-frame?${params}`);
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(err.detail || 'Preview frame failed');
  }
  return r.json();
}

export async function fetchPreviewNoise(jobId: number): Promise<{
  values: number[];
  frame_count: number;
  best_index: number;
  best_mse: number;
  source_center_index: number;
  suggested_offset: number;
  pad_frames: number;
  scene_cuts: number[];
  selection_start: number;
  selection_end: number;
  scale_max: number;
  source_frame_count: number;
}> {
  const r = await fetch(`/api/jobs/${jobId}/preview-noise`);
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(err.detail || 'Noise graph failed');
  }
  return r.json();
}

export async function runPreviewDiff(
  jobId: number,
  index: number,
  mode: ServerDiffMode,
  offset?: number
): Promise<{ image: string; mse: number; ssim: number; psnr: number | null }> {
  const params = new URLSearchParams({
    index: String(index),
    mode
  });
  if (offset != null) params.set('offset', String(offset));
  const r = await fetch(`/api/jobs/${jobId}/preview-diff?${params}`, {
    method: 'POST'
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(err.detail || 'Preview diff failed');
  }
  return r.json();
}

export async function setFrameOffset(
  jobId: number,
  offset: number,
  locked?: boolean
): Promise<EncodeJob> {
  const params = new URLSearchParams({ offset: String(offset) });
  if (locked != null) params.set('locked', locked ? 'true' : 'false');
  const r = await fetch(`/api/jobs/${jobId}/frame-offset?${params}`, {
    method: 'PATCH'
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(err.detail || 'Failed to set offset');
  }
  return r.json();
}

export async function computePreviewNoiseScore(
  jobId: number,
  offset?: number
): Promise<{
  frame_count: number;
  sampled: number;
  skipped: number;
  offset: number;
  ssim_mean: number;
  ssim_std: number;
  psnr_mean: number | null;
  psnr_std: number | null;
  mse_mean: number;
  mse_std: number;
  noise_score: number;
  job: EncodeJob;
}> {
  const params = new URLSearchParams();
  if (offset != null) params.set('offset', String(offset));
  const q = params.toString();
  const r = await fetch(`/api/jobs/${jobId}/preview-noise-score${q ? `?${q}` : ''}`, {
    method: 'POST'
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(err.detail || 'Noise score failed');
  }
  return r.json();
}

const CHUNK = 16 * 1024 * 1024;

export async function uploadLibraryFile(
  file: File,
  onProgress?: (pct: number) => void
): Promise<number> {
  const totalChunks = Math.ceil(file.size / CHUNK) || 1;
  for (let i = 0; i < totalChunks; i++) {
    const blob = file.slice(i * CHUNK, Math.min((i + 1) * CHUNK, file.size));
    const form = new FormData();
    form.append('chunk', blob, file.name);
    form.append('chunk_index', String(i));
    form.append('total_chunks', String(totalChunks));
    form.append('filename', file.name);
    form.append('compression', 'none');
    const r = await fetch('/api/library/chunk', { method: 'POST', body: form });
    if (!r.ok) {
      const err = await r.json().catch(() => ({ detail: r.statusText }));
      throw new Error(err.detail || 'Upload failed');
    }
    const d = await r.json();
    onProgress?.(Math.round(((i + 1) / totalChunks) * 100));
    if (d.status === 'complete' && d.id != null) {
      return d.id as number;
    }
  }
  throw new Error('Upload did not return library id');
}

export async function uploadExternalPair(
  source: File,
  dest: File,
  onProgress?: (label: string, pct: number) => void
): Promise<number> {
  const sessionId = crypto.randomUUID();

  async function uploadSide(file: File, side: 'source' | 'dest') {
    const totalChunks = Math.ceil(file.size / CHUNK) || 1;
    for (let i = 0; i < totalChunks; i++) {
      const blob = file.slice(i * CHUNK, Math.min((i + 1) * CHUNK, file.size));
      const form = new FormData();
      form.append('chunk', blob, file.name);
      form.append('chunk_index', String(i));
      form.append('total_chunks', String(totalChunks));
      form.append('filename', file.name);
      form.append('side', side);
      form.append('session_id', sessionId);
      form.append('compression', 'none');
      const r = await fetch('/api/comparisons/external/chunk', {
        method: 'POST',
        body: form
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({ detail: r.statusText }));
        throw new Error(err.detail || `Upload failed (${side})`);
      }
      const d = await r.json();
      onProgress?.(side, Math.round(((i + 1) / totalChunks) * 100));
      if (d.status === 'complete' && d.job_id != null) {
        return d.job_id as number;
      }
    }
    return null;
  }

  await uploadSide(source, 'source');
  const jobId = await uploadSide(dest, 'dest');
  if (jobId == null) throw new Error('External compare did not return a job id');
  return jobId;
}
