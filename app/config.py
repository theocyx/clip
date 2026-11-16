"""Configuration, lue depuis les variables d'environnement (ou le fichier .env)."""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Général
    data_dir: Path = Path("/data")
    public_url: str = "http://localhost:8000"  # URL publique (sert aux redirections OAuth)
    admin_user: str = "admin"
    admin_password: str = "change-moi"

    # Surveillance des sources
    poll_interval: int = 600  # secondes entre deux vérifications des chaînes

    # Transcription (faster-whisper, CPU)
    whisper_model: str = "small"  # tiny / base / small / medium
    whisper_language: str = "fr"
    whisper_threads: int = 0  # 0 = automatique

    # IA locale (Ollama) pour classer les moments et écrire les titres. Vide = désactivé.
    ollama_url: str = "http://ollama:11434"
    ollama_model: str = "qwen2.5:3b"

    # Clips
    clips_per_video: int = 3  # nombre max de clips par vidéo d'une heure (proportionnel à la durée)
    clips_max_per_video: int = 10
    clip_target: float = 60.0  # durée visée (s)
    clip_min: float = 35.0
    clip_max: float = 180.0  # au-delà, le clip est coupé en parties
    part_max: float = 90.0  # durée max d'une partie
    min_score: float = 0.0

    # Rendu vidéo
    video_codec: str = "libx264"  # ou h264_vaapi (GPU intégré Intel/AMD)
    vaapi_device: str = ""  # ex. /dev/dri/renderD128 pour h264_vaapi
    crf: int = 21
    font_name: str = "Montserrat"
    hook_title: bool = True  # accroche courte en haut de la vidéo
    hook_seconds: float = 0  # durée d'affichage de l'accroche (0 = tout le clip)
    keep_source_videos: bool = False

    # Publication
    auto_publish: bool = True
    publish_youtube: bool = True
    publish_tiktok: bool = True
    min_minutes_between_posts: int = 90
    max_posts_per_day: int = 6  # par plateforme (quota YouTube : ~6 envois/jour)
    hashtags: str = "#shorts #clip #fr"

    youtube_client_id: str = ""
    youtube_client_secret: str = ""
    youtube_privacy: str = "public"  # public / unlisted / private
    youtube_category: str = "20"  # 20 = Gaming, 24 = Divertissement

    tiktok_client_key: str = ""
    tiktok_client_secret: str = ""
    tiktok_mode: str = "direct"  # direct (publication) ou draft (brouillon dans l'appli TikTok)
    tiktok_privacy: str = "SELF_ONLY"  # SELF_ONLY tant que l'app n'est pas auditée, puis PUBLIC_TO_EVERYONE

    @property
    def db_path(self) -> Path:
        return self.data_dir / "clip.db"

    @property
    def videos_dir(self) -> Path:
        return self.data_dir / "videos"

    @property
    def clips_dir(self) -> Path:
        return self.data_dir / "clips"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    for d in (s.data_dir, s.videos_dir, s.clips_dir):
        d.mkdir(parents=True, exist_ok=True)
    return s
