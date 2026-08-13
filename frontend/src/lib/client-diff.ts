import type { CapturedFrame } from './types';

function loadImage(dataUrl: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error('Failed to load image'));
    img.src = dataUrl;
  });
}

function drawToCanvas(
  width: number,
  height: number,
  draw: (ctx: CanvasRenderingContext2D) => void
): HTMLCanvasElement {
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new Error('Could not get canvas context');
  draw(ctx);
  return canvas;
}

export async function urlToFrame(url: string): Promise<CapturedFrame> {
  const img = await loadImage(url);
  return { dataUrl: url, width: img.naturalWidth, height: img.naturalHeight };
}

export async function renderSideBySide(
  frameA: CapturedFrame,
  frameB: CapturedFrame
): Promise<string> {
  const [imgA, imgB] = await Promise.all([
    loadImage(frameA.dataUrl),
    loadImage(frameB.dataUrl)
  ]);
  const height = Math.max(frameA.height, frameB.height);
  const width = frameA.width + frameB.width;
  const canvas = drawToCanvas(width, height, (ctx) => {
    ctx.fillStyle = '#000';
    ctx.fillRect(0, 0, width, height);
    ctx.drawImage(imgA, 0, 0);
    ctx.drawImage(imgB, frameA.width, 0);
  });
  return canvas.toDataURL('image/png');
}

export async function renderOverlay(
  frameA: CapturedFrame,
  frameB: CapturedFrame,
  opacityB: number
): Promise<string> {
  const [imgA, imgB] = await Promise.all([
    loadImage(frameA.dataUrl),
    loadImage(frameB.dataUrl)
  ]);
  const width = Math.max(frameA.width, frameB.width);
  const height = Math.max(frameA.height, frameB.height);
  const canvas = drawToCanvas(width, height, (ctx) => {
    ctx.fillStyle = '#111';
    ctx.fillRect(0, 0, width, height);
    ctx.drawImage(imgA, 0, 0, width, height);
    ctx.globalAlpha = opacityB;
    ctx.drawImage(imgB, 0, 0, width, height);
    ctx.globalAlpha = 1;
  });
  return canvas.toDataURL('image/png');
}

export async function renderAbsDiff(
  frameA: CapturedFrame,
  frameB: CapturedFrame,
  gain: number
): Promise<string> {
  const [imgA, imgB] = await Promise.all([
    loadImage(frameA.dataUrl),
    loadImage(frameB.dataUrl)
  ]);
  const width = Math.max(frameA.width, frameB.width);
  const height = Math.max(frameA.height, frameB.height);

  const tempA = drawToCanvas(width, height, (ctx) => {
    ctx.drawImage(imgA, 0, 0, width, height);
  });
  const tempB = drawToCanvas(width, height, (ctx) => {
    ctx.drawImage(imgB, 0, 0, width, height);
  });

  const ctxA = tempA.getContext('2d')!;
  const ctxB = tempB.getContext('2d')!;
  const dataA = ctxA.getImageData(0, 0, width, height);
  const dataB = ctxB.getImageData(0, 0, width, height);
  const out = ctxA.createImageData(width, height);

  const g = Math.max(1, gain);
  for (let i = 0; i < dataA.data.length; i += 4) {
    out.data[i] = Math.min(255, Math.abs(dataA.data[i] - dataB.data[i]) * g);
    out.data[i + 1] = Math.min(
      255,
      Math.abs(dataA.data[i + 1] - dataB.data[i + 1]) * g
    );
    out.data[i + 2] = Math.min(
      255,
      Math.abs(dataA.data[i + 2] - dataB.data[i + 2]) * g
    );
    out.data[i + 3] = 255;
  }

  const canvas = drawToCanvas(width, height, (ctx) => {
    ctx.putImageData(out, 0, 0);
  });
  return canvas.toDataURL('image/png');
}

export function downloadDataUrl(dataUrl: string, filename: string) {
  const a = document.createElement('a');
  a.href = dataUrl;
  a.download = filename;
  a.click();
}
