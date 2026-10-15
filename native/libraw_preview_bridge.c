/* Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * Доступ к полям LibRaw через его настоящие заголовки. Python не повторяет
 * огромную libraw_data_t и не угадывает смещения полей разных версий ABI.
 * Память принадлежит LibRaw; мост только читает её до libraw_close/clear_mem.
 */
#include <stdint.h>
#include "libraw/libraw_types.h"
#include "libraw/libraw_version.h"

#ifdef _WIN32
#define EXPORT __declspec(dllexport)
#else
#define EXPORT __attribute__((visibility("default")))
#endif

EXPORT int rawww_headers_version(void) { return LIBRAW_VERSION; }
EXPORT int rawww_bridge_version(void) { return 1; }

/* make_mem_thumb добавляет EXIF из sizes.flip, если JPEG не содержит свой.
 * TIFF-превью может иметь собственный поворот, отличный от исходного RAW. */
EXPORT void rawww_preview_orientation(libraw_data_t *raw, int index)
{
    if (!raw || index < 0 || index >= raw->thumbs_list.thumbcount ||
        index >= LIBRAW_THUMBNAIL_MAXCOUNT)
        return;
    unsigned flip = raw->thumbs_list.thumblist[index].tflip;
    if (flip <= 7)
        raw->sizes.flip = (int)flip;
}

/* Неподдерживаемый внутренний формат не выдаётся за JPEG. */
EXPORT int rawww_preview_info(const libraw_data_t *raw, int index, int64_t *out)
{
    if (!raw || !out || index < 0 || index >= raw->thumbs_list.thumbcount ||
        index >= LIBRAW_THUMBNAIL_MAXCOUNT)
        return 0;
    const libraw_thumbnail_item_t *item = &raw->thumbs_list.thumblist[index];
    out[0] = item->twidth;
    out[1] = item->theight;
    out[2] = item->tflip;
    out[3] = item->tlength;
    out[4] = item->tformat == LIBRAW_INTERNAL_THUMBNAIL_JPEG;
    out[5] = raw->sizes.flip;
    return 1;
}

/* Копирование JPEG выполняется вызывающей стороной до освобождения image. */
EXPORT const unsigned char *rawww_jpeg_data(const libraw_processed_image_t *image,
                                           uint64_t *size)
{
    if (!image || !size || image->type != LIBRAW_IMAGE_JPEG)
        return 0;
    *size = image->data_size;
    return image->data;
}
