from sqlalchemy import Column, Integer, String, ForeignKey
from sqlalchemy.orm import relationship
from database import Base

class Profile(Base):
    """
    Represents the 'contract' or rules that a media library must follow.
    """
    __tablename__ = "profiles"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    
    # Video constraints
    container = Column(String)  # e.g., 'mkv', 'mp4'
    video_codec = Column(String)  # e.g., 'h264', 'h265'
    video_max_res = Column(Integer)  # e.g., 1080, 2160
    video_max_bitrate = Column(Integer)  # in kbps
    
    # Audio constraints
    audio_codec = Column(String)  # e.g., 'ac3', 'eac3', 'aac'
    audio_def_language = Column(String)  # Default track language (e.g., 'eng')
    audio_languages = Column(String)  # Comma-separated whitelist (e.g., 'eng,spa,fra')
    
    # Subtitle constraints
    subtitle_codec = Column(String)  # e.g., 'srt', 'ass'
    subtitle_def_language = Column(String)  # Default subtitle (e.g., 'eng-forced')
    subtitle_languages = Column(String)  # Comma-separated whitelist

    # Relationship with libraries
    libraries = relationship("Library", back_populates="profile")

class Library(Base):
    """
    Represents a physical media folder linked to a specific Profile.
    """
    __tablename__ = "libraries"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True)
    source_path = Column(String, unique=True)  # Path to monitor
    temp_path = Column(String)  # Path for processing
    
    profile_id = Column(Integer, ForeignKey("profiles.id"))
    profile = relationship("Profile", back_populates="libraries")
