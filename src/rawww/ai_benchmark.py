from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from PySide6.QtCore import QSettings

from .ai import extract_embedding_batch, prepare_analysis_batch, recognize_face_batch
from .cache import FolderCache
from .exif import extract_metadata_batch
from .imaging import decode_pixels, is_supported_image


def main() -> None:
    default = Path(str(QSettings("RAWww", "RAWww").value("last_directory", Path.cwd())))
    parser = argparse.ArgumentParser(description="Benchmark RAWww photo processing stages.")
    parser.add_argument("folder", type=Path, nargs="?", default=default)
    parser.add_argument("--limit", type=int, default=32)
    parser.add_argument("--thumb-size", type=int, default=256)
    args = parser.parse_args()
    paths = sorted((p for p in args.folder.iterdir() if p.is_file() and is_supported_image(p)), key=lambda p: p.name.lower())[:args.limit]
    if not paths:
        raise SystemExit("No supported images found")
    print(f"Folder: {args.folder}")
    print(f"Images: {len(paths)} ({sum(p.stat().st_size for p in paths) / 1048576:.2f} MiB)")
    with TemporaryDirectory(prefix="rawww-benchmark-") as temporary:
        cache = FolderCache(args.folder, {p.name for p in paths}, cache_root=Path(temporary))
        exif, exif_s = timed(extract_metadata_batch, [str(p) for p in paths])
        pixels, decode_s = timed(lambda: [decode_pixels(p, args.thumb_size) for p in paths])
        _, preview_write_s = timed(lambda: [cache.store_pixels(pixel, args.thumb_size) for pixel in pixels])
        sources, source_s = timed(prepare_analysis_batch, [str(p) for p in paths])
        first_emb, clip_cold_s = timed(extract_embedding_batch, sources[:1])
        warm_emb, clip_warm_s = timed(extract_embedding_batch, sources[1:])
        embeddings = [*first_emb, *warm_emb]
        _, emb_write_s = timed(cache.store_image_embeddings, embeddings)
        first_faces, face_cold_s = timed(recognize_face_batch, sources[:1])
        warm_faces, face_warm_s = timed(recognize_face_batch, sources[1:])
        faces = [*first_faces, *warm_faces]
        _, face_write_s = timed(cache.store_face_analysis, faces)
        _, exif_write_s = timed(cache.store_photo_metadata, exif)
        with closing(sqlite3.connect(cache.path)) as db:
            face_count = sum(len(json.loads(row[0])) for row in db.execute("SELECT faces_json FROM face_analysis"))
        cache_size = sum(p.stat().st_size for p in cache.path.parent.glob(f"{cache.path.name}*") if p.is_file())
        cache.close(flush=False)
    show("EXIF, stay-open ExifTool", len(exif), exif_s)
    show(f"Preview decode {args.thumb_size}px", len(pixels), decode_s)
    show("Preview SQLite write", len(pixels), preview_write_s)
    show("Reusable 640px JPEG in memory", len(sources), source_s)
    show("CLIP cold model + first image", len(first_emb), clip_cold_s)
    show("CLIP warm batch", len(warm_emb), clip_warm_s)
    show("CLIP SQLite write", len(embeddings), emb_write_s)
    show("InsightFace cold + first image", len(first_faces), face_cold_s)
    show("InsightFace warm images", len(warm_faces), face_warm_s)
    show("Face SQLite write", len(faces), face_write_s)
    show("EXIF SQLite write", len(exif), exif_write_s)
    print(f"Faces found: {face_count}")
    print(f"Temporary cache size: {cache_size / 1048576:.2f} MiB")


def timed(function, *args):
    started = perf_counter()
    result = function(*args)
    return result, perf_counter() - started


def show(label: str, count: int, seconds: float) -> None:
    rate = count / seconds if seconds and count else 0.0
    latency = seconds * 1000 / count if count else 0.0
    print(f"{label:36} {seconds:8.3f}s  {rate:7.2f} img/s  {latency:8.2f} ms/img")


if __name__ == "__main__":
    main()
