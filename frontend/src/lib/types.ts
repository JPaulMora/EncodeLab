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
  kind: string;
  progress: number;
  fps: number | null;
  eta_seconds: number | null;
  output_size: number | null;
  output_size_human: string | null;
  source_size: number | null;
  source_size_human: string | null;
  compression_ratio: number | null;
  error: string | null;
  dest_path: string | null;
  library_file_id: number | null;
  parent_job_id: number | null;
  source_label: string;
  frame_offset: number;
  align_confidence: number | null;
  has_preview_clips: boolean;
  noise_score: number | null;
  noise_ssim_mean: number | null;
  noise_ssim_std: number | null;
  noise_psnr_mean: number | null;
  noise_psnr_std: number | null;
  noise_mse_mean: number | null;
  noise_mse_std: number | null;
  noise_frame_count: number | null;
  encode_duration_seconds: number | null;
  keep_tracks: boolean;
  frames: JobFrame[];
  created_at: string | null;
  updated_at?: string | null;
  download_url: string | null;
};

export type LibraryFile = {
  id: number;
  original_filename: string;
  size: number;
  size_human: string;
  created_at: string | null;
  download_url: string;
};

export type OutputFile = {
  job_id: number;
  name: string;
  display_name: string;
  preset: string;
  size: number;
  size_human: string;
  source_label: string;
  download_url: string;
  delete_url: string;
};

export type CapturedFrame = {
  dataUrl: string;
  width: number;
  height: number;
};

export type CompareMode = 'side-by-side' | 'overlay' | 'abs-diff' | 'subtract';
export type ServerDiffMode = 'absdiff' | 'ssim_map';

export type JobSource =
  | { type: 'library'; id: number }
  | { type: 'job'; id: number };
