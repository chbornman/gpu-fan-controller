#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "nvs_flash.h"

#include "fan_pwm.h"
#include "fan_tach.h"
#include "usb_serial.h"
#include "proto.h"

static const char *TAG = "gpu_fan";

#define GPIO_FAN_PWM    4
#define GPIO_FAN_TACH   5

// Device watchdog: if no frame arrives within this window, fall back to 100%.
// Host keepalive is every ~1s so 5s gives plenty of margin for transient glitches.
#define WATCHDOG_TIMEOUT_MS     5000
#define WATCHDOG_FAILSAFE_SPEED 100

// Unsolicited STATUS cadence. Host declares the link dead if it doesn't see one
// within host_watchdog_s, so this must be comfortably shorter than that.
#define STATUS_INTERVAL_MS      500

static int64_t last_frame_time_us = 0;
static bool    watchdog_triggered = false;

static void reset_watchdog(void)
{
    last_frame_time_us = esp_timer_get_time();
    if (watchdog_triggered) {
        watchdog_triggered = false;
        ESP_LOGI(TAG, "Watchdog cleared — control restored");
    }
}

static void check_watchdog(void)
{
    if (watchdog_triggered) return;
    int64_t elapsed_ms = (esp_timer_get_time() - last_frame_time_us) / 1000;
    if (elapsed_ms > WATCHDOG_TIMEOUT_MS) {
        watchdog_triggered = true;
        ESP_LOGW(TAG, "WATCHDOG: no frame in %lld ms — ramping to %d%%",
                 elapsed_ms, WATCHDOG_FAILSAFE_SPEED);
        fan_pwm_set_percent(WATCHDOG_FAILSAFE_SPEED);
    }
}

static void on_frame(uint8_t type, const uint8_t *payload, size_t payload_len)
{
    switch (type) {
    case MSG_CMD_SET:
        if (payload_len != 1) {
            ESP_LOGW(TAG, "CMD_SET: bad payload len %u", (unsigned)payload_len);
            return;
        }
        {
            uint8_t pct = payload[0];
            if (pct > 100) pct = 100;
            fan_pwm_set_percent(pct);
        }
        break;

    case MSG_CMD_KEEPALIVE:
        break;

    default:
        ESP_LOGW(TAG, "Unknown frame type 0x%02x", type);
        return;
    }
    reset_watchdog();
}

static void send_status(void)
{
    status_payload_t s = {
        .speed        = fan_pwm_get_percent(),
        .rpm          = (uint16_t)fan_tach_get_rpm(),
        .wd_triggered = watchdog_triggered ? 1 : 0,
        .uptime_ms    = (uint32_t)(esp_timer_get_time() / 1000),
    };
    usb_serial_send_frame(MSG_STATUS, (const uint8_t *)&s, sizeof(s));
}

void app_main(void)
{
    ESP_LOGI(TAG, "=== GPU Fan Controller ===");
    ESP_LOGI(TAG, "PWM GPIO: %d, TACH GPIO: %d", GPIO_FAN_PWM, GPIO_FAN_TACH);
    ESP_LOGI(TAG, "Device watchdog: %d ms -> %d%%, STATUS every %d ms",
             WATCHDOG_TIMEOUT_MS, WATCHDOG_FAILSAFE_SPEED, STATUS_INTERVAL_MS);

    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    ESP_ERROR_CHECK(usb_serial_init());
    ESP_ERROR_CHECK(fan_pwm_init(GPIO_FAN_PWM));
    ESP_ERROR_CHECK(fan_tach_init(GPIO_FAN_TACH));

    usb_serial_set_frame_callback(on_frame);
    last_frame_time_us = esp_timer_get_time();

    // All ESP_LOG calls are compiled out (CONFIG_LOG_DEFAULT_LEVEL_NONE) so
    // nothing besides protocol frames can ever reach UART0.

    int64_t next_status_us = esp_timer_get_time();
    while (1) {
        usb_serial_process();
        check_watchdog();

        int64_t now = esp_timer_get_time();
        if (now >= next_status_us) {
            send_status();
            next_status_us = now + STATUS_INTERVAL_MS * 1000LL;
        }

        vTaskDelay(pdMS_TO_TICKS(10));
    }
}
