from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Source(SQLModel, table=True):
    """Chaîne surveillée (Twitch ou YouTube)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    platform: str  # twitch / youtube
    url: str
    enabled: bool = True
    record_lives: bool = True
    include_videos: bool = True  # vidéos YouTube / VOD Twitch publiées
    initialized: bool = False  # premier scan fait (l'historique existant est ignoré)
    last_checked: Optional[datetime] = None
    created_at: datetime = Field(default_factory=now)


class Video(SQLModel, table=True):
    """Vidéo ou live à traiter."""

    id: Optional[int] = Field(default=None, primary_key=True)
    source_id: Optional[int] = Field(default=None, foreign_key="source.id")
    key: str = Field(index=True, unique=True)  # identifiant unique (plateforme:id)
    url: str
    title: str = ""
    is_live: bool = False
    # new, recording, downloading, downloaded, transcribing, analyzing, rendering, done, failed, skipped
    status: str = Field(default="new", index=True)
    error: str = ""
    path: str = ""
    duration: float = 0.0
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class Clip(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    video_id: int = Field(foreign_key="video.id", index=True)
    group: str = Field(index=True)  # même valeur pour toutes les parties d'un clip
    part: int = 1
    part_count: int = 1
    start: float
    end: float
    score: float = 0.0
    title: str = ""
    description: str = ""
    path: str = ""
    status: str = "ready"  # ready, failed, deleted
    created_at: datetime = Field(default_factory=now)


class Post(SQLModel, table=True):
    """Publication d'un clip sur une plateforme."""

    id: Optional[int] = Field(default=None, primary_key=True)
    clip_id: int = Field(foreign_key="clip.id", index=True)
    platform: str  # youtube / tiktok
    status: str = Field(default="pending", index=True)  # pending, publishing, published, failed, cancelled
    remote_id: str = ""
    error: str = ""
    attempts: int = 0
    published_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=now)


class Credential(SQLModel, table=True):
    """Jetons OAuth des comptes connectés."""

    platform: str = Field(primary_key=True)
    data: str  # JSON
    account: str = ""
    updated_at: datetime = Field(default_factory=now)


class ClipInfo(SQLModel, table=True):
    """Caractéristiques d'un clip au moment de sa création (servent à apprendre ce qui marche)."""

    clip_id: int = Field(foreign_key="clip.id", primary_key=True)
    data: str = "{}"  # JSON : durée, sonie, parole, excitation, notes IA, accroche…


class PostStat(SQLModel, table=True):
    """Relevé des statistiques d'une publication (un relevé toutes les quelques heures)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    post_id: int = Field(foreign_key="post.id", index=True)
    fetched_at: datetime = Field(default_factory=now, index=True)
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    avg_view_pct: Optional[float] = None  # rétention moyenne (% du clip regardé), YouTube seulement
    avg_view_s: Optional[float] = None


class Tuning(SQLModel, table=True):
    """Réglages appris à partir des performances (une ligne par analyse, la dernière s'applique)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=now)
    samples: int = 0
    data: str = "{}"  # JSON
