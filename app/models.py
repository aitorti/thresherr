from sqlalchemy import Column, Integer, String, ForeignKey, BigInteger
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

    libraries = relationship("Library", back_populates="profile", cascade="all, delete-orphan")

class Library(Base): 
    __tablename__ = "libraries" 
    id = Column(Integer, primary_key=True, index=True) 
    name = Column(String) 
    media_path = Column(String) 
    temp_path = Column(String) 
    profile_id = Column(Integer, ForeignKey("profiles.id")) 

    profile = relationship("Profile", back_populates="libraries")

class MediaFile(Base):
    __tablename__ = "media_files"
    id = Column(Integer, primary_key=True, index=True)
    file_name = Column(String)
    full_path = Column(String, unique=True)
    status = Column(String, default="pending") # pending, processing, completed, failed
    
    # Tracking space (BigInteger for large files in bytes)
    size_original = Column(BigInteger, nullable=True)
    size_final = Column(BigInteger, nullable=True)
    
    library_id = Column(Integer, ForeignKey("libraries.id"))
    library = relationship("Library")
