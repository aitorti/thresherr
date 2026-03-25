from sqlalchemy import Column, Integer, String, ForeignKey
from sqlalchemy.orm import relationship
from database import Base

class Profile(Base):
    __tablename__ = "profiles"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)
    video_codec = Column(String)
    container = Column(String)
    video_max_res = Column(Integer)
    video_max_bitrate = Column(Integer)
    audio_codec = Column(String)
    audio_def_language = Column(String, nullable=True)
    audio_languages = Column(String, nullable=True)
    subtitle_codec = Column(String)
    subtitle_def_language = Column(String, nullable=True)
    subtitle_languages = Column(String, nullable=True)

class Library(Base):
    __tablename__ = "libraries"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)
    media_path = Column(String)
    temp_path = Column(String)
    profile_id = Column(Integer, ForeignKey("profiles.id"))

    # Relationship to access profile data from a library
    profile = relationship("Profile")
