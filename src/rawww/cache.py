from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QStandardPaths
from PySide6.QtGui import QImage

from .imaging import DecodedImage, PixelImage, decode_pixels, pixel_to_decoded


CACHE_APP_DIRECTORY = "RAWww"
CACHE_DIRECTORY = "folder-caches"
DISK_JPEG_QUALITY = 88
SQLITE_PAGE_SIZE = 32 * 1024
SQLITE_CACHE_KIB = 128 * 1024
SQLITE_MMAP_SIZE = 512 * 1024 * 1024
SCHEMA = """
CREATE TABLE IF NOT EXISTS previews (
    name TEXT NOT NULL,
    variant INTEGER NOT NULL,
    file_size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    format TEXT NOT NULL,
    pixels BLOB NOT NULL,
    accessed_ns INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (name, variant)
);
CREATE TABLE IF NOT EXISTS image_embeddings (
    name TEXT PRIMARY KEY, file_size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
    embedding BLOB NOT NULL, processed_ns INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS face_analysis (
    name TEXT PRIMARY KEY, file_size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
    faces_json TEXT NOT NULL, processed_ns INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS photo_metadata (
    name TEXT PRIMARY KEY, file_size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
    metadata_json TEXT NOT NULL, processed_ns INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS photo_selection (
    name TEXT PRIMARY KEY,
    rating INTEGER,
    color_label TEXT NOT NULL DEFAULT '',
    comment TEXT NOT NULL DEFAULT '',
    updated_ns INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class FileStamp:
    size: int
    mtime_ns: int


class FolderCache:
    """Disk-backed preview cache for one image folder.

    The database is opened lazily so the application can still initialize it
    off the UI thread.  Its location is centralised rather than kept beside
    the images.
    """

    def __init__(
        self,
        folder: Path,
        live_names: set[str],
        eager_variants: set[int] | None = None,
        *,
        load_from_disk: bool = True,
        cache_root: Path | None = None,
    ) -> None:
        self.folder = folder
        self.live_names = live_names
        self.eager_variants = eager_variants or set()
        self.path = cache_path(folder, cache_root)
        self._lock = threading.RLock()
        self._db: sqlite3.Connection | None = None
        if load_from_disk:
            self.load_from_disk()

    def close(self, *, flush: bool) -> None:
        if flush:
            self.flush()
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def load_or_decode(self, path: Path, max_size: int) -> DecodedImage:
        cached = self.load(path, max_size)
        if cached is not None:
            return cached
        pixel = decode_pixels(path, max_size)
        self.store_pixels(pixel, max_size)
        return pixel_to_decoded(pixel)

    def load(self, path: Path, max_size: int) -> DecodedImage | None:
        stamp = _stamp(path)
        with self._lock:
            row = self._db_or_raise().execute(
                """
                SELECT width, height, format, pixels
                FROM previews
                WHERE name = ? AND variant = ? AND file_size = ? AND mtime_ns = ?
                """,
                (path.name, max_size, stamp.size, stamp.mtime_ns),
            ).fetchone()
        if row is None:
            return None

        width, height, fmt, data = row
        if fmt == "jpeg":
            data = _decode_jpeg_to_rgba(data)
            fmt = "rgba"
        if fmt != "rgba":
            return None
        image = QImage(data, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()
        return DecodedImage(path=path, image=image, width=width, height=height)

    def store(self, decoded: DecodedImage, max_size: int) -> None:
        self.store_pixels(
            PixelImage(
                path=decoded.path,
                pixels=_encode_rgba(decoded.image),
                width=decoded.width,
                height=decoded.height,
            ),
            max_size,
        )

    def store_pixels(self, pixel: PixelImage, max_size: int) -> None:
        stamp = _stamp(pixel.path)
        with self._lock:
            db = self._db_or_raise()
            db.execute(
                """
                INSERT OR REPLACE INTO previews
                    (name, variant, file_size, mtime_ns, width, height, format, pixels, accessed_ns)
                VALUES (?, ?, ?, ?, ?, ?, 'jpeg', ?, ?)
                """,
                (
                    pixel.path.name, max_size, stamp.size, stamp.mtime_ns,
                    pixel.width, pixel.height,
                    _encode_jpeg(pixel.pixels, pixel.width, pixel.height), time.time_ns(),
                ),
            )
            db.commit()


    def flush(self) -> None:
        """Commit pending work; writes are normally committed immediately."""
        with self._lock:
            if self._db is not None:
                self._db.commit()

    def missing_ai_paths(self, paths: list[Path], table: str) -> list[Path]:
        if table not in {"image_embeddings", "face_analysis", "photo_metadata"}:
            raise ValueError(f"Unknown AI cache table: {table}")
        missing = []
        with self._lock:
            db = self._db_or_raise()
            for path in paths:
                stamp = _stamp(path)
                row = db.execute(
                    f"SELECT 1 FROM {table} WHERE name=? AND file_size=? AND mtime_ns=?",
                    (path.name, stamp.size, stamp.mtime_ns),
                ).fetchone()
                if row is None:
                    missing.append(path)
        return missing

    def store_image_embeddings(self, results: list[tuple[str, bytes]]) -> None:
        self._store_ai_results("image_embeddings", "embedding", results)

    def store_face_analysis(self, results: list[tuple[str, str]]) -> None:
        self._store_ai_results("face_analysis", "faces_json", results)

    def store_photo_metadata(self, results: list[tuple[str, str]]) -> None:
        self._store_ai_results("photo_metadata", "metadata_json", results)

    def load_photo_details(self) -> dict[str, dict]:
        """Return cached EXIF/AI metadata and the user's local selection state."""
        import json

        details: dict[str, dict] = {}
        with self._lock:
            db = self._db_or_raise()
            for name, payload in db.execute("SELECT name, metadata_json FROM photo_metadata"):
                try:
                    details[name] = json.loads(payload)
                except (TypeError, ValueError):
                    details[name] = {}
            for name, payload in db.execute("SELECT name, faces_json FROM face_analysis"):
                try:
                    faces = json.loads(payload)
                except (TypeError, ValueError):
                    faces = []
                details.setdefault(name, {})["faces"] = faces if isinstance(faces, list) else []
            for name, rating, color, comment in db.execute(
                "SELECT name, rating, color_label, comment FROM photo_selection"
            ):
                details.setdefault(name, {}).update(
                    rating=rating, color_label=color or "", comment=comment or ""
                )
        return details

    def store_photo_selection(
        self, name: str, *, rating: int | None, color_label: str, comment: str
    ) -> None:
        with self._lock:
            db = self._db_or_raise()
            db.execute(
                """INSERT OR REPLACE INTO photo_selection
                   (name, rating, color_label, comment, updated_ns)
                   VALUES (?, ?, ?, ?, ?)""",
                (name, rating, color_label, comment, time.time_ns()),
            )
            db.commit()

    def _store_ai_results(self, table: str, value_column: str, results: list[tuple[str, object]]) -> None:
        with self._lock:
            db = self._db_or_raise()
            rows = []
            for path_value, value in results:
                path = Path(path_value)
                try:
                    stamp = _stamp(path)
                except OSError:
                    continue
                rows.append((path.name, stamp.size, stamp.mtime_ns, value, time.time_ns()))
            if rows:
                db.executemany(
                    f"INSERT OR REPLACE INTO {table} "
                    f"(name, file_size, mtime_ns, {value_column}, processed_ns) VALUES (?, ?, ?, ?, ?)",
                    rows,
                )
                db.commit()

    def load_from_disk(self) -> None:
        """Open the central on-disk database and discard entries for deleted files."""
        with self._lock:
            if self._db is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                db = self._open_database()
            except sqlite3.DatabaseError:
                # Everything in this database is derived from source photos.
                # A torn/corrupt cache after an unclean shutdown is cheaper and
                # safer to rebuild than to attempt to salvage.
                for suffix in ("", "-wal", "-shm"):
                    (Path(f"{self.path}{suffix}")).unlink(missing_ok=True)
                db = self._open_database()
            self._db = db

    def _open_database(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        try:
            _configure_database(db)
            db.executescript(SCHEMA)
            self._remove_deleted_entries(db)
            db.commit()
            return db
        except Exception:
            db.close()
            raise

    def _remove_deleted_entries(self, db: sqlite3.Connection) -> None:
        db.execute("CREATE TEMP TABLE live_names (name TEXT PRIMARY KEY)")
        try:
            db.executemany("INSERT INTO live_names VALUES (?)", ((name,) for name in self.live_names))
            db.execute("DELETE FROM previews WHERE NOT EXISTS (SELECT 1 FROM live_names WHERE live_names.name = previews.name)")
            db.execute("DELETE FROM image_embeddings WHERE NOT EXISTS (SELECT 1 FROM live_names WHERE live_names.name = image_embeddings.name)")
            db.execute("DELETE FROM face_analysis WHERE NOT EXISTS (SELECT 1 FROM live_names WHERE live_names.name = face_analysis.name)")
            db.execute("DELETE FROM photo_metadata WHERE NOT EXISTS (SELECT 1 FROM live_names WHERE live_names.name = photo_metadata.name)")
            db.execute("DELETE FROM photo_selection WHERE NOT EXISTS (SELECT 1 FROM live_names WHERE live_names.name = photo_selection.name)")
        finally:
            db.execute("DROP TABLE live_names")

    def _db_or_raise(self) -> sqlite3.Connection:
        if self._db is None:
            raise RuntimeError("Folder cache has not been opened")
        return self._db


def cache_root() -> Path:
    # GenericDataLocation deliberately does not include Qt's application name.
    # That name changes between `uv run`, an installed console script, and a
    # packaged executable, whereas the cache location must stay stable.
    location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation)
    if location:
        return Path(location) / CACHE_APP_DIRECTORY / "cache" / CACHE_DIRECTORY
    return Path.home() / ".cache" / CACHE_APP_DIRECTORY / CACHE_DIRECTORY


def cache_path(folder: Path, root: Path | None = None) -> Path:
    try:
        identity = str(folder.resolve(strict=False))
    except OSError:
        identity = str(folder.absolute())
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return (root or cache_root()) / f"{digest}.sqlite"


def _configure_database(db: sqlite3.Connection) -> None:
    """Tune a disposable, write-heavy thumbnail/AI cache for throughput."""
    db.execute(f"PRAGMA page_size={SQLITE_PAGE_SIZE}")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=OFF")
    db.execute("PRAGMA temp_store=MEMORY")
    db.execute(f"PRAGMA cache_size=-{SQLITE_CACHE_KIB}")
    db.execute(f"PRAGMA mmap_size={SQLITE_MMAP_SIZE}")
    db.execute("PRAGMA busy_timeout=60000")
    db.execute("PRAGMA wal_autocheckpoint=4096")
    db.execute("PRAGMA journal_size_limit=268435456")


def _stamp(path: Path) -> FileStamp:
    stat = path.stat()
    return FileStamp(size=stat.st_size, mtime_ns=stat.st_mtime_ns)


def _encode_rgba(image: QImage) -> bytes:
    rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
    return bytes(rgba.bits())


def _encode_jpeg(pixels: bytes, width: int, height: int) -> bytes:
    image = QImage(pixels, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "JPG", DISK_JPEG_QUALITY)
    buffer.close()
    return bytes(data)


def _decode_jpeg_to_rgba(data: bytes) -> bytes:
    image = QImage()
    if not image.loadFromData(data, "JPG"):
        raise RuntimeError("Failed to decode cached JPEG preview")
    rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
    return bytes(rgba.bits())
