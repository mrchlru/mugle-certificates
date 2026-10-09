"""Хранение обложек: локальный диск или S3-совместимое хранилище."""

from pathlib import Path

from bonus.config import Settings, get_settings
from bonus.services import BonusError

_CONTENT_TYPE = "image/webp"
_CACHE_CONTROL = "public, max-age=31536000, immutable"


def s3_enabled(settings: Settings | None = None) -> bool:
    """Проверяет, что заданы бакет, ключи и публичный URL."""
    cfg = settings or get_settings()
    return bool(
        cfg.s3_bucket.strip()
        and cfg.s3_access_key.strip()
        and cfg.s3_secret_key.strip()
        and cfg.s3_public_base_url.strip()
    )


def store_template_bytes(payload: bytes, filename: str) -> str:
    """Сохраняет WebP и возвращает ключ/путь для записи в базу."""
    key = _object_key(filename)
    settings = get_settings()
    if s3_enabled(settings):
        _put_s3(settings, key, payload)
        return key
    path = _local_file(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return key


def public_media_url(image_path: str) -> str:
    """Публичный адрес картинки: CDN/S3 или локальный /media."""
    cleaned = image_path.strip()
    if cleaned.startswith("http://") or cleaned.startswith("https://"):
        return cleaned
    settings = get_settings()
    if s3_enabled(settings):
        base = settings.s3_public_base_url.rstrip("/")
        return f"{base}/{cleaned.lstrip('/')}"
    return f"/media/{cleaned.lstrip('/')}"


def read_template_bytes(image_path: str) -> bytes | None:
    """Читает локальный файл шаблона, если он ещё на диске."""
    if image_path.startswith("http://") or image_path.startswith("https://"):
        return None
    path = _local_file(image_path)
    if path.is_file():
        return path.read_bytes()
    fallback = get_settings().data_path() / "uploads" / Path(image_path).name
    if fallback.is_file():
        return fallback.read_bytes()
    return None


def delete_local_template(image_path: str) -> None:
    """Удаляет локальную копию после выгрузки в S3."""
    if image_path.startswith("http://") or image_path.startswith("https://"):
        return
    _safe_unlink(_local_file(image_path))
    _safe_unlink(get_settings().data_path() / "uploads" / Path(image_path).name)


def delete_template_object(image_path: str) -> None:
    """Удаляет обложку с диска и из S3, если хранилище настроено."""
    key = _storage_key(image_path)
    if not key:
        return
    delete_local_template(key)
    settings = get_settings()
    if s3_enabled(settings):
        _delete_s3(settings, key)


def _object_key(filename: str) -> str:
    settings = get_settings()
    prefix = settings.s3_prefix.strip().strip("/")
    name = Path(filename).name
    if prefix:
        return f"{prefix}/{name}"
    return name


def _local_file(image_path: str) -> Path:
    return get_settings().data_path() / "uploads" / image_path.lstrip("/").replace("\\", "/")


def _storage_key(image_path: str) -> str:
    cleaned = image_path.strip()
    if cleaned == "":
        return ""
    if cleaned.startswith("http://") or cleaned.startswith("https://"):
        settings = get_settings()
        base = settings.s3_public_base_url.rstrip("/")
        if base and cleaned.startswith(base + "/"):
            return cleaned[len(base) + 1 :]
        return cleaned.rsplit("/", 1)[-1]
    return cleaned.lstrip("/")


def _put_s3(settings: Settings, key: str, payload: bytes) -> None:
    try:
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as exc:
        raise BonusError("Для S3 нужен пакет boto3") from exc
    try:
        client = _s3_client(settings)
        client.put_object(
            Bucket=settings.s3_bucket,
            Key=key,
            Body=payload,
            ContentType=_CONTENT_TYPE,
            CacheControl=_CACHE_CONTROL,
        )
    except (BotoCoreError, ClientError, OSError) as exc:
        raise BonusError("Не удалось загрузить картинку в S3") from exc


def _delete_s3(settings: Settings, key: str) -> None:
    try:
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        return
    try:
        _s3_client(settings).delete_object(Bucket=settings.s3_bucket, Key=key)
    except (BotoCoreError, ClientError, OSError):
        return


def _s3_client(settings: Settings):
    try:
        import boto3
        from botocore.client import Config
    except ImportError as exc:
        raise BonusError("Для S3 нужен пакет boto3") from exc
    kwargs: dict[str, object] = {
        "service_name": "s3",
        "aws_access_key_id": settings.s3_access_key,
        "aws_secret_access_key": settings.s3_secret_key,
        "region_name": settings.s3_region or "ru-1",
        "config": Config(signature_version="s3v4"),
    }
    endpoint = settings.s3_endpoint_url.strip()
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return boto3.client(**kwargs)


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return
