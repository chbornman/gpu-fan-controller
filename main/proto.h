#pragma once

#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>

// Binary wire protocol for host <-> ESP32 fan controller.
// Every frame is COBS-encoded and terminated with a single 0x00 byte.
// Pre-encoding layout: [type:u8][payload...]. All multi-byte ints little-endian.

typedef enum {
    // Host -> device
    MSG_CMD_SET       = 0x01,  // payload: u8 percent (0..100)
    MSG_CMD_KEEPALIVE = 0x02,  // no payload — just resets the device watchdog

    // Device -> host
    MSG_STATUS        = 0x10,  // payload: status_payload_t (unsolicited, periodic)
} msg_type_t;

typedef struct __attribute__((packed)) {
    uint8_t  speed;          // Current PWM setpoint (0..100)
    uint16_t rpm;            // Latest tachometer reading
    uint8_t  wd_triggered;   // 0 = normal, 1 = failsafe engaged
    uint32_t uptime_ms;      // Device uptime since boot
} status_payload_t;

// Pre-COBS envelope + safety headroom. STATUS is the largest frame at 9 bytes.
#define PROTO_MAX_PAYLOAD  32
#define PROTO_MAX_FRAME    64

// Encode a frame into `out` including the trailing 0x00 delimiter.
// Returns total bytes written (including delimiter), or 0 on error.
size_t proto_encode_frame(uint8_t type,
                          const uint8_t *payload, size_t payload_len,
                          uint8_t *out, size_t out_cap);

// Decode one COBS-encoded frame (delimiter already stripped).
// On success writes *out_type and copies payload into out_payload (up to
// *out_payload_len bytes, updated to actual length). Returns true on success.
bool proto_decode_frame(const uint8_t *frame, size_t frame_len,
                        uint8_t *out_type,
                        uint8_t *out_payload, size_t *out_payload_len);
