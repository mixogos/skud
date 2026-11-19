from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Table
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    """Текущее время UTC без tzinfo (SQLite не хранит часовой пояс)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    permissions: Mapped[list] = mapped_column(JSON, default=list)
    predefined: Mapped[bool] = mapped_column(Boolean, default=False)
    created_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    users: Mapped[list["User"]] = relationship(back_populates="role")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)  # логин
    real_name: Mapped[str] = mapped_column(String(256), default="")
    password_hash: Mapped[str] = mapped_column(String(256))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Первый администратор: его нельзя удалить, отключить или лишить прав.
    is_superadmin: Mapped[bool] = mapped_column(Boolean, default=False)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"))
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    modified_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    role: Mapped[Role] = relationship(back_populates="users")
    sessions: Mapped[list["Session"]] = relationship(back_populates="user", cascade="all, delete-orphan")

    def has_perm(self, perm: str) -> bool:
        return self.is_superadmin or perm in (self.role.permissions or [])


class Session(Base):
    """Сессия пользователя на устройстве. Сам токен не хранится — только его SHA-256."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires: Mapped[datetime] = mapped_column(DateTime)
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(512), default="")

    user: Mapped[User] = relationship(back_populates="sessions")


class AuditLog(Base):
    """Журнал действий пользователей."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    # Без внешнего ключа: запись журнала должна пережить удаление пользователя.
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    user_name: Mapped[str] = mapped_column(String(128), default="")
    action: Mapped[str] = mapped_column(String(32), index=True)  # login, create, update, delete...
    object_type: Mapped[str] = mapped_column(String(64), default="", index=True)
    object_id: Mapped[str] = mapped_column(String(64), default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    ip: Mapped[str] = mapped_column(String(64), default="")


# --- Картотека ---
card_watch_lists = Table(
    "card_watch_lists",
    Base.metadata,
    Column("card_id", ForeignKey("human_cards.id", ondelete="CASCADE"), primary_key=True),
    Column("watch_list_id", ForeignKey("watch_lists.id", ondelete="CASCADE"), primary_key=True),
)


class WatchList(Base):
    """Список наблюдения: классификация карточек (персонал, VIP, нежелательные и т. д.)."""

    __tablename__ = "watch_lists"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(256), unique=True)
    comment: Mapped[str] = mapped_column(String(2048), default="")
    color: Mapped[str] = mapped_column(String(7), default="#3d7eff")  # цвет в уведомлениях
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Индивидуальный порог схожести; None — использовать общий.
    face_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    acknowledge: Mapped[bool] = mapped_column(Boolean, default=False)  # требовать принятия событий
    notify: Mapped[bool] = mapped_column(Boolean, default=False)  # звуковое оповещение
    ignore_events: Mapped[bool] = mapped_column(Boolean, default=False)  # не создавать события
    # Контроль доступа: allow — список разрешает проход, deny — запрещает, none — не влияет.
    access_policy: Mapped[str] = mapped_column(String(8), default="none")
    # Расписание прохода по дням недели: {"monday": [["08:00", "20:00"]], ...}; пусто — круглосуточно.
    access_schedule: Mapped[dict] = mapped_column(JSON, default=dict)
    predefined: Mapped[bool] = mapped_column(Boolean, default=False)
    created_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    modified_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    cards: Mapped[list["HumanCard"]] = relationship(secondary=card_watch_lists, back_populates="watch_lists")


class HumanCard(Base):
    """Карточка человека."""

    __tablename__ = "human_cards"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(256), index=True)
    comment: Mapped[str] = mapped_column(String(2048), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)  # произвольные дополнительные поля
    # Период действия карточки (UTC); None — без ограничения.
    active_after: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    active_before: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Личное расписание прохода; если задано, заменяет расписание списков.
    access_schedule: Mapped[dict] = mapped_column(JSON, default=dict)
    created_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    modified_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    watch_lists: Mapped[list[WatchList]] = relationship(secondary=card_watch_lists, back_populates="cards")
    faces: Mapped[list["FaceObject"]] = relationship(
        back_populates="card", cascade="all, delete-orphan", order_by="FaceObject.id"
    )


class FaceObject(Base):
    """Фото лица в карточке вместе с вектором признаков."""

    __tablename__ = "face_objects"

    id: Mapped[int] = mapped_column(primary_key=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("human_cards.id", ondelete="CASCADE"), index=True)
    embedding: Mapped[bytes] = mapped_column(LargeBinary)  # 512 чисел float32
    bbox: Mapped[list] = mapped_column(JSON, default=list)  # left, top, right, bottom на исходном фото
    detection_score: Mapped[float] = mapped_column(Float, default=0.0)
    has_source_photo: Mapped[bool] = mapped_column(Boolean, default=False)
    created_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    card: Mapped[HumanCard] = relationship(back_populates="faces")


# --- Источники видео ---
class CameraGroup(Base):
    """Группа камер: объединяет камеры и видеофайлы по месту расположения."""

    __tablename__ = "camera_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(256), unique=True)
    comment: Mapped[str] = mapped_column(String(2048), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Дедупликация событий в пределах группы (используется при обработке событий).
    deduplicate: Mapped[bool] = mapped_column(Boolean, default=False)
    deduplicate_delay: Mapped[int] = mapped_column(Integer, default=15)  # секунд
    face_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Точка прохода: для событий группы принимается решение «пропустить / отказать».
    access_control: Mapped[bool] = mapped_column(Boolean, default=False)
    # Проверка живости: при подозрении на фото или экран в проходе отказывают.
    liveness_check: Mapped[bool] = mapped_column(Boolean, default=False)
    # Детекция людей (силуэтов) и их атрибутов — отдельно от лиц, ловит и тех, кто спиной.
    detect_people: Mapped[bool] = mapped_column(Boolean, default=False)
    predefined: Mapped[bool] = mapped_column(Boolean, default=False)
    created_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    modified_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Camera(Base):
    """Камера (видеопоток, который сервер читает сам) или внешний детектор (устройство присылает кадры)."""

    __tablename__ = "cameras"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    group_id: Mapped[int] = mapped_column(ForeignKey("camera_groups.id"), index=True)
    url: Mapped[str] = mapped_column(String(1024), default="")
    comment: Mapped[str] = mapped_column(String(2048), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    external_detector: Mapped[bool] = mapped_column(Boolean, default=False)
    token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)  # для внешних детекторов
    stream_settings: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(32), default="DISABLED")
    state_message: Mapped[str] = mapped_column(String(1024), default="")
    created_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    modified_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    group: Mapped[CameraGroup] = relationship()


class VideoArchive(Base):
    """Загруженный видеофайл и состояние его обработки."""

    __tablename__ = "video_archives"

    # Состояния: UPLOADED → QUEUED → PROCESSING → COMPLETED | FAILED | CANCELLED
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    camera_group_id: Mapped[int] = mapped_column(ForeignKey("camera_groups.id"), index=True)
    file_name: Mapped[str] = mapped_column(String(256), default="")  # имя файла в хранилище
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(16), default="UPLOADED", index=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    progress: Mapped[float] = mapped_column(Float, default=0.0)  # 0..1
    error: Mapped[str] = mapped_column(String(1024), default="")
    duration: Mapped[float] = mapped_column(Float, default=0.0)  # секунд
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    frames_processed: Mapped[int] = mapped_column(Integer, default=0)
    events_created: Mapped[int] = mapped_column(Integer, default=0)
    stream_settings: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    camera_group: Mapped[CameraGroup] = relationship()


# --- События ---
class FaceEvent(Base):
    """Событие распознавания лица: лучший кадр одного трека (прохода человека перед камерой)."""

    __tablename__ = "face_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    camera_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("camera_groups.id", ondelete="SET NULL"), nullable=True, index=True
    )
    camera_id: Mapped[int | None] = mapped_column(ForeignKey("cameras.id", ondelete="SET NULL"), nullable=True, index=True)
    video_archive_id: Mapped[int | None] = mapped_column(
        ForeignKey("video_archives.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # Положение в видеофайле, секунды от начала (для событий из файлов).
    video_offset: Mapped[float | None] = mapped_column(Float, nullable=True)
    track_duration: Mapped[float] = mapped_column(Float, default=0.0)
    track_frames: Mapped[int] = mapped_column(Integer, default=1)

    bbox: Mapped[list] = mapped_column(JSON, default=list)
    frame_width: Mapped[int] = mapped_column(Integer, default=0)
    frame_height: Mapped[int] = mapped_column(Integer, default=0)
    detection_score: Mapped[float] = mapped_column(Float, default=0.0)
    quality: Mapped[float] = mapped_column(Float, default=0.0)
    # Оценка живости 0..1 (вероятность, что лицо настоящее); None — проверка не выполнялась.
    liveness: Mapped[float | None] = mapped_column(Float, nullable=True)
    embedding: Mapped[bytes] = mapped_column(LargeBinary)
    features: Mapped[dict] = mapped_column(JSON, default=dict)  # атрибуты лица (следующие этапы)

    # Сопоставление с картотекой.
    matched: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    matched_card_id: Mapped[int | None] = mapped_column(
        ForeignKey("human_cards.id", ondelete="SET NULL"), nullable=True, index=True
    )
    matched_face_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    matched_lists: Mapped[list] = mapped_column(JSON, default=list)

    # Принятие события оператором.
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    acknowledged_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    acknowledged_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    acknowledged_reaction: Mapped[str] = mapped_column(String(64), default="")

    episode_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    # Решение о доступе (только для групп камер с контролем доступа): granted | denied.
    access: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    access_reason: Mapped[str] = mapped_column(String(256), default="")


class HumanEpisode(Base):
    """Эпизод: события одного человека, идущие подряд в течение короткого времени."""

    __tablename__ = "human_episodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_date: Mapped[datetime] = mapped_column(DateTime, index=True)  # время первого события
    last_event_date: Mapped[datetime] = mapped_column(DateTime, index=True)
    closed_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    open: Mapped[bool] = mapped_column(Boolean, default=True, index=True)  # LIVE-эпизод
    events_count: Mapped[int] = mapped_column(Integer, default=0)
    # Лучшее событие эпизода: его миниатюра и вектор представляют эпизод.
    best_event_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    best_quality: Mapped[float] = mapped_column(Float, default=0.0)
    embedding: Mapped[bytes] = mapped_column(LargeBinary)
    matched: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    matched_card_id: Mapped[int | None] = mapped_column(
        ForeignKey("human_cards.id", ondelete="SET NULL"), nullable=True, index=True
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    matched_lists: Mapped[list] = mapped_column(JSON, default=list)
    camera_groups: Mapped[list] = mapped_column(JSON, default=list)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    video_archive_id: Mapped[int | None] = mapped_column(
        ForeignKey("video_archives.id", ondelete="CASCADE"), nullable=True, index=True
    )


class PersonEvent(Base):
    """Событие по человеку (силуэту): лучший кадр одного прохода с атрибутами внешнего вида."""

    __tablename__ = "person_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    camera_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("camera_groups.id", ondelete="SET NULL"), nullable=True, index=True
    )
    camera_id: Mapped[int | None] = mapped_column(ForeignKey("cameras.id", ondelete="SET NULL"), nullable=True, index=True)
    track_duration: Mapped[float] = mapped_column(Float, default=0.0)
    track_frames: Mapped[int] = mapped_column(Integer, default=1)
    bbox: Mapped[list] = mapped_column(JSON, default=list)
    frame_width: Mapped[int] = mapped_column(Integer, default=0)
    frame_height: Mapped[int] = mapped_column(Integer, default=0)
    detection_score: Mapped[float] = mapped_column(Float, default=0.0)
    # Пол и признаки внешнего вида (пол, одежда, аксессуары, цвет одежды).
    gender: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    gender_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    attrs: Mapped[dict] = mapped_column(JSON, default=dict)  # hat, bag, coat, longsleeves, longpants, longhair → 0..1
    upper_color: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    lower_color: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)


class Setting(Base):
    """Общие настройки системы, изменяемые во время работы (ключ — значение)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)


class Webhook(Base):
    """Вебхук: при новом событии сервер отправляет его описание на заданный адрес."""

    __tablename__ = "webhooks"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    url: Mapped[str] = mapped_column(String(2048))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Условия отправки: matched, watch_lists, camera_groups, access.
    filters: Mapped[dict] = mapped_column(JSON, default=dict)
    # Секрет для подписи тела запроса (HMAC-SHA256); наружу не выдаётся.
    secret: Mapped[str] = mapped_column(String(256), default="")
    send_attempts: Mapped[int] = mapped_column(Integer, default=3)
    last_status: Mapped[str] = mapped_column(String(16), default="")  # ok | error
    last_error: Mapped[str] = mapped_column(String(512), default="")
    last_sent_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    modified_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# --- Видеоаналитика ---
class Counter(Base):
    """Счётчик: через равные промежутки времени считает лица в кадре камеры (или в его области)."""

    __tablename__ = "counters"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    camera_id: Mapped[int] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    comment: Mapped[str] = mapped_column(String(2048), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    count_interval: Mapped[int] = mapped_column(Integer, default=10)  # секунд между подсчётами
    # Область подсчёта в долях кадра: [left, top, right, bottom]; пусто — весь кадр.
    roi: Mapped[list] = mapped_column(JSON, default=list)
    created_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    modified_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class CounterRecord(Base):
    """Запись счётчика: сколько лиц было в кадре в момент подсчёта."""

    __tablename__ = "counter_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    counter_id: Mapped[int] = mapped_column(ForeignKey("counters.id", ondelete="CASCADE"), index=True)
    created_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    faces_count: Mapped[int] = mapped_column(Integer, default=0)
    # Рамки посчитанных лиц в долях кадра — чтобы показать их на сохранённом снимке.
    faces_bbox: Mapped[list] = mapped_column(JSON, default=list)
    has_image: Mapped[bool] = mapped_column(Boolean, default=False)  # сохранён ли снимок кадра


class Line(Base):
    """Линия в кадре камеры: фиксируются пересечения её людьми в обе стороны."""

    __tablename__ = "lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    camera_id: Mapped[int] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    comment: Mapped[str] = mapped_column(String(2048), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Две точки в долях кадра: [[x1, y1], [x2, y2]]. Если смотреть от первой точки ко второй,
    # сторона A — слева, сторона B — справа.
    points: Mapped[list] = mapped_column(JSON, default=list)
    created_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    modified_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class LineCrossing(Base):
    """Пересечение линии: кто (если распознан), когда и в какую сторону."""

    __tablename__ = "line_crossings"

    id: Mapped[int] = mapped_column(primary_key=True)
    line_id: Mapped[int] = mapped_column(ForeignKey("lines.id", ondelete="CASCADE"), index=True)
    created_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    direction: Mapped[str] = mapped_column(String(2), index=True)  # AB | BA
    # Заполняются, когда человек уходит из кадра и его лицо распознано.
    face_event_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    matched_card_id: Mapped[int | None] = mapped_column(
        ForeignKey("human_cards.id", ondelete="SET NULL"), nullable=True, index=True
    )


# --- Охранная аналитика ---
class SecurityRule(Base):
    """Правило охранной аналитики на камере: вторжение в зону, праздношатание,
    скопление людей или саботаж камеры. Зона — многоугольник в долях кадра."""

    __tablename__ = "security_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    camera_id: Mapped[int] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    # intrusion | loitering | crowd | tampering
    rule_type: Mapped[str] = mapped_column(String(16), index=True)
    comment: Mapped[str] = mapped_column(String(2048), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Многоугольник зоны: [[x, y], ...] в долях кадра. Пусто = весь кадр.
    zone: Mapped[list] = mapped_column(JSON, default=list)
    # Параметры: dwell_seconds (праздношатание), min_count (скопление), sensitivity (саботаж 1..100).
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    created_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    modified_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SecurityEvent(Base):
    """Охранная тревога: сработавшее правило, кадр и место."""

    __tablename__ = "security_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    camera_id: Mapped[int | None] = mapped_column(ForeignKey("cameras.id", ondelete="SET NULL"), nullable=True, index=True)
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("security_rules.id", ondelete="SET NULL"), nullable=True, index=True)
    rule_type: Mapped[str] = mapped_column(String(16), index=True)
    rule_name: Mapped[str] = mapped_column(String(256), default="")
    severity: Mapped[str] = mapped_column(String(8), default="alarm")  # alarm | warning
    message: Mapped[str] = mapped_column(String(512), default="")
    # Снимок места: зона и рамки людей в долях кадра — чтобы нарисовать на сохранённом кадре.
    zone: Mapped[list] = mapped_column(JSON, default=list)
    boxes: Mapped[list] = mapped_column(JSON, default=list)
    frame_width: Mapped[int] = mapped_column(Integer, default=0)
    frame_height: Mapped[int] = mapped_column(Integer, default=0)
    has_image: Mapped[bool] = mapped_column(Boolean, default=False)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    acknowledged_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    acknowledged_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
