export type JobFrame = {
  id: number;
  position: number;
  label: string;
  source_url: string | null;
  dest_url: string | null;
};

export type EncodeJob = {
  id: number;
  filename: string;
  preset: string;
  status: string;
  origin: string;
  progress: number;
  fps: number | null;
  eta_seconds: number | null;
  output_size: number | null;
  output_size_human: string | null;
  error: string | null;
  frames: JobFrame[];
  created_at: string | null;
};

export type CapturedFrame = {
  dataUrl: string;
  width: number;
  height: number;
};

export type CompareMode = 'side-by-side' | 'overlay' | 'abs-diff';
export type ServerDiffMode = 'absdiff' | 'ssim_map';
