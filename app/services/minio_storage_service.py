from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from typing import Any, BinaryIO
from urllib.parse import urlparse
from uuid import uuid4

from app.core.config import MinioConfig


class ObjectStorageError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class StoredObject:
    bucket_name: str
    object_key: str
    size_bytes: int
    etag: str | None


@dataclass(frozen=True)
class MinioDownloadMetadata:
    request_id: str
    tenant_id: str
    download_id: str
    bucket_name: str
    object_key: str
    file_name: str
    content_type: str
    expires_at: datetime | None = None


_DOWNLOADS_BY_ID: dict[str, MinioDownloadMetadata] = {}
_DOWNLOADS_BY_REQUEST: dict[str, MinioDownloadMetadata] = {}


def register_minio_download(metadata: MinioDownloadMetadata, *, configured_bucket: str) -> None:
    if metadata.bucket_name != configured_bucket:
        raise ObjectStorageError("Agent bucket does not match configured bucket.", code="OBJECT_STORAGE_FORBIDDEN")
    _DOWNLOADS_BY_ID[metadata.download_id] = metadata
    _DOWNLOADS_BY_REQUEST[metadata.request_id] = metadata


def find_minio_download(*, download_id: str | None = None, request_id: str | None = None) -> MinioDownloadMetadata | None:
    if download_id:
        return _DOWNLOADS_BY_ID.get(download_id)
    if request_id:
        return _DOWNLOADS_BY_REQUEST.get(request_id)
    return None


def build_export_object_key(*, environment: str, tenant_id: str, request_id: str, now: datetime) -> str:
    safe_environment = "".join(ch for ch in environment if ch.isalnum() or ch in "-_") or "unknown"
    safe_tenant = "".join(ch for ch in tenant_id if ch.isalnum() or ch in "-_") or "default"
    safe_request = "".join(ch for ch in request_id if ch.isalnum() or ch in "-_")
    if not safe_request:
        raise ValueError("request_id must contain a safe identifier")
    download_id = f"DL-{uuid4().hex}"
    return f"{safe_environment}/{safe_tenant}/agent-results/{now:%Y/%m}/{safe_request}/{download_id}.xlsx"


class MinioStorageService:
    def __init__(self, config: MinioConfig, *, client: Any | None = None) -> None:
        self.config = config
        self._client = client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.config.enabled:
            raise ObjectStorageError("Object storage is disabled.", code="MINIO_CONNECTION_FAILED")
        if not self.config.access_key or not self.config.secret_key:
            raise ObjectStorageError("Object storage credentials are not configured.", code="MINIO_CONNECTION_FAILED")
        try:
            from minio import Minio
        except ImportError as exc:
            raise ObjectStorageError("MinIO client is not installed.", code="MINIO_CONNECTION_FAILED") from exc
        parsed = urlparse(self.config.endpoint)
        endpoint = parsed.netloc or parsed.path
        self._client = Minio(
            endpoint,
            access_key=self.config.access_key,
            secret_key=self.config.secret_key,
            secure=self.config.secure if not parsed.scheme else parsed.scheme == "https",
            region=self.config.region or None,
        )
        return self._client

    def upload_bytes(self, *, object_key: str, content: bytes, content_type: str) -> StoredObject:
        try:
            result = self._get_client().put_object(
                self.config.bucket_exports,
                object_key,
                BytesIO(content),
                length=len(content),
                content_type=content_type,
            )
        except ObjectStorageError:
            raise
        except Exception as exc:  # SDK exceptions vary by version and transport.
            raise ObjectStorageError("Object upload failed.", code="OBJECT_UPLOAD_FAILED") from exc
        return StoredObject(
            bucket_name=self.config.bucket_exports,
            object_key=object_key,
            size_bytes=len(content),
            etag=getattr(result, "etag", None),
        )

    def get_object(self, *, object_key: str, bucket_name: str | None = None) -> BinaryIO:
        try:
            bucket = bucket_name or self.config.bucket_exports
            if bucket != self.config.bucket_exports:
                raise ObjectStorageError("Object bucket is not allowed.", code="OBJECT_STORAGE_FORBIDDEN")
            return self._get_client().get_object(bucket, object_key)
        except ObjectStorageError:
            raise
        except Exception as exc:
            raise ObjectStorageError("Object download failed.", code="DOWNLOAD_FAILED") from exc

    def find_artifact_object_key(self, artifact_id: str) -> str:
        """Resolve an Agent artifact ID without exposing MinIO object keys to clients."""
        safe_id = artifact_id.strip()
        if not safe_id or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in safe_id):
            raise ObjectStorageError("Artifact identifier is invalid.", code="ARTIFACT_NOT_FOUND")
        try:
            matches = [
                item.object_name
                for item in self._get_client().list_objects(
                    self.config.bucket_exports,
                    recursive=True,
                )
                if safe_id in str(item.object_name).split("/")
            ]
        except ObjectStorageError:
            raise
        except Exception as exc:
            raise ObjectStorageError(
                "Object storage lookup failed.",
                code="MINIO_CONNECTION_FAILED",
            ) from exc
        if len(matches) != 1:
            raise ObjectStorageError("Artifact object was not found.", code="ARTIFACT_NOT_FOUND")
        return matches[0]

    def health(self) -> str:
        if not self.config.enabled:
            return "DISABLED"
        try:
            self._get_client().bucket_exists(self.config.bucket_exports)
            return "UP"
        except Exception:
            return "DOWN"


def get_minio_storage_service() -> MinioStorageService:
    from app.core.config import get_settings

    return MinioStorageService(get_settings().minio)
