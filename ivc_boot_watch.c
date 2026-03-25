#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
 
enum target_state {
    TARGET_WAITING = 0,
    TARGET_ACTIVE = 1,
    TARGET_TIMED_OUT = 2,
};

struct target_watch {
    const char *name;
    int irq;
    uint64_t base;
    uint64_t last;
    time_t last_activity;
    enum target_state state;
    int timeout_reported;
    int read_error_reported;
};

static int read_irq_count(int irq, uint64_t *out_count) {
    char path[64];
    snprintf(path, sizeof(path), "/proc/irq/%d/spurious", irq);
 
    FILE *fp = fopen(path, "r");
    if (!fp) {
        return -1;
    }
 
    char line[128];
    if (!fgets(line, sizeof(line), fp)) {
        fclose(fp);
        return -1;
    }
    fclose(fp);
 
    uint64_t count = 0;
    if (sscanf(line, "count %" SCNu64, &count) != 1) {
        errno = EPROTO;
        return -1;
    }
    *out_count = count;
    return 0;
}

static int parse_int_arg(const char *text, const char *label, int min_value, int *out_value) {
    char *end = NULL;
    long value;

    errno = 0;
    value = strtol(text, &end, 0);
    if (errno != 0 || end == text || *end != '\0' || value < min_value || value > INT_MAX) {
        fprintf(stderr, "invalid %s: %s\n", label, text);
        return -1;
    }

    *out_value = (int)value;
    return 0;
}
 
static void print_ts(FILE *out) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    struct tm tmv;
    localtime_r(&ts.tv_sec, &tmv);
    char buf[64];
    strftime(buf, sizeof(buf), "%F %T", &tmv);
    fprintf(out, "%s.%03ld", buf, ts.tv_nsec / 1000000L);
}
 
static void sleep_ms(int ms) {
    struct timespec ts;
    ts.tv_sec = ms / 1000;
    ts.tv_nsec = (long)(ms % 1000) * 1000000L;
    while (nanosleep(&ts, &ts) != 0 && errno == EINTR) {
    }
}
 
static void print_activity(const struct target_watch *target, uint64_t from, uint64_t to, int after_timeout) {
    print_ts(stdout);
    printf(" %s activity detected%s (IRQ%d count %" PRIu64 " -> %" PRIu64 ")\n",
           target->name,
           after_timeout ? " after startup timeout" : "",
           target->irq,
           from,
           to);
    fflush(stdout);
}

static time_t monotonic_s(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec;
}

static void usage(const char *argv0) {
    fprintf(stderr,
            "Usage: %s [-g guest_irq] [-r rtt_irq] [-p poll_ms] [-s startup_s] [-R rearm_s] [-t timeout_s]\n"
            "Default: guest_irq=107 rtt_irq=109 poll_ms=200 startup_s=10 rearm_s=0(disable) timeout_s=0(infinite)\n"
            "Note: this tool reports IVC IRQ activity; guest payload-level readiness still needs a guest heartbeat/ready message.\n",
            argv0);
}

static int poll_target(struct target_watch *target, time_t now_s) {
    uint64_t current = 0;

    if (read_irq_count(target->irq, &current) != 0) {
        if (!target->read_error_reported) {
            print_ts(stderr);
            fprintf(stderr, " failed to read /proc/irq/%d/spurious for %s: %s\n",
                    target->irq,
                    target->name,
                    strerror(errno));
            target->read_error_reported = 1;
        }
        return -1;
    }

    if (target->read_error_reported) {
        print_ts(stderr);
        fprintf(stderr, " resumed reading /proc/irq/%d/spurious for %s\n", target->irq, target->name);
        target->read_error_reported = 0;
    }

    if (current <= target->last) {
        return 0;
    }

    if (target->state != TARGET_ACTIVE) {
        print_activity(target, target->last, current, target->state == TARGET_TIMED_OUT);
    }

    target->state = TARGET_ACTIVE;
    target->timeout_reported = 0;
    target->last_activity = now_s;
    target->last = current;
    return 1;
}

static void maybe_rearm_target(struct target_watch *target, time_t now_s, int rearm_s) {
    if (rearm_s <= 0 || target->state != TARGET_ACTIVE || target->last_activity <= 0) {
        return;
    }
    if ((now_s - target->last_activity) >= rearm_s) {
        target->state = TARGET_WAITING;
    }
}

static void maybe_report_startup_timeout(struct target_watch *target, time_t now_s, time_t start_s, int startup_s) {
    if (startup_s <= 0 || target->state != TARGET_WAITING || target->timeout_reported) {
        return;
    }
    if ((now_s - start_s) < startup_s) {
        return;
    }

    print_ts(stderr);
    fprintf(stderr, " timeout waiting %s activity (IRQ%d)\n", target->name, target->irq);
    target->state = TARGET_TIMED_OUT;
    target->timeout_reported = 1;
}
 
int main(int argc, char **argv) {
    int guest_irq = 107;
    int rtt_irq = 109;
    int poll_ms = 200;
    int startup_s = 10;
    int rearm_s = 0;
    int timeout_s = 0;
 
    int opt;
    while ((opt = getopt(argc, argv, "g:r:p:s:R:t:h")) != -1) {
        switch (opt) {
        case 'g':
            if (parse_int_arg(optarg, "guest_irq", 0, &guest_irq) != 0) {
                return 2;
            }
            break;
        case 'r':
            if (parse_int_arg(optarg, "rtt_irq", 0, &rtt_irq) != 0) {
                return 2;
            }
            break;
        case 'p':
            if (parse_int_arg(optarg, "poll_ms", 1, &poll_ms) != 0) {
                return 2;
            }
            break;
        case 's':
            if (parse_int_arg(optarg, "startup_s", 0, &startup_s) != 0) {
                return 2;
            }
            break;
        case 'R':
            if (parse_int_arg(optarg, "rearm_s", 0, &rearm_s) != 0) {
                return 2;
            }
            break;
        case 't':
            if (parse_int_arg(optarg, "timeout_s", 0, &timeout_s) != 0) {
                return 2;
            }
            break;
        case 'h':
        default:
            usage(argv[0]);
            return 2;
        }
    }
 
    struct target_watch guest = {
        .name = "Guest Linux",
        .irq = guest_irq,
    };
    struct target_watch rtt = {
        .name = "RT-Thread",
        .irq = rtt_irq,
    };

    if (read_irq_count(guest.irq, &guest.base) != 0) {
        fprintf(stderr, "failed to read /proc/irq/%d/spurious: %s\n", guest_irq, strerror(errno));
        return 1;
    }
    if (read_irq_count(rtt.irq, &rtt.base) != 0) {
        fprintf(stderr, "failed to read /proc/irq/%d/spurious: %s\n", rtt_irq, strerror(errno));
        return 1;
    }

    guest.last = guest.base;
    rtt.last = rtt.base;
 
    printf("ivc_boot_watch: baseline guest_irq=%d count=%" PRIu64 ", rtt_irq=%d count=%" PRIu64 "\n",
           guest.irq, guest.base, rtt.irq, rtt.base);
    fflush(stdout);

    struct timespec start;
    clock_gettime(CLOCK_MONOTONIC, &start);
    time_t start_s = start.tv_sec;
 
    for (;;) {
        time_t now_s = monotonic_s();

        poll_target(&guest, now_s);
        poll_target(&rtt, now_s);
 
        if (rearm_s > 0) {
            maybe_rearm_target(&guest, now_s, rearm_s);
            maybe_rearm_target(&rtt, now_s, rearm_s);
        }

        maybe_report_startup_timeout(&guest, now_s, start_s, startup_s);
        maybe_report_startup_timeout(&rtt, now_s, start_s, startup_s);

        if (timeout_s > 0) {
            struct timespec now;
            clock_gettime(CLOCK_MONOTONIC, &now);
            time_t elapsed = now.tv_sec - start.tv_sec;
            if (elapsed >= timeout_s) {
                fprintf(stderr, "timeout: guest_state=%d rtt_state=%d\n", guest.state, rtt.state);
                return 3;
            }
        }
 
        sleep_ms(poll_ms);
    }
}
