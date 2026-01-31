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

static const char *TAG = "gpu_fan";

// GPIO Configuration - adjust these for your wiring
#define GPIO_FAN_PWM    4   // Blue wire - PWM control
#define GPIO_FAN_TACH   5   // Green wire - Tachometer input

// Watchdog: if no command received in this time, ramp to 100%
#define WATCHDOG_TIMEOUT_MS 5000
#define WATCHDOG_FAILSAFE_SPEED 100

static int64_t last_command_time_us = 0;
static bool watchdog_triggered = false;

// Protocol commands:
//   SET <0-100>    - Set fan speed percentage (resets watchdog)
//   GET            - Get current fan speed setting
//   RPM            - Get current RPM reading
//   STATUS         - Get full status (speed + RPM + watchdog state)
//   PING           - Connectivity check, responds PONG (resets watchdog)
//   WD             - Get watchdog status

static void reset_watchdog(void)
{
    last_command_time_us = esp_timer_get_time();
    if (watchdog_triggered) {
        watchdog_triggered = false;
        ESP_LOGI(TAG, "Watchdog reset - control restored");
        usb_serial_send("INFO: Watchdog reset, control restored\n");
    }
}

static void handle_command(const char *cmd, const char *arg)
{
    if (strcasecmp(cmd, "SET") == 0) {
        if (arg == NULL) {
            usb_serial_send("ERR: SET requires value 0-100\n");
            return;
        }
        int val = atoi(arg);
        if (val < 0 || val > 100) {
            usb_serial_send("ERR: Value must be 0-100\n");
            return;
        }
        reset_watchdog();
        fan_pwm_set_percent((uint8_t)val);
        usb_serial_sendf("OK: %d\n", val);
    }
    else if (strcasecmp(cmd, "GET") == 0) {
        usb_serial_sendf("SPEED: %d\n", fan_pwm_get_percent());
    }
    else if (strcasecmp(cmd, "RPM") == 0) {
        usb_serial_sendf("RPM: %lu\nPULSES: %lu\n", fan_tach_get_rpm(), fan_tach_get_pulse_count());
    }
    else if (strcasecmp(cmd, "STATUS") == 0) {
        int64_t since_cmd_ms = (esp_timer_get_time() - last_command_time_us) / 1000;
        usb_serial_sendf("SPEED: %d\nRPM: %lu\nWATCHDOG: %s\nLAST_CMD_MS: %lld\n",
            fan_pwm_get_percent(),
            fan_tach_get_rpm(),
            watchdog_triggered ? "TRIGGERED" : "OK",
            since_cmd_ms);
    }
    else if (strcasecmp(cmd, "WD") == 0) {
        int64_t since_cmd_ms = (esp_timer_get_time() - last_command_time_us) / 1000;
        usb_serial_sendf("WATCHDOG: %s\nTIMEOUT_MS: %d\nLAST_CMD_MS: %lld\n",
            watchdog_triggered ? "TRIGGERED" : "OK",
            WATCHDOG_TIMEOUT_MS,
            since_cmd_ms);
    }
    else if (strcasecmp(cmd, "PING") == 0) {
        reset_watchdog();
        usb_serial_send("PONG\n");
    }
    else {
        usb_serial_sendf("ERR: Unknown command '%s'\n", cmd);
        usb_serial_send("Commands: SET <0-100>, GET, RPM, STATUS, PING, WD\n");
    }
}

static void check_watchdog(void)
{
    if (watchdog_triggered) {
        return;  // Already in failsafe mode
    }

    int64_t now_us = esp_timer_get_time();
    int64_t elapsed_ms = (now_us - last_command_time_us) / 1000;

    if (elapsed_ms > WATCHDOG_TIMEOUT_MS) {
        watchdog_triggered = true;
        ESP_LOGW(TAG, "WATCHDOG TRIGGERED! No command for %lld ms. Ramping to %d%%",
            elapsed_ms, WATCHDOG_FAILSAFE_SPEED);
        fan_pwm_set_percent(WATCHDOG_FAILSAFE_SPEED);
        usb_serial_sendf("WARN: Watchdog triggered! Fan set to %d%%\n", WATCHDOG_FAILSAFE_SPEED);
    }
}

void app_main(void)
{
    ESP_LOGI(TAG, "=== GPU Fan Controller ===");
    ESP_LOGI(TAG, "PWM GPIO: %d, TACH GPIO: %d", GPIO_FAN_PWM, GPIO_FAN_TACH);
    ESP_LOGI(TAG, "Watchdog timeout: %d ms, Failsafe speed: %d%%",
        WATCHDOG_TIMEOUT_MS, WATCHDOG_FAILSAFE_SPEED);

    // Initialize NVS (required for some drivers)
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    // Initialize subsystems
    ESP_ERROR_CHECK(usb_serial_init());
    ESP_ERROR_CHECK(fan_pwm_init(GPIO_FAN_PWM));
    ESP_ERROR_CHECK(fan_tach_init(GPIO_FAN_TACH));

    // Register command handler
    usb_serial_set_callback(handle_command);

    // Initialize watchdog timer
    last_command_time_us = esp_timer_get_time();

    ESP_LOGI(TAG, "Initialization complete. Waiting for commands...");
    usb_serial_send("\n=== GPU Fan Controller Ready ===\n");
    usb_serial_sendf("Watchdog: %d ms timeout, %d%% failsafe\n",
        WATCHDOG_TIMEOUT_MS, WATCHDOG_FAILSAFE_SPEED);
    usb_serial_send("Commands: SET <0-100>, GET, RPM, STATUS, PING, WD\n\n");

    // Main loop
    uint32_t loop_count = 0;
    while (1) {
        usb_serial_process();
        check_watchdog();

        // Log status every 10 seconds
        if (++loop_count >= 1000) {
            ESP_LOGI(TAG, "Fan: %d%%, RPM: %lu, WD: %s",
                fan_pwm_get_percent(),
                fan_tach_get_rpm(),
                watchdog_triggered ? "TRIGGERED" : "OK");
            loop_count = 0;
        }

        vTaskDelay(pdMS_TO_TICKS(10));
    }
}
