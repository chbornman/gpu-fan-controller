#include "usb_serial.h"
#include "esp_log.h"
#include <string.h>
#include <stdarg.h>
#include <stdio.h>

static const char *TAG = "usb_serial";

#define LINE_BUF_SIZE 128

static char line_buf[LINE_BUF_SIZE];
static size_t line_pos = 0;
static usb_serial_cmd_callback_t cmd_callback = NULL;

esp_err_t usb_serial_init(void)
{
    ESP_LOGI(TAG, "USB Serial initialized");
    return ESP_OK;
}

void usb_serial_set_callback(usb_serial_cmd_callback_t callback)
{
    cmd_callback = callback;
}

void usb_serial_send(const char *response)
{
    printf("%s", response);
    fflush(stdout);
}

void usb_serial_sendf(const char *fmt, ...)
{
    va_list args;
    va_start(args, fmt);
    vprintf(fmt, args);
    va_end(args);
    fflush(stdout);
}

static void parse_and_dispatch(char *line)
{
    // Trim trailing whitespace
    size_t len = strlen(line);
    while (len > 0 && (line[len-1] == '\r' || line[len-1] == '\n' || line[len-1] == ' ')) {
        line[--len] = '\0';
    }

    if (len == 0) {
        return;
    }

    // Split into command and argument
    char *cmd = line;
    char *arg = NULL;

    char *space = strchr(line, ' ');
    if (space) {
        *space = '\0';
        arg = space + 1;
        while (*arg == ' ') arg++;
    }

    ESP_LOGI(TAG, "Command: '%s', Arg: '%s'", cmd, arg ? arg : "(none)");

    if (cmd_callback) {
        cmd_callback(cmd, arg);
    }
}

void usb_serial_process(void)
{
    int c = getchar();

    while (c != EOF) {
        if (c == '\n' || c == '\r') {
            if (line_pos > 0) {
                line_buf[line_pos] = '\0';
                parse_and_dispatch(line_buf);
                line_pos = 0;
            }
        } else if (line_pos < LINE_BUF_SIZE - 1) {
            line_buf[line_pos++] = (char)c;
        }
        c = getchar();
    }
}
