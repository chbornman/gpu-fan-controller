#include "proto.h"
#include "cobs.h"
#include <string.h>

size_t proto_encode_frame(uint8_t type,
                          const uint8_t *payload, size_t payload_len,
                          uint8_t *out, size_t out_cap)
{
    if (payload_len > PROTO_MAX_PAYLOAD) return 0;

    uint8_t buf[1 + PROTO_MAX_PAYLOAD];
    buf[0] = type;
    if (payload_len > 0) {
        memcpy(buf + 1, payload, payload_len);
    }

    if (out_cap < PROTO_MAX_FRAME) return 0;
    size_t n = cobs_encode(buf, 1 + payload_len, out);
    if (n == 0 || n + 1 > out_cap) return 0;
    out[n] = 0x00;
    return n + 1;
}

bool proto_decode_frame(const uint8_t *frame, size_t frame_len,
                        uint8_t *out_type,
                        uint8_t *out_payload, size_t *out_payload_len)
{
    uint8_t buf[1 + PROTO_MAX_PAYLOAD];
    if (frame_len == 0 || frame_len > sizeof(buf) + 2) return false;

    size_t n = cobs_decode(frame, frame_len, buf);
    if (n == 0) return false;

    *out_type = buf[0];
    size_t pl = n - 1;
    if (pl > *out_payload_len) return false;
    if (pl > 0) memcpy(out_payload, buf + 1, pl);
    *out_payload_len = pl;
    return true;
}
