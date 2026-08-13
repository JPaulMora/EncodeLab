"""ORM models for library files, encode jobs, and comparison frames."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LibraryFile(Base):
    __tablename__ = "library_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    jobs: Mapped[list[EncodeJob]] = relationship(
        "EncodeJob",
        back_populates="library_file",
        foreign_keys="EncodeJob.library_file_id",
    )


class EncodeJob(Base):
    __tablename__ = "encode_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    preset: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued"
    )  # queued|encoding|extracting|done|failed|cancelled|previewing|preview_ready
    origin: Mapped[str] = mapped_column(
        String(32), nullable=False, default="encode"
    )  # encode|external
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="encode"
    )  # encode|preview
    progress: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    fps: Mapped[float | None] = mapped_column(Float, nullable=True)
    eta_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    dest_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    output_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    library_file_id: Mapped[int | None] = mapped_column(
        ForeignKey("library_files.id", ondelete="SET NULL"), nullable=True
    )
    parent_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("encode_jobs.id", ondelete="SET NULL"), nullable=True
    )
    frame_offset: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    align_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_clip_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    dest_clip_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Aggregate quality after locked-offset pass over usable preview frames
    noise_ssim_mean: Mapped[float | None] = mapped_column(Float, nullable=True)
    noise_ssim_std: Mapped[float | None] = mapped_column(Float, nullable=True)
    noise_psnr_mean: Mapped[float | None] = mapped_column(Float, nullable=True)
    noise_psnr_std: Mapped[float | None] = mapped_column(Float, nullable=True)
    noise_mse_mean: Mapped[float | None] = mapped_column(Float, nullable=True)
    noise_mse_std: Mapped[float | None] = mapped_column(Float, nullable=True)
    noise_frame_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    encode_duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    library_file: Mapped[LibraryFile | None] = relationship(
        "LibraryFile",
        back_populates="jobs",
        foreign_keys=[library_file_id],
    )
    parent_job: Mapped[EncodeJob | None] = relationship(
        "EncodeJob",
        remote_side=[id],
        foreign_keys=[parent_job_id],
    )
    frames: Mapped[list[ComparisonFrame]] = relationship(
        "ComparisonFrame",
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="ComparisonFrame.position",
    )


class ComparisonFrame(Base):
    __tablename__ = "comparison_frames"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("encode_jobs.id", ondelete="CASCADE"))
    position: Mapped[float] = mapped_column(Float, nullable=False)  # 0.25 | 0.50 | 0.75
    source_png: Mapped[str] = mapped_column(String(1024), nullable=False)
    dest_png: Mapped[str] = mapped_column(String(1024), nullable=False)

    job: Mapped[EncodeJob] = relationship("EncodeJob", back_populates="frames")
