from sqlalchemy import (
    Column,
    Integer,
    String,
    ForeignKey,
    BigInteger,
    DateTime,
    Text
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base


# --------------------
# Profiles
# --------------------
class Profile(Base):
    __tablename__ = "profiles"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)

    # Video rules
    video_codec = Column(String, nullable=False)
    container = Column(String, nullable=False)
    video_max_res = Column(Integer, nullable=False)
    video_max_bitrate = Column(Integer, nullable=False)

    # Audio rules
    audio_codec = Column(String, nullable=False)
    audio_def_language = Column(String, nullable=True)
    audio_languages = Column(String, nullable=True)  # comma-separated whitelist

    # Subtitle rules
    subtitle_codec = Column(String, nullable=False)
    subtitle_def_language = Column(String, nullable=True)
    subtitle_languages = Column(String, nullable=True)  # comma-separated whitelist

    libraries = relationship(
        "Library",
        back_populates="profile",
        cascade="all, delete-orphan"
    )


# --------------------
# Libraries
# --------------------
class Library(Base):
    __tablename__ = "libraries"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)

    media_path = Column(String, nullable=False)
    temp_path = Column(String, nullable=False)

    profile_id = Column(Integer, ForeignKey("profiles.id"), nullable=False)
    profile = relationship("Profile", back_populates="libraries")

    media_files = relationship(
        "MediaFile",
        back_populates="library",
        cascade="all, delete-orphan"
    )


# --------------------
# Media files (worker subject)
# --------------------
class MediaFile(Base):
    __tablename__ = "media_files"

    id = Column(Integer, primary_key=True, index=True)

    # Identity
    file_name = Column(String, nullable=False)
    full_path = Column(String, unique=True, nullable=False)

    library_id = Column(Integer, ForeignKey("libraries.id"), nullable=False)
    library = relationship("Library", back_populates="media_files")

    # --------------------
    # State machine
    # --------------------
    status = Column(String, nullable=False, default="pending")
    # Contractual values:
    # pending | queued | processing | completed | failed

    # --------------------
    # Timestamps (traceability)
    # --------------------
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    # --------------------
    # Technical metadata (SUMMARY ONLY, UI/cache)
    # The worker MUST NOT trust these for decisions
    # --------------------
    video_codec = Column(String, nullable=True)
    resolution = Column(String, nullable=True)
    audio_codec = Column(String, nullable=True)
    audio_languages = Column(String, nullable=True)
    subtitle_codec = Column(String, nullable=True)
    subtitle_languages = Column(String, nullable=True)

    # --------------------
    # Space tracking
    # --------------------
    size_original = Column(BigInteger, nullable=True)
    size_final = Column(BigInteger, nullable=True)

    # --------------------
    # Worker planning & verification
    # --------------------
    job_plan = Column(
        Text,
        nullable=True
    )
    # JSON/text describing WHAT the worker decided to do:
    # - transcode video (why)
    # - remux container
    # - audio streams kept / transcoded / removed
    # - subtitle streams kept / removed
    # - expected warnings

    verification_result = Column(
        Text,
        nullable=True
    )
    # Result of post-processing verification:
    # - "ok"
    # - "failed: <reason>"

    # --------------------
    # Worker diagnostics
    # --------------------
    warnings = Column(
        Text,
        nullable=True
    )
    # Non-blocking issues:
    # - missing audio languages
    # - missing subtitle languages

    last_error = Column(
        Text,
        nullable=True
    )
    # Blocking error message if status == failed
