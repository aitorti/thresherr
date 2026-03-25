import os
from sqlalchemy.orm import Session
import models

VIDEO_EXTENSIONS = ('.mkv', '.mp4', '.avi', '.mov', '.m4v')

def scan_libraries(db: Session):
    libraries = db.query(models.Library).all()
    new_files_count = 0

    for lib in libraries:
        if not os.path.exists(lib.media_path):
            continue

        for root, dirs, files in os.walk(lib.media_path):
            for file in files:
                if file.lower().endswith(VIDEO_EXTENSIONS):
                    full_path = os.path.join(root, file)
                    
                    # Check if file already exists in DB
                    exists = db.query(models.MediaFile).filter(models.MediaFile.full_path == full_path).first()
                    
                    if not exists:
                        new_media = models.MediaFile(
                            file_name=file,
                            full_path=full_path,
                            library_id=lib.id,
                            status="pending"
                        )
                        db.add(new_media)
                        new_files_count += 1
    
    db.commit()
    return new_files_count
