#include "fan_tach.h"
#include "driver/gpio.h"
#include "esp_attr.h"
#include "esp_timer.h"
#include "esp_log.h"
#include <stdatomic.h>

static const char *TAG = "fan_tach";

// Pulse counting
static atomic_uint_fast32_t pulse_count = 0;
static uint32_t last_pulse_count = 0;
static int64_t last_calc_time_us = 0;
static uint32_t cached_rpm = 0;

// RPM calculation interval
#define RPM_CALC_INTERVAL_MS 500

static void IRAM_ATTR tach_isr_handler(void *arg)
{
    atomic_fetch_add(&pulse_count, 1);
}

esp_err_t fan_tach_init(int gpio_num)
{
    ESP_LOGI(TAG, "Initializing tachometer input on GPIO %d", gpio_num);

    gpio_config_t io_conf = {
        .intr_type = GPIO_INTR_NEGEDGE,  // Tach pulses low
        .mode = GPIO_MODE_INPUT,
        .pin_bit_mask = (1ULL << gpio_num),
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .pull_up_en = GPIO_PULLUP_ENABLE,  // Internal pull-up for open-drain tach
    };

    esp_err_t err = gpio_config(&io_conf);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to configure GPIO: %s", esp_err_to_name(err));
        return err;
    }

    err = gpio_install_isr_service(0);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        // ESP_ERR_INVALID_STATE means already installed, which is fine
        ESP_LOGE(TAG, "Failed to install ISR service: %s", esp_err_to_name(err));
        return err;
    }

    err = gpio_isr_handler_add(gpio_num, tach_isr_handler, NULL);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to add ISR handler: %s", esp_err_to_name(err));
        return err;
    }

    last_calc_time_us = esp_timer_get_time();

    ESP_LOGI(TAG, "Tachometer initialized");
    return ESP_OK;
}

uint32_t fan_tach_get_rpm(void)
{
    int64_t now_us = esp_timer_get_time();
    int64_t elapsed_us = now_us - last_calc_time_us;

    // Only recalculate every RPM_CALC_INTERVAL_MS
    if (elapsed_us < (RPM_CALC_INTERVAL_MS * 1000)) {
        return cached_rpm;
    }

    uint32_t current_count = atomic_load(&pulse_count);
    uint32_t pulses = current_count - last_pulse_count;

    // RPM = (pulses / pulses_per_rev) * (60 seconds / elapsed_seconds)
    // RPM = (pulses / TACH_PULSES_PER_REV) * (60 * 1000000 / elapsed_us)
    // RPM = pulses * 60 * 1000000 / (TACH_PULSES_PER_REV * elapsed_us)

    if (elapsed_us > 0) {
        cached_rpm = (pulses * 60ULL * 1000000ULL) / (TACH_PULSES_PER_REV * elapsed_us);
    } else {
        cached_rpm = 0;
    }

    last_pulse_count = current_count;
    last_calc_time_us = now_us;

    return cached_rpm;
}

uint32_t fan_tach_get_pulse_count(void)
{
    return atomic_load(&pulse_count);
}
