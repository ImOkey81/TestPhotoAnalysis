import base64
import logging
import os
import random
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np
import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request
from flask_cors import CORS
from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, String, Text, create_engine, desc
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, selectinload, sessionmaker
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("photo-analysis")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    service_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    input: Mapped["JobInput | None"] = relationship(
        back_populates="job",
        uselist=False,
        cascade="all, delete-orphan",
    )
    result: Mapped["JobResult | None"] = relationship(
        back_populates="job",
        uselist=False,
        cascade="all, delete-orphan",
    )
    artifacts: Mapped[list["Artifact"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="desc(Artifact.created_at)",
    )
    logs: Mapped[list["JobLog"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="JobLog.created_at",
    )


class JobInput(Base):
    __tablename__ = "job_inputs"

    job_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False)

    job: Mapped[Job] = relationship(back_populates="input")


class JobResult(Base):
    __tablename__ = "job_results"

    job_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    gherkin_text: Mapped[str] = mapped_column(Text, nullable=False)
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    job: Mapped[Job] = relationship(back_populates="result")


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    artifact_type: Mapped[str] = mapped_column(String(100), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    job: Mapped[Job] = relationship(back_populates="artifacts")


class JobLog(Base):
    __tablename__ = "job_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    level: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    job: Mapped[Job] = relationship(back_populates="logs")


def build_cors_origins() -> list[str]:
    raw_value = os.getenv("CORS_ALLOWED_ORIGINS", "")
    if not raw_value.strip():
        return []
    return [origin.strip() for origin in raw_value.split(",") if origin.strip()]


def build_database_url() -> str:
    explicit_url = os.getenv("DATABASE_URL", "").strip()
    if explicit_url:
        return explicit_url

    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "8080")
    database = os.getenv("POSTGRES_DB", "test_platform")
    username = os.getenv("POSTGRES_USER", "postgres")
    password = os.getenv("POSTGRES_PASSWORD", "postgres")
    return f"postgresql+psycopg2://{username}:{password}@{host}:{port}/{database}"


MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY") or os.getenv("MISTRAL_AI_API_KEY")
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "10"))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
DATABASE_URL = build_database_url()
SERVICE_TYPE = "image_analysis"
JOB_STATUS_PENDING = "pending"
JOB_STATUS_PROCESSING = "processing"
JOB_STATUS_DONE = "done"
JOB_STATUS_FAILED = "failed"
ALLOWED_MIME_PREFIX = "image/"
STORAGE_ROOT = Path(os.getenv("STORAGE_ROOT", "storage"))
IMAGE_STORAGE_DIR = STORAGE_ROOT / "images"

if not MISTRAL_API_KEY:
    raise RuntimeError("MISTRAL_API_KEY or MISTRAL_AI_API_KEY not found in environment")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_SIZE_BYTES

cors_origins = build_cors_origins()
CORS(
    app,
    resources={r"/*": {"origins": cors_origins or []}},
    supports_credentials=True,
)

engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)

HEADERS = {
    "Authorization": f"Bearer {MISTRAL_API_KEY}",
    "Content-Type": "application/json",
}
MISTRAL_LOCK = threading.Lock()


@contextmanager
def session_scope():
    session: Session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_storage() -> None:
    IMAGE_STORAGE_DIR.mkdir(parents=True, exist_ok=True)


def init_db() -> None:
    Base.metadata.create_all(engine)


def error_response(status_code: int, code: str, message: str, details: dict | None = None):
    payload = {
        "success": False,
        "error": {
            "code": code,
            "message": message,
        },
    }
    if details:
        payload["error"]["details"] = details
    return jsonify(payload), status_code


class JobRepository:
    def create_job(
        self,
        *,
        job_id: str,
        filename: str,
        content_type: str,
        file_size: int,
        artifact_path: Path,
        original_filename: str,
    ) -> str:
        now = utc_now()
        job = Job(
            id=job_id,
            service_type=SERVICE_TYPE,
            status=JOB_STATUS_PENDING,
            title=filename,
            created_at=now,
            updated_at=now,
        )
        job.input = JobInput(
            job_id=job_id,
            payload_json={
                "original_filename": original_filename,
                "stored_filename": filename,
                "content_type": content_type,
                "size_bytes": file_size,
            },
        )
        job.artifacts.append(
            Artifact(
                id=str(uuid.uuid4()),
                artifact_type="uploaded_image",
                file_name=filename,
                file_path=str(artifact_path.resolve()),
                mime_type=content_type,
                size_bytes=file_size,
                created_at=now,
            )
        )
        job.logs.append(
            JobLog(
                id=str(uuid.uuid4()),
                level="INFO",
                message="file uploaded",
                created_at=now,
            )
        )

        with session_scope() as session:
            session.add(job)

        return job_id

    def append_log(self, job_id: str, level: str, message: str) -> None:
        with session_scope() as session:
            session.add(
                JobLog(
                    id=str(uuid.uuid4()),
                    job_id=job_id,
                    level=level.upper(),
                    message=message,
                    created_at=utc_now(),
                )
            )

    def mark_processing(self, job_id: str) -> None:
        with session_scope() as session:
            job = session.get(Job, job_id)
            if not job:
                return
            now = utc_now()
            job.status = JOB_STATUS_PROCESSING
            job.started_at = now
            job.updated_at = now

    def mark_done(self, job_id: str, gherkin_text: str, result_json: dict | None = None) -> None:
        with session_scope() as session:
            job = session.get(Job, job_id)
            if not job:
                return

            now = utc_now()
            job.status = JOB_STATUS_DONE
            job.finished_at = now
            job.updated_at = now
            job.error_message = None

            if job.result is None:
                job.result = JobResult(
                    job_id=job_id,
                    gherkin_text=gherkin_text,
                    result_json=result_json,
                )
            else:
                job.result.gherkin_text = gherkin_text
                job.result.result_json = result_json

    def mark_failed(self, job_id: str, error_message: str) -> None:
        with session_scope() as session:
            job = session.get(Job, job_id)
            if not job:
                return

            now = utc_now()
            job.status = JOB_STATUS_FAILED
            job.finished_at = now
            job.updated_at = now
            job.error_message = error_message

    def get_job(self, job_id: str) -> Job | None:
        with session_scope() as session:
            return (
                session.query(Job)
                .options(selectinload(Job.input), selectinload(Job.result))
                .filter(Job.id == job_id)
                .one_or_none()
            )

    def get_job_artifacts(self, job_id: str) -> list[Artifact]:
        with session_scope() as session:
            job = (
                session.query(Job)
                .options(selectinload(Job.artifacts))
                .filter(Job.id == job_id)
                .one_or_none()
            )
            if not job:
                return []
            return list(job.artifacts)

    def list_jobs(
        self,
        *,
        service_type: str | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> list[Job]:
        with session_scope() as session:
            query = session.query(Job).options(selectinload(Job.input)).order_by(desc(Job.created_at))
            if service_type:
                query = query.filter(Job.service_type == service_type)
            if status:
                query = query.filter(Job.status == status)
            return list(query.offset(offset).limit(limit).all())

    def get_latest_successful_result(self) -> JobResult | None:
        with session_scope() as session:
            return (
                session.query(JobResult)
                .join(Job, JobResult.job_id == Job.id)
                .filter(Job.service_type == SERVICE_TYPE, Job.status == JOB_STATUS_DONE)
                .order_by(desc(Job.finished_at), desc(Job.created_at))
                .first()
            )


class ImageTestGenerator:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.cooldown_until = 0.0
        self.cooldown_lock = threading.Lock()

    def preprocess_image(self, image_bytes: bytes) -> tuple[bytes, str]:
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return image_bytes, "application/octet-stream"

        height, width = img.shape[:2]
        max_width = 1024
        if width > max_width:
            scale = max_width / float(width)
            img = cv2.resize(
                img,
                (int(width * scale), int(height * scale)),
                interpolation=cv2.INTER_AREA,
            )

        ok, buffer = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            return image_bytes, "application/octet-stream"

        return buffer.tobytes(), "image/jpeg"

    def _apply_cooldown_if_needed(self):
        with self.cooldown_lock:
            now = time.time()
            if now < self.cooldown_until:
                time.sleep(self.cooldown_until - now)

    def _set_cooldown(self, seconds: float):
        with self.cooldown_lock:
            self.cooldown_until = max(self.cooldown_until, time.time() + seconds)

    def call_mistral_with_retry(self, payload: dict, timeout: int = 60, max_attempts: int = 8):
        last_status = None
        last_text = None
        last_headers = None

        for attempt in range(1, max_attempts + 1):
            try:
                self._apply_cooldown_if_needed()

                with MISTRAL_LOCK:
                    response = self.session.post(MISTRAL_API_URL, json=payload, timeout=timeout)

                last_status = response.status_code
                last_text = response.text
                last_headers = dict(response.headers)

                if response.status_code == 429:
                    self._set_cooldown(random.uniform(5.0, 10.0))
                    retry_after = response.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        sleep_seconds = int(retry_after)
                    else:
                        sleep_seconds = min(2 ** (attempt - 1), 30) + random.uniform(0.5, 1.5)
                    time.sleep(sleep_seconds)
                    continue

                if 500 <= response.status_code < 600:
                    time.sleep(min(2 ** (attempt - 1), 30) + random.uniform(0.5, 1.5))
                    continue

                response.raise_for_status()
                return response.json(), None

            except requests.RequestException as exc:
                last_status = last_status or "network_error"
                last_text = str(exc)
                time.sleep(min(2 ** (attempt - 1), 30) + random.uniform(0.5, 1.5))

        return None, {"status": last_status, "body": last_text, "headers": last_headers}

    def generate_gherkin(self, image_bytes: bytes, content_type: str):
        compressed_bytes, forced_type = self.preprocess_image(image_bytes)
        use_type = forced_type if forced_type.startswith("image/") else content_type
        base64_image = base64.b64encode(compressed_bytes).decode("utf-8")

        messages = [
            {
                "role": "system",
                "content": (
                    "Ты QA-инженер. Генерируешь тест-кейсы ТОЛЬКО по фактам, видимым на одном изображении UI.\n"
                    "ЖЁСТКИЕ ПРАВИЛА:\n"
                    "1) Запрещены любые предположения о поведении и действиях пользователя. НЕ используй слова/идеи: click, tap, hover, scroll, open, opens, navigate, redirect, play, playback, search, login, modal.\n"
                    "2) Разрешены только проверки snapshot текущего экрана: видимость/наличие элементов, читаемые тексты, названия секций, структура областей (левая панель/центральная область/верхняя часть/нижняя панель/правая часть), очевидные состояния (выделение/активный пункт), если это видно.\n"
                    "3) Если элемент не читается или ты не уверен - НЕ упоминай его.\n"
                    "4) Все числа/цены/проценты/счётчики/время/имена/прочие динамические значения заменяй на <value>.\n"
                    "5) НЕ используй точные координаты и сравнительные формулировки: top/right/left/bottom corner, 'справа от', 'слева от', 'ниже/выше', 'слева направо', 'в вертикальном порядке'.\n"
                    "6) НЕ интерпретируй иконки без текста и не перечисляй 'additional icons/controls'. Если у элемента нет читаемого текста - обычно не упоминай его.\n"
                    "7) Для карточек/плиток используй нейтральные формулировки: 'отображается минимум одна карточка' и 'карточка содержит текст <value>'. Не утверждай 'артист/страна/трек', если это не написано явно читаемым текстом.\n"
                    "8) Ответ строго в Gherkin. Без markdown. Без таблиц. Без комментариев.\n"
                    "9) Каждый Scenario должен содержать только шаги Then/And (без Given/When).\n"
                ),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Сгенерируй 8-15 Scenario для проверки текущего экрана по этому изображению UI.\n"
                            "Только проверяемые по скриншоту утверждения (snapshot validation).\n"
                            "Покрой: левую панель (если видна), центральную область, верхнюю часть (если видна), нижнюю панель (если видна), заголовки/лейблы, основные секции и минимум одну карточку/плитку контента (если видны).\n"
                            "Сценарии должны быть устойчивыми к адаптивной верстке: избегай проверок точного порядка и точных координат.\n"
                            "Не используй действия пользователя и не описывай то, чего не видно на изображении."
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": f"data:{use_type};base64,{base64_image}",
                    },
                ],
            },
        ]

        payload = {
            "model": "ministral-14b-2512",
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 1200,
        }

        data, err = self.call_mistral_with_retry(payload, timeout=60, max_attempts=8)
        if err:
            return None, err

        content = data["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.replace("```gherkin", "").replace("```", "").strip()

        return content, None


repository = JobRepository()
generator = ImageTestGenerator()
init_storage()
init_db()


def validate_image_upload():
    if "image" not in request.files:
        return None, error_response(400, "MISSING_IMAGE", "File field must be named 'image'")

    file_storage = request.files["image"]
    filename = secure_filename(file_storage.filename or "")
    if not filename:
        return None, error_response(400, "INVALID_FILENAME", "Uploaded file must have a filename")

    content_type = (file_storage.content_type or "").lower()
    if not content_type.startswith(ALLOWED_MIME_PREFIX):
        return None, error_response(400, "UNSUPPORTED_MEDIA_TYPE", "Uploaded file must be an image")

    image_bytes = file_storage.read()
    if not image_bytes:
        return None, error_response(400, "EMPTY_FILE", "Uploaded file is empty")

    return {
        "file_storage": file_storage,
        "filename": filename,
        "content_type": content_type,
        "image_bytes": image_bytes,
        "file_size": len(image_bytes),
    }, None


def store_uploaded_image(job_id: str, filename: str, image_bytes: bytes) -> Path:
    stored_name = f"{job_id}_{filename}"
    target_path = IMAGE_STORAGE_DIR / stored_name
    target_path.write_bytes(image_bytes)
    return target_path


def serialize_job(job: Job) -> dict:
    payload = {
        "jobId": job.id,
        "serviceType": job.service_type,
        "status": job.status,
        "title": job.title,
        "createdAt": job.created_at.isoformat(),
        "startedAt": job.started_at.isoformat() if job.started_at else None,
        "finishedAt": job.finished_at.isoformat() if job.finished_at else None,
        "updatedAt": job.updated_at.isoformat(),
        "errorMessage": job.error_message,
    }
    if job.input:
        payload["input"] = job.input.payload_json
    return payload


def serialize_artifact(artifact: Artifact) -> dict:
    return {
        "artifactId": artifact.id,
        "jobId": artifact.job_id,
        "artifactType": artifact.artifact_type,
        "fileName": artifact.file_name,
        "filePath": artifact.file_path,
        "mimeType": artifact.mime_type,
        "sizeBytes": artifact.size_bytes,
        "createdAt": artifact.created_at.isoformat(),
    }


def parse_pagination_arg(name: str, default: int) -> tuple[int | None, tuple | None]:
    raw_value = request.args.get(name)
    if raw_value is None:
        return default, None
    try:
        value = int(raw_value)
    except ValueError:
        return None, error_response(400, "INVALID_QUERY", f"Query parameter '{name}' must be an integer")
    if value < 0:
        return None, error_response(400, "INVALID_QUERY", f"Query parameter '{name}' must be non-negative")
    return value, None


@app.errorhandler(RequestEntityTooLarge)
def handle_file_too_large(_error):
    return error_response(
        413,
        "FILE_TOO_LARGE",
        f"Uploaded file exceeds the {MAX_FILE_SIZE_MB} MB limit",
    )


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "UP", "service": "photo-analysis"})


@app.route("/upload-image", methods=["POST"])
def upload_image():
    validated, validation_error = validate_image_upload()
    if validation_error:
        return validation_error

    filename = validated["filename"]
    content_type = validated["content_type"]
    image_bytes = validated["image_bytes"]
    file_size = validated["file_size"]

    job_id = str(uuid.uuid4())
    try:
        artifact_path = store_uploaded_image(job_id, filename, image_bytes)
        repository.create_job(
            job_id=job_id,
            filename=filename,
            content_type=content_type,
            file_size=file_size,
            artifact_path=artifact_path,
            original_filename=validated["file_storage"].filename or filename,
        )
    except Exception:
        logger.exception("Failed to persist uploaded image job_id=%s", job_id)
        artifact_candidate = IMAGE_STORAGE_DIR / f"{job_id}_{filename}"
        if artifact_candidate.exists():
            artifact_candidate.unlink(missing_ok=True)
        return error_response(500, "INTERNAL_ERROR", "Failed to persist uploaded image")

    logger.info(
        "Received image upload job_id=%s filename=%s content_type=%s file_size=%s",
        job_id,
        filename,
        content_type,
        file_size,
    )

    repository.mark_processing(job_id)
    repository.append_log(job_id, "INFO", "ai request started")

    started_at = time.perf_counter()

    try:
        gherkin, err = generator.generate_gherkin(image_bytes=image_bytes, content_type=content_type)
        duration_ms = round((time.perf_counter() - started_at) * 1000, 2)

        if err:
            model_status = err.get("status")
            repository.append_log(job_id, "ERROR", "ai request failed")
            repository.mark_failed(job_id, f"Mistral request failed with status {model_status}")
            logger.warning(
                "Mistral request failed job_id=%s status=%s duration_ms=%s",
                job_id,
                model_status,
                duration_ms,
            )

            if model_status == 429:
                return error_response(429, "RATE_LIMIT", "Mistral rate limit exceeded")

            if isinstance(model_status, int) and 500 <= model_status < 600:
                return error_response(503, "MODEL_UNAVAILABLE", "Mistral is temporarily unavailable")

            return error_response(500, "GHERKIN_GENERATION_FAILED", "Failed to generate gherkin")

        repository.mark_done(job_id, gherkin, {"content_type": "text/x-gherkin"})
        repository.append_log(job_id, "INFO", "ai request finished")
        logger.info(
            "Mistral request completed job_id=%s status=%s duration_ms=%s",
            job_id,
            200,
            duration_ms,
        )
        return jsonify({"status": "success", "gherkin": gherkin})

    except Exception:
        repository.append_log(job_id, "ERROR", "ai request failed")
        repository.mark_failed(job_id, "Unexpected server error")
        logger.exception("Unexpected error while processing job_id=%s", job_id)
        return error_response(500, "INTERNAL_ERROR", "Unexpected server error")


@app.route("/jobs/<job_id>", methods=["GET"])
def get_job(job_id: str):
    job = repository.get_job(job_id)
    if not job:
        return error_response(404, "JOB_NOT_FOUND", "Job not found")
    return jsonify(serialize_job(job))


@app.route("/jobs/<job_id>/result", methods=["GET"])
def get_job_result(job_id: str):
    job = repository.get_job(job_id)
    if not job:
        return error_response(404, "JOB_NOT_FOUND", "Job not found")
    if job.status != JOB_STATUS_DONE or not job.result:
        return error_response(409, "JOB_NOT_READY", "Job result is not available yet")
    return jsonify(
        {
            "jobId": job.id,
            "status": "success",
            "gherkin": job.result.gherkin_text,
            "result": job.result.result_json,
        }
    )


@app.route("/jobs/<job_id>/artifacts", methods=["GET"])
def get_job_artifacts(job_id: str):
    job = repository.get_job(job_id)
    if not job:
        return error_response(404, "JOB_NOT_FOUND", "Job not found")
    artifacts = repository.get_job_artifacts(job_id)
    return jsonify({"jobId": job_id, "artifacts": [serialize_artifact(item) for item in artifacts]})


@app.route("/jobs", methods=["GET"])
def list_jobs():
    limit, limit_error = parse_pagination_arg("limit", 20)
    if limit_error:
        return limit_error

    offset, offset_error = parse_pagination_arg("offset", 0)
    if offset_error:
        return offset_error

    service_type = request.args.get("service_type")
    status = request.args.get("status")
    jobs = repository.list_jobs(
        service_type=service_type,
        status=status,
        limit=limit,
        offset=offset,
    )
    return jsonify(
        {
            "items": [serialize_job(job) for job in jobs],
            "limit": limit,
            "offset": offset,
        }
    )


@app.route("/get-test-cases", methods=["GET"])
def get_test_cases():
    latest_result = repository.get_latest_successful_result()
    if not latest_result:
        return jsonify({"status": "error", "message": "Test cases have not been generated yet"}), 400
    return jsonify({"status": "success", "gherkin": latest_result.gherkin_text})


@app.route("/jobs/<job_id>/feature", methods=["GET"])
def download_feature(job_id: str):
    job = repository.get_job(job_id)
    if not job:
        return error_response(404, "JOB_NOT_FOUND", "Job not found")
    if job.status != JOB_STATUS_DONE or not job.result:
        return error_response(409, "JOB_NOT_READY", "Job result is not available yet")

    feature_name = Path(job.title or "generated").stem or "generated"
    download_name = secure_filename(feature_name) or "generated"
    if not download_name.endswith(".feature"):
        download_name = f"{download_name}.feature"

    return Response(
        job.result.gherkin_text,
        mimetype="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8001"))
    logger.info(
        "Starting photo-analysis service port=%s db=%s cors_origins=%s max_file_size_mb=%s",
        port,
        DATABASE_URL,
        cors_origins if cors_origins else "[]",
        MAX_FILE_SIZE_MB,
    )
    app.run(host="0.0.0.0", port=port)
