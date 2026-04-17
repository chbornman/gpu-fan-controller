#pragma once

#include <stddef.h>
#include <stdint.h>

// Consistent Overhead Byte Stuffing — framing for a self-synchronising byte
// stream. 0x00 never appears inside an encoded frame, so the receiver can
// always resync on the next 0x00 delimiter regardless of prior garbage.

// Encode src[0..src_len) into dst. Worst-case output size is src_len +
// ceil(src_len/254) + 1. Returns bytes written to dst (no trailing delimiter).
size_t cobs_encode(const uint8_t *src, size_t src_len, uint8_t *dst);

// Decode COBS-encoded src into dst. src must NOT contain the 0x00 delimiter.
// Returns bytes written to dst, or 0 on decode error.
size_t cobs_decode(const uint8_t *src, size_t src_len, uint8_t *dst);
