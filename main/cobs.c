#include "cobs.h"

size_t cobs_encode(const uint8_t *src, size_t src_len, uint8_t *dst)
{
    size_t read = 0;
    size_t write = 1;
    size_t code_idx = 0;
    uint8_t code = 1;

    while (read < src_len) {
        if (src[read] == 0) {
            dst[code_idx] = code;
            code_idx = write++;
            code = 1;
            read++;
        } else {
            dst[write++] = src[read++];
            code++;
            if (code == 0xFF) {
                dst[code_idx] = code;
                code_idx = write++;
                code = 1;
            }
        }
    }
    dst[code_idx] = code;
    return write;
}

size_t cobs_decode(const uint8_t *src, size_t src_len, uint8_t *dst)
{
    size_t read = 0;
    size_t write = 0;

    while (read < src_len) {
        uint8_t code = src[read++];
        if (code == 0) {
            return 0;
        }
        for (uint8_t i = 1; i < code; i++) {
            if (read >= src_len) return 0;
            dst[write++] = src[read++];
        }
        // Emit implicit zero between blocks, but not after the final block
        // and not when the block ran to its maximum (0xFF) length.
        if (code != 0xFF && read < src_len) {
            dst[write++] = 0;
        }
    }
    return write;
}
