import type { EncodeJob, ServerDiffMode } from './types';

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

const CHUNK = 16 * 1024 * 1024;

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
      onProgress?.(
        side,
        Math.round(((i + 1) / totalChunks) * 100)
      );
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
