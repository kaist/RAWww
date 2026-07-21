## Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
## SPDX-License-Identifier: GPL-3.0-or-later

"""Дисковый кэш папок с превью, метаданными и результатами анализа.

SQLite хранит состояние каждой папки отдельно: так повреждение одного кэша не
превращает всю фототеку в археологическую экспедицию.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import threading
import time
from collections.abc import Iterable
from uuid import uuid4
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QStandardPaths
from PySide6.QtGui import QImage

from .runtime_paths import PORTABLE, work_path

from .imaging import DecodedImage, PixelImage, decode_pixels, pixel_to_decoded


CACHE_APP_DIRECTORY = "ctrlka"
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
CREATE TABLE IF NOT EXISTS quality_analysis (
    name TEXT PRIMARY KEY, file_size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
    quality_json TEXT NOT NULL, processed_ns INTEGER NOT NULL
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
    keywords_json TEXT NOT NULL DEFAULT '[]',
    updated_ns INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS xmp_state (
    sidecar_name TEXT PRIMARY KEY,
    file_size INTEGER NOT NULL DEFAULT 0,
    mtime_ns INTEGER NOT NULL DEFAULT 0,
    digest TEXT,
    base_fields_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'synchronized',
    conflicts_json TEXT NOT NULL DEFAULT '[]',
    error TEXT NOT NULL DEFAULT '',
    updated_ns INTEGER NOT NULL
);
-- Lets background maintenance map a hashed cache filename back to its folder.
CREATE TABLE IF NOT EXISTS cache_info (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    folder_path TEXT NOT NULL
);
-- ShotSync "selection" session state (feature 2). A single row marks this
-- folder as a synced copy of a server shooting.
CREATE TABLE IF NOT EXISTS shotsync_session (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    shooting_id INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT ''
);
-- Maps a local file name to its server photo id so marks can be routed back.
CREATE TABLE IF NOT EXISTS shotsync_photos (
    name TEXT PRIMARY KEY,
    photo_id INTEGER NOT NULL,
    shooting_id INTEGER NOT NULL
);
-- Durable offline queue of marks awaiting delivery to the server. Coalesced
-- per (photo_id, kind) so the latest value wins; drained once the socket is up.
CREATE TABLE IF NOT EXISTS shotsync_pending (
    photo_id INTEGER NOT NULL,
    kind TEXT NOT NULL,                 -- 'rating' | 'meta'
    shooting_id INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    updated_ns INTEGER NOT NULL,
    PRIMARY KEY (photo_id, kind)
);
"""


@dataclass(frozen=True)
class FileStamp:
    """Короткий отпечаток файла для проверки актуальности записи кэша."""

    size: int
    mtime_ns: int


class FolderCache:
    """Дисковый кэш превью и метаданных для одной папки с изображениями.

    SQLite-база лежит в общем каталоге кэша, а не рядом с фотографиями. Открытие
    можно отложить и выполнить в фоне, чтобы большая папка не замораживала Qt.
    В одной базе хранятся варианты превью, EXIF, пользовательские метки,
    результаты AI и служебное соответствие ShotSync.
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
        image = image.convertToFormat(QImage.Format.Format_RGB888)
        return DecodedImage(path=path, image=image, width=width, height=height)

    def load_batch(self, paths: list[Path], max_size: int) -> dict[Path, DecodedImage]:
        """Читает несколько миниатюр одним запросом, оставляя устаревшие записи за бортом."""
        stamps: dict[Path, FileStamp] = {}
        for path in paths:
            try:
                stamps[path] = _stamp(path)
            except OSError:
                continue
        if not stamps:
            return {}
        placeholders = ", ".join("?" for _ in stamps)
        with self._lock:
            rows = self._db_or_raise().execute(
                f"""
                SELECT name, file_size, mtime_ns, width, height, format, pixels
                FROM previews
                WHERE variant = ? AND name IN ({placeholders})
                """,
                (max_size, *(path.name for path in stamps)),
            ).fetchall()
        rows_by_name = {str(row[0]): row[1:] for row in rows}
        decoded: dict[Path, DecodedImage] = {}
        for path, stamp in stamps.items():
            row = rows_by_name.get(path.name)
            if row is None:
                continue
            file_size, mtime_ns, width, height, fmt, data = row
            if file_size != stamp.size or mtime_ns != stamp.mtime_ns or fmt != "jpeg":
                continue
            try:
                rgba = _decode_jpeg_to_rgba(data)
                image = QImage(rgba, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()
                image = image.convertToFormat(QImage.Format.Format_RGB888)
            except Exception:
                continue
            decoded[path] = DecodedImage(path=path, image=image, width=width, height=height)
        return decoded

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
        self.store_pixels_batch([(pixel, max_size)])

    def store_pixels_batch(self, previews: list[tuple[PixelImage, int]]) -> None:
        """Кодирует несколько превью и фиксирует их одной транзакцией."""
        rows = []
        for pixel, max_size in previews:
            try:
                stamp = _stamp(pixel.path)
                encoded = _encode_jpeg(pixel.pixels, pixel.width, pixel.height)
            except Exception:
                continue
            rows.append(
                (
                    pixel.path.name, max_size, stamp.size, stamp.mtime_ns,
                    pixel.width, pixel.height, encoded, time.time_ns(),
                )
            )
        if not rows:
            return
        with self._lock:
            db = self._db_or_raise()
            db.executemany(
                """
                INSERT OR REPLACE INTO previews
                    (name, variant, file_size, mtime_ns, width, height, format, pixels, accessed_ns)
                VALUES (?, ?, ?, ?, ?, ?, 'jpeg', ?, ?)
                """,
                rows,
            )
            db.commit()


    def flush(self) -> None:
        """Фиксирует накопленные изменения; обычно записи сохраняются сразу."""
        with self._lock:
            if self._db is not None:
                self._db.commit()

    def prune_deleted_entries(self, *, vacuum: bool = False) -> None:
        """Удаляет записи файлов, которых больше нет в папке.

        Метод подходит и короткоживущему фоновому экземпляру: удалённые исходники
        освобождают свои превью и AI-данные, даже если папку больше не откроют в
        текущем сеансе.
        """
        with self._lock:
            db = self._db_or_raise()
            self._remove_deleted_entries(db)
            db.commit()
            if vacuum:
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                db.execute("VACUUM")

    def has_deleted_entries(self) -> bool:
        """Принадлежит ли какая-либо кэшированная запись файлу, которого больше нет на диске."""
        with self._lock:
            db = self._db_or_raise()
            db.execute("CREATE TEMP TABLE live_names (name TEXT PRIMARY KEY)")
            try:
                db.executemany("INSERT INTO live_names VALUES (?)", ((name,) for name in self.live_names))
                row = db.execute(
                    """
                    SELECT 1 FROM (
                        SELECT name FROM previews
                        UNION SELECT name FROM image_embeddings
                        UNION SELECT name FROM face_analysis
                        UNION SELECT name FROM quality_analysis
                        UNION SELECT name FROM photo_metadata
                        UNION SELECT name FROM photo_selection
                        UNION SELECT name FROM shotsync_photos
                    ) AS cached
                    WHERE NOT EXISTS (SELECT 1 FROM live_names WHERE live_names.name = cached.name)
                    LIMIT 1
                    """
                ).fetchone()
                return row is not None
            finally:
                db.execute("DROP TABLE live_names")

    def missing_ai_paths(self, paths: list[Path], table: str) -> list[Path]:
        if table not in {"image_embeddings", "face_analysis", "quality_analysis"}:
            raise ValueError(f"Unknown AI cache table: {table}")
        return self._missing_paths(paths, table)

    def missing_metadata_paths(self, paths: list[Path]) -> list[Path]:
        return self._missing_paths(paths, "photo_metadata")

    def _missing_paths(self, paths: list[Path], table: str) -> list[Path]:
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

    def load_image_embeddings(self) -> dict[str, bytes]:
        """Возвращает нормализованные векторы CLIP, используемые пользовательским интерфейсом серии просмотра.
        """
        with self._lock:
            db = self._db_or_raise()
            return {
                str(name): bytes(embedding)
                for name, embedding in db.execute("SELECT name, embedding FROM image_embeddings")
                if embedding
            }

    def store_face_analysis(self, results: list[tuple[str, str]]) -> None:
        self._store_ai_results("face_analysis", "faces_json", results)

    def store_quality_analysis(self, results: list[tuple[str, str]]) -> None:
        self._store_ai_results("quality_analysis", "quality_json", results)

    def load_quality_analysis(self) -> dict[str, str]:
        """Возвращает сохранённый JSON оценок NIMA отдельно для каждого файла."""
        with self._lock:
            db = self._db_or_raise()
            return {
                str(name): quality_json
                for name, quality_json in db.execute("SELECT name, quality_json FROM quality_analysis")
                if quality_json
            }

    def load_face_analysis(self) -> dict[str, str]:
        """Возвращает сохранённый AI-конвейером JSON отдельно для каждого файла."""
        with self._lock:
            db = self._db_or_raise()
            return {
                str(name): faces_json
                for name, faces_json in db.execute("SELECT name, faces_json FROM face_analysis")
                if faces_json
            }

    def store_photo_metadata(self, results: list[tuple[str, str]]) -> None:
        self._store_ai_results("photo_metadata", "metadata_json", results)

    def load_audio_details(self) -> dict[str, dict]:
        details = {}
        wavs = {}
        try:
            entries = self.folder.iterdir()
            for path in entries:
                try:
                    if path.is_file() and path.suffix.casefold() == ".wav":
                        wavs[path.stem.casefold()] = path
                except OSError:
                    continue
        except OSError:
            return details
        for name in self.live_names:
            audio = wavs.get(Path(name).stem.casefold())
            if audio is not None:
                details[name] = {"audio_comment_path": str(audio)}
        return details

    def load_photo_details(self, *, include_metadata: bool = True) -> dict[str, dict]:
        """Возвращает кэшированные метаданные EXIF/AI и состояние локального выбора пользователя."""
        details: dict[str, dict] = {}
        with self._lock:
            db = self._db_or_raise()
            if include_metadata:
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
            for name, payload in db.execute("SELECT name, quality_json FROM quality_analysis"):
                try:
                    quality = json.loads(payload)
                except (TypeError, ValueError):
                    quality = None
                if isinstance(quality, dict):
                    details.setdefault(name, {})["quality"] = quality
            for name, rating, color, comment, keywords_json, updated_ns in db.execute(
                "SELECT name, rating, color_label, comment, keywords_json, updated_ns FROM photo_selection"
            ):
                try:
                    keywords = json.loads(keywords_json)
                except (TypeError, ValueError):
                    keywords = []
                details.setdefault(name, {}).update(
                    rating=rating, color_label=color or "", comment=comment or "",
                    keywords=keywords if isinstance(keywords, list) else [],
                    _selection_updated_ns=int(updated_ns),
                )
        for name, audio in self.load_audio_details().items():
            details.setdefault(name, {}).update(audio)
        return details

    def store_photo_selection(
        self, name: str, *, rating: int | None, color_label: str, comment: str,
        keywords: Iterable[str] | None = None,
    ) -> None:
        with self._lock:
            db = self._db_or_raise()
            if keywords is None:
                row = db.execute(
                    "SELECT keywords_json FROM photo_selection WHERE name = ?", (name,)
                ).fetchone()
                keywords_json = str(row[0]) if row else "[]"
            else:
                keywords_json = json.dumps(
                    list(dict.fromkeys(str(item).strip() for item in keywords if str(item).strip())),
                    ensure_ascii=False, separators=(",", ":"),
                )
            db.execute(
                """INSERT OR REPLACE INTO photo_selection
                   (name, rating, color_label, comment, keywords_json, updated_ns)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (name, rating, color_label, comment, keywords_json, time.time_ns()),
            )
            db.commit()

    def load_xmp_states(self) -> dict[str, dict]:
        """Возвращает последние синхронизированные снимки sidecar-файлов."""
        with self._lock:
            rows = self._db_or_raise().execute(
                """SELECT sidecar_name, file_size, mtime_ns, digest, base_fields_json,
                          status, conflicts_json, error
                   FROM xmp_state"""
            ).fetchall()
        states = {}
        for name, size, mtime_ns, digest, base_json, status, conflicts_json, error in rows:
            try:
                base_fields = json.loads(base_json)
            except (TypeError, ValueError):
                base_fields = {}
            try:
                conflicts = json.loads(conflicts_json)
            except (TypeError, ValueError):
                conflicts = []
            states[str(name)] = {
                "size": int(size), "mtime_ns": int(mtime_ns), "digest": digest,
                "base_fields": base_fields if isinstance(base_fields, dict) else {},
                "status": str(status),
                "conflicts": conflicts if isinstance(conflicts, list) else [],
                "error": str(error or ""),
            }
        return states

    def store_xmp_state(
        self, sidecar_name: str, *, size: int, mtime_ns: int, digest: str | None,
        base_fields: dict, status: str = "synchronized",
        conflicts: list[dict] | None = None, error: str = "",
    ) -> None:
        """Фиксирует базу трёхстороннего слияния и видимый статус XMP."""
        with self._lock:
            self._db_or_raise().execute(
                """INSERT OR REPLACE INTO xmp_state
                   (sidecar_name, file_size, mtime_ns, digest, base_fields_json,
                    status, conflicts_json, error, updated_ns)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    sidecar_name, int(size), int(mtime_ns), digest,
                    json.dumps(base_fields, ensure_ascii=False, separators=(",", ":")),
                    status,
                    json.dumps(conflicts or [], ensure_ascii=False, separators=(",", ":")),
                    error, time.time_ns(),
                ),
            )
            self._db_or_raise().commit()

    def store_xmp_batch(self, selections: list[dict], states: list[dict]) -> None:
        """Записывает импортированный XMP одним фоновым SQLite-пакетом."""
        if not selections and not states:
            return
        with self._lock:
            db = self._db_or_raise()
            now = time.time_ns()
            if selections:
                db.executemany(
                    """INSERT OR REPLACE INTO photo_selection
                       (name, rating, color_label, comment, keywords_json, updated_ns)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        (
                            str(item["name"]), item.get("rating"),
                            str(item.get("color_label") or ""), str(item.get("comment") or ""),
                            json.dumps(item.get("keywords") or [], ensure_ascii=False, separators=(",", ":")),
                            now,
                        )
                        for item in selections
                    ),
                )
            if states:
                db.executemany(
                    """INSERT OR REPLACE INTO xmp_state
                       (sidecar_name, file_size, mtime_ns, digest, base_fields_json,
                        status, conflicts_json, error, updated_ns)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        (
                            str(item["sidecar_name"]), int(item.get("size") or 0),
                            int(item.get("mtime_ns") or 0), item.get("digest"),
                            json.dumps(item.get("base_fields") or {}, ensure_ascii=False, separators=(",", ":")),
                            str(item.get("status") or "synchronized"),
                            json.dumps(item.get("conflicts") or [], ensure_ascii=False, separators=(",", ":")),
                            str(item.get("error") or ""), now,
                        )
                        for item in states
                    ),
                )
            db.commit()

    def rename_photo_names(self, names: dict[str, str]) -> None:
        """Переносит кэш при переименовании, не ломаясь на обмене имён и циклах.

        Имена файлов служат ключами базы. Прямое обновление ``a.jpg`` в ``b.jpg``
        конфликтует, если оба участвуют в одной операции. Поэтому транзакция
        сначала выдаёт всем строкам временные имена, а уже затем назначает
        окончательные — обмены и произвольные циклы проходят без потерь.
        """
        changes = {str(old): str(new) for old, new in names.items() if old != new}
        if not changes:
            return
        if len(set(changes.values())) != len(changes):
            raise ValueError("Renamed filenames must be unique")

        tables = (
            "previews", "image_embeddings", "face_analysis", "quality_analysis",
            "photo_metadata", "photo_selection",
        )
        map_table = f"rename_map_{uuid4().hex}"
        prefix = f".__rawww_rename_{uuid4().hex}_"
        with self._lock:
            db = self._db_or_raise()
            try:
                db.execute("BEGIN")
                db.execute(
                    f"CREATE TEMP TABLE {map_table} (old_name TEXT PRIMARY KEY, new_name TEXT NOT NULL)"
                )
                db.executemany(
                    f"INSERT INTO {map_table} (old_name, new_name) VALUES (?, ?)",
                    changes.items(),
                )
                for table in tables:
                    db.execute(
                        f"UPDATE {table} SET name = ? || name "
                        f"WHERE name IN (SELECT old_name FROM {map_table})",
                        (prefix,),
                    )
                    db.execute(
                        f"UPDATE {table} SET name = ("
                        f"SELECT new_name FROM {map_table} "
                        f"WHERE old_name = substr({table}.name, ?)) "
                        f"WHERE substr(name, 1, ?) = ?",
                        (len(prefix) + 1, len(prefix), prefix),
                    )
                db.execute(f"DROP TABLE {map_table}")
                self.live_names = {changes.get(name, name) for name in self.live_names}
                db.commit()
            except Exception:
                db.rollback()
                raise

    def relocate_xmp_states(self, plan: dict[str, tuple[str, ...]]) -> None:
        """Повторяет перенос общих sidecar в производном состоянии синхронизации."""
        if not plan:
            return
        with self._lock:
            db = self._db_or_raise()
            try:
                db.execute("BEGIN")
                rows = {
                    source: db.execute(
                        """SELECT file_size, mtime_ns, digest, base_fields_json,
                                  status, conflicts_json, error, updated_ns
                           FROM xmp_state WHERE sidecar_name = ?""",
                        (source,),
                    ).fetchone()
                    for source in plan
                }
                all_targets = {target for targets in plan.values() for target in targets}
                db.executemany(
                    "DELETE FROM xmp_state WHERE sidecar_name = ?",
                    ((name,) for name in set(plan) | all_targets),
                )
                inserts = []
                for source, targets in plan.items():
                    row = rows.get(source)
                    if row is not None:
                        inserts.extend((target, *row) for target in targets)
                if inserts:
                    db.executemany(
                        """INSERT OR REPLACE INTO xmp_state
                           (sidecar_name, file_size, mtime_ns, digest, base_fields_json,
                            status, conflicts_json, error, updated_ns)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        inserts,
                    )
                db.commit()
            except Exception:
                db.rollback()
                raise

    def set_shotsync_session(self, shooting_id: int, title: str) -> None:
        """Связывает папку с синхронизированной съёмкой ShotSync."""
        with self._lock:
            db = self._db_or_raise()
            db.execute(
                """INSERT OR REPLACE INTO shotsync_session (id, shooting_id, title)
                   VALUES (1, ?, ?)""",
                (int(shooting_id), title or ""),
            )
            db.commit()

    def shotsync_session(self) -> tuple[int, str] | None:
        """Возвращает ``(shooting_id, title)``, если папка связана с ShotSync."""
        with self._lock:
            row = self._db_or_raise().execute(
                "SELECT shooting_id, title FROM shotsync_session WHERE id = 1"
            ).fetchone()
        return (int(row[0]), row[1] or "") if row else None

    def clear_shotsync_session(self) -> None:
        """Удаляет связь с ShotSync и снова делает папку обычной локальной."""
        with self._lock:
            db = self._db_or_raise()
            db.execute("DELETE FROM shotsync_session")
            db.execute("DELETE FROM shotsync_photos")
            db.execute("DELETE FROM shotsync_pending")
            db.commit()

    def set_shotsync_photos(self, mapping: list[tuple[str, int, int]]) -> None:
        """Пакетно сохраняет строки ``(name, photo_id, shooting_id)``."""
        if not mapping:
            return
        rows = _unique_shotsync_mapping(mapping)
        with self._lock:
            db = self._db_or_raise()
            db.executemany(
                """INSERT OR REPLACE INTO shotsync_photos (name, photo_id, shooting_id)
                   VALUES (?, ?, ?)""",
                rows,
            )
            db.commit()

    def replace_shotsync_photos(self, mapping: list[tuple[str, int, int]]) -> None:
        """Полностью заменяет соответствие локальных файлов фотографиям сервера."""
        rows = _unique_shotsync_mapping(mapping)
        with self._lock:
            db = self._db_or_raise()
            db.execute("DELETE FROM shotsync_photos")
            if rows:
                db.executemany(
                    """INSERT INTO shotsync_photos (name, photo_id, shooting_id)
                       VALUES (?, ?, ?)""",
                    rows,
                )
            db.commit()

    def shotsync_photo_names(self) -> list[str]:
        with self._lock:
            rows = self._db_or_raise().execute("SELECT name FROM shotsync_photos").fetchall()
        return [str(row[0]) for row in rows]

    def shotsync_photo_id(self, name: str) -> int | None:
        with self._lock:
            row = self._db_or_raise().execute(
                "SELECT photo_id FROM shotsync_photos WHERE name = ?", (name,)
            ).fetchone()
        return int(row[0]) if row else None

    def shotsync_local_name_for_photo_id(self, photo_id: int) -> str | None:
        """Возвращает локальное имя файла, сопоставленное с идентификатором фотографии сервера, если оно известно.
        """
        with self._lock:
            row = self._db_or_raise().execute(
                "SELECT name FROM shotsync_photos WHERE photo_id = ?", (int(photo_id),)
            ).fetchone()
        return str(row[0]) if row else None

    def enqueue_shotsync_mark(
        self, *, photo_id: int, shooting_id: int, kind: str, payload_json: str
    ) -> None:
        """Ставит метку в очередь, объединяя повторы по ``(photo_id, kind)``."""
        with self._lock:
            db = self._db_or_raise()
            db.execute(
                """INSERT OR REPLACE INTO shotsync_pending
                   (photo_id, kind, shooting_id, payload_json, updated_ns)
                   VALUES (?, ?, ?, ?, ?)""",
                (int(photo_id), kind, int(shooting_id), payload_json, time.time_ns()),
            )
            db.commit()

    def pending_shotsync_marks(self) -> list[dict]:
        """Возвращает ожидающие отправки метки, начиная с самых старых."""
        with self._lock:
            rows = self._db_or_raise().execute(
                """SELECT photo_id, kind, shooting_id, payload_json
                   FROM shotsync_pending ORDER BY updated_ns ASC"""
            ).fetchall()
        return [
            {"photo_id": int(r[0]), "kind": r[1], "shooting_id": int(r[2]), "payload_json": r[3]}
            for r in rows
        ]

    def clear_shotsync_mark(self, photo_id: int, kind: str) -> None:
        with self._lock:
            db = self._db_or_raise()
            db.execute(
                "DELETE FROM shotsync_pending WHERE photo_id = ? AND kind = ?",
                (int(photo_id), kind),
            )
            db.commit()

    def pending_shotsync_count(self) -> int:
        with self._lock:
            row = self._db_or_raise().execute(
                "SELECT COUNT(*) FROM shotsync_pending"
            ).fetchone()
        return int(row[0]) if row else 0

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
        """Открывает центральную базу и удаляет записи исчезнувших файлов."""
        with self._lock:
            if self._db is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                db = self._open_database()
            except sqlite3.DatabaseError:
                for suffix in ("", "-wal", "-shm"):
                    (Path(f"{self.path}{suffix}")).unlink(missing_ok=True)
                db = self._open_database()
            self._db = db

    def _open_database(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        try:
            _configure_database(db)
            db.executescript(SCHEMA)
            columns = {str(row[1]) for row in db.execute("PRAGMA table_info(photo_selection)")}
            if "keywords_json" not in columns:
                db.execute("ALTER TABLE photo_selection ADD COLUMN keywords_json TEXT NOT NULL DEFAULT '[]'")
            db.execute(
                "INSERT OR REPLACE INTO cache_info (id, folder_path) VALUES (1, ?)",
                (_folder_identity(self.folder),),
            )
            db.execute("DROP TABLE IF EXISTS audio_transcripts")
            self._remove_deleted_entries(db)
            db.commit()
            return db
        except Exception:
            db.close()
            raise

    def _remove_deleted_entries(self, db: sqlite3.Connection) -> None:
        db.execute("CREATE TEMP TABLE live_names (name TEXT PRIMARY KEY)")
        db.execute("CREATE TEMP TABLE live_sidecars (name TEXT PRIMARY KEY)")
        try:
            db.executemany("INSERT INTO live_names VALUES (?)", ((name,) for name in self.live_names))
            db.executemany(
                "INSERT OR IGNORE INTO live_sidecars VALUES (?)",
                ((Path(name).with_suffix(".xmp").name,) for name in self.live_names),
            )
            db.execute("DELETE FROM previews WHERE NOT EXISTS (SELECT 1 FROM live_names WHERE live_names.name = previews.name)")
            db.execute("DELETE FROM image_embeddings WHERE NOT EXISTS (SELECT 1 FROM live_names WHERE live_names.name = image_embeddings.name)")
            db.execute("DELETE FROM face_analysis WHERE NOT EXISTS (SELECT 1 FROM live_names WHERE live_names.name = face_analysis.name)")
            db.execute("DELETE FROM quality_analysis WHERE NOT EXISTS (SELECT 1 FROM live_names WHERE live_names.name = quality_analysis.name)")
            db.execute("DELETE FROM photo_metadata WHERE NOT EXISTS (SELECT 1 FROM live_names WHERE live_names.name = photo_metadata.name)")
            db.execute("DELETE FROM photo_selection WHERE NOT EXISTS (SELECT 1 FROM live_names WHERE live_names.name = photo_selection.name)")
            db.execute("DELETE FROM shotsync_photos WHERE NOT EXISTS (SELECT 1 FROM live_names WHERE live_names.name = shotsync_photos.name)")
            db.execute("DELETE FROM shotsync_pending WHERE photo_id NOT IN (SELECT photo_id FROM shotsync_photos)")
            db.execute("DELETE FROM xmp_state WHERE NOT EXISTS (SELECT 1 FROM live_sidecars WHERE live_sidecars.name = xmp_state.sidecar_name)")
        finally:
            db.execute("DROP TABLE live_names")
            db.execute("DROP TABLE live_sidecars")

    def _db_or_raise(self) -> sqlite3.Connection:
        if self._db is None:
            raise RuntimeError("Folder cache has not been opened")
        return self._db


def _unique_shotsync_mapping(mapping: list[tuple[str, int, int]]) -> list[tuple[str, int, int]]:
    """Оставляет по одной строке на локальное имя перед вставкой по ключу SQLite.
    """
    rows: dict[str, tuple[str, int, int]] = {}
    for name, photo_id, shooting_id in mapping:
        rows[str(name)] = (str(name), int(photo_id), int(shooting_id))
    return list(rows.values())


def cache_root() -> Path:
    if PORTABLE:
        return work_path() / "cache" / CACHE_DIRECTORY
    location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation)
    if location:
        return Path(location) / CACHE_APP_DIRECTORY / "cache" / CACHE_DIRECTORY
    return Path.home() / ".cache" / CACHE_APP_DIRECTORY / CACHE_DIRECTORY


def cache_path(folder: Path, root: Path | None = None) -> Path:
    digest = hashlib.sha256(_folder_identity(folder).encode("utf-8")).hexdigest()
    return (root or cache_root()) / f"{digest}.sqlite"


def _folder_identity(folder: Path) -> str:
    try:
        return str(folder.resolve(strict=False))
    except OSError:
        return str(folder.absolute())


def remove_folder_cache(folder: Path, *, cache_root: Path | None = None) -> None:
    """Удаляет кэш папки вместе со служебными файлами SQLite."""
    path = cache_path(folder, cache_root)
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def cache_size(root: Path | None = None) -> int:
    """Возвращает суммарный размер файлов кэша в байтах."""
    root = root or cache_root()
    if not root.is_dir():
        return 0
    total = 0
    for path in root.rglob("*"):
        if path.is_file():
            try:
                total += path.stat().st_size
            except OSError:
                continue
    return total


def clear_cache(root: Path | None = None) -> None:
    """Удаляет кэши всех папок, но сохраняет сам корневой каталог кэша."""
    root = root or cache_root()
    if not root.is_dir():
        return
    for path in root.iterdir():
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except FileNotFoundError:
            continue


def relocate_folder_caches(old_folder: Path, new_folder: Path, *, cache_dir: Path | None = None) -> int:
    """Переносит кэши переименованной папки и всех её закэшированных потомков.

    Имя базы — хэш абсолютного пути. После переименования файл получает новый
    хэш, а запись ``cache_info`` обновляется, поэтому превью и результаты AI не
    приходится считать заново.
    """
    root = cache_dir or cache_root()
    if not root.is_dir():
        return 0
    old_identity = Path(_folder_identity(old_folder))
    new_identity = Path(_folder_identity(new_folder))
    relocations: list[tuple[Path, Path, Path]] = []
    for source in root.glob("*.sqlite"):
        try:
            db = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=1)
            try:
                row = db.execute("SELECT folder_path FROM cache_info WHERE id = 1").fetchone()
            finally:
                db.close()
        except sqlite3.DatabaseError:
            continue
        if not row or not row[0]:
            continue
        folder = Path(str(row[0]))
        try:
            relative = folder.relative_to(old_identity)
        except ValueError:
            continue
        destination_folder = new_identity / relative
        relocations.append((source, folder, destination_folder))

    moved = 0
    for source, _folder, destination_folder in relocations:
        destination = cache_path(destination_folder, root)
        if source == destination:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            Path(f"{destination}{suffix}").unlink(missing_ok=True)
        for suffix in ("", "-wal", "-shm"):
            source_sidecar = Path(f"{source}{suffix}")
            if source_sidecar.exists():
                source_sidecar.replace(Path(f"{destination}{suffix}"))
        db = sqlite3.connect(destination, timeout=30)
        try:
            db.execute(
                "INSERT OR REPLACE INTO cache_info (id, folder_path) VALUES (1, ?)",
                (_folder_identity(destination_folder),),
            )
            db.commit()
        finally:
            db.close()
        moved += 1
    return moved


def prune_folder_cache(folder: Path, *, cache_root: Path | None = None) -> None:
    """Удаляет записи исчезнувших исходников и уплотняет кэш."""
    names = {
        path.name
        for path in folder.iterdir()
        if path.is_file()
    }
    cache = FolderCache(folder, live_names=names, load_from_disk=True, cache_root=cache_root)
    try:
        cache.prune_deleted_entries(vacuum=True)
    finally:
        cache.close(flush=False)


def maintain_folder_caches(root: Path | None = None) -> dict[str, int]:
    """Удаляет бесхозные кэши и уплотняет базы после удаления исходников.

    Старые базы без ``cache_info`` остаются нетронутыми: по одному хэшу имени
    нельзя надёжно восстановить исходную папку. Сведения появятся естественным
    образом при следующем открытии такого кэша.
    """
    root = root or cache_root()
    result = {"removed": 0, "optimized": 0, "skipped": 0}
    if not root.is_dir():
        return result
    for database_path in root.glob("*.sqlite"):
        try:
            db = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True, timeout=1)
            try:
                has_info = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'cache_info'"
                ).fetchone()
                row = db.execute("SELECT folder_path FROM cache_info WHERE id = 1").fetchone() if has_info else None
                cached_names = {
                    name
                    for (name,) in db.execute(
                        """
                        SELECT name FROM previews
                        UNION SELECT name FROM image_embeddings
                        UNION SELECT name FROM face_analysis
                        UNION SELECT name FROM quality_analysis
                        UNION SELECT name FROM photo_metadata
                        UNION SELECT name FROM photo_selection
                        UNION SELECT name FROM shotsync_photos
                        """
                    )
                } if row else set()
            finally:
                db.close()
        except sqlite3.DatabaseError:
            result["skipped"] += 1
            continue
        except OSError:
            result["skipped"] += 1
            continue
        if not row or not row[0]:
            result["skipped"] += 1
            continue
        folder = Path(str(row[0]))
        if not folder.is_dir():
            for suffix in ("", "-wal", "-shm"):
                Path(f"{database_path}{suffix}").unlink(missing_ok=True)
            result["removed"] += 1
            continue
        try:
            names = {path.name for path in folder.iterdir() if path.is_file()}
            if cached_names - names:
                cache = FolderCache(folder, names, load_from_disk=True, cache_root=root)
                try:
                    cache.prune_deleted_entries(vacuum=True)
                    result["optimized"] += 1
                finally:
                    cache.close(flush=False)
        except (OSError, sqlite3.DatabaseError):
            result["skipped"] += 1
    return result


def _configure_database(db: sqlite3.Connection) -> None:
    """Настраивает SQLite для частой записи превью и результатов AI."""
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
