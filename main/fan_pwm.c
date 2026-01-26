#include "fan_pwm.h"
#include "driver/ledc.h"
#include "esp_log.h"

static const char *TAG = "fan_pwm";

#define LEDC_TIMER          LEDC_TIMER_0
#define LEDC_MODE           LEDC_LOW_SPEED_MODE
#define LEDC_CHANNEL        LEDC_CHANNEL_0
#define LEDC_DUTY_RES       LEDC_TIMER_10_BIT  // 0-1023 resolution
#define LEDC_MAX_DUTY       ((1 << LEDC_DUTY_RES) - 1)

static uint8_t current_percent = 0;

esp_err_t fan_pwm_init(int gpio_num)
{
    ESP_LOGI(TAG, "Initializing PWM on GPIO %d at %d Hz", gpio_num, FAN_PWM_FREQ_HZ);

    ledc_timer_config_t timer_conf = {
        .speed_mode = LEDC_MODE,
        .timer_num = LEDC_TIMER,
        .duty_resolution = LEDC_DUTY_RES,
        .freq_hz = FAN_PWM_FREQ_HZ,
        .clk_cfg = LEDC_AUTO_CLK,
    };
    esp_err_t err = ledc_timer_config(&timer_conf);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to configure LEDC timer: %s", esp_err_to_name(err));
        return err;
    }

    ledc_channel_config_t channel_conf = {
        .speed_mode = LEDC_MODE,
        .channel = LEDC_CHANNEL,
        .timer_sel = LEDC_TIMER,
        .intr_type = LEDC_INTR_DISABLE,
        .gpio_num = gpio_num,
        .duty = 0,
        .hpoint = 0,
    };
    err = ledc_channel_config(&channel_conf);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to configure LEDC channel: %s", esp_err_to_name(err));
        return err;
    }

    // Start at a safe 50%
    fan_pwm_set_percent(50);

    return ESP_OK;
}

void fan_pwm_set_percent(uint8_t percent)
{
    if (percent > 100) {
        percent = 100;
    }
    current_percent = percent;

    uint32_t duty = (percent * LEDC_MAX_DUTY) / 100;
    ledc_set_duty(LEDC_MODE, LEDC_CHANNEL, duty);
    ledc_update_duty(LEDC_MODE, LEDC_CHANNEL);

    ESP_LOGI(TAG, "Fan speed set to %d%% (duty: %lu/%d)", percent, duty, LEDC_MAX_DUTY);
}

uint8_t fan_pwm_get_percent(void)
{
    return current_percent;
}
