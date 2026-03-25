# 🌾 Thresherr
> **Unify and organize your media library. One profile per library. Clear results.**

---

## 🧭 What is Thresherr?
**Thresherr** is a self-hosted, web-based application designed to **standardize** entire media libraries according to a strict, consistent quality profile.

Unlike general-purpose transcoders, Thresherr focuses on **enforcement and consistency**. It analyzes your files, identifies streams that don't fit your "Threshing Profile" (unwanted languages, bloated bitrates, or incompatible formats), and performs the necessary conversions (via FFmpeg) to ensure your library is clean, lean, and predictable.

### ✨ Key Features
* **Library-Specific Profiles:** Define unique rules for Movies, TV Shows, or 4K content.
* **Smart Analysis:** Uses `ffprobe` to scan codecs, bitrates, and language tags.
* **Selective Threshing:** Automatically removes unwanted audio/subtitle tracks ("the chaff") while keeping the essential content ("the grain").
* **Safe Processing:** Works in a `TEMP` directory and only replaces the original file after a successful validation.
* **Web-First UI:** Full control from your browser—no CLI knowledge required. Perfect for the "Arr" suite ecosystem (Radarr, Sonarr, etc.).

---

## 👤 Is Thresherr for you?
| ✅ Thresherr is for you if... | ❌ Thresherr is NOT for you if... |
| :--- | :--- |
| You crave total consistency in your media folders. | You want low-level FFmpeg configuration per file. |
| You want to reclaim space by removing extra languages. | You need a "one-stop shop" with infinite plugins. |
| You prefer a visual, "one-click" logic. | You prefer using the command line for daily tasks. |

---

## 🧪 The Concept: The Threshing Profile
In Thresherr, a **Library** is a contract. If a file doesn't meet the profile, it's flagged for processing.

### A Profile defines:
* **Container:** MKV or MP4.
* **Video:** Codec, Max Resolution, and Max Bitrate.
* **Audio:** Codec, Default Language, and Whitelisted Languages.
* **Subtitles:** Codec, Default (Forced/Full), and Whitelisted Languages.

> [!TIP]
> **Tolerance Rule:** Thresherr cleans what is extra but does not fail if an optional language is missing. It only acts on what exists but doesn't comply with your standard.

---

## 🛠️ Tech Stack
Designed to be lightweight and professional (perfect for **DietPi** or low-resource servers):

* **Backend:** [Python](https://www.python.org/) with [FastAPI](https://fastapi.tiangolo.com/)
* **Frontend:** [HTMX](https://htmx.org/) + Tailwind CSS
* **Database:** SQLite
* **Core Engine:** FFmpeg & FFprobe
* **Deployment:** [Docker](https://www.docker.com/) & Docker Compose

---

## 🚀 Installation (Coming Soon)
```bash
docker-compose up -d
