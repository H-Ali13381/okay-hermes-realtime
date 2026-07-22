#define _GNU_SOURCE
#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include <pipewire/pipewire.h>

#include <spa/param/audio/format-utils.h>

#include "onnxruntime_c_api.h"

#define MODEL_RATE 16000U
#define MODEL_SECONDS 3U
#define MODEL_SAMPLES (MODEL_RATE * MODEL_SECONDS)
#define RING_SECONDS 4U

#define DEFAULT_THRESHOLD 0.6973556280136108f
#define DEFAULT_CONSECUTIVE_WINDOWS 2U
#define DEFAULT_INFERENCE_INTERVAL_MS 250U

#define CAPTURE_STATUS_DIR "okay-hermes-realtime"
#define CAPTURE_STATUS_NAME "capture.status"

#define CAPTURE_HEALTH_TIMEOUT_SECONDS 10.0
#define WORKER_MIN_SLEEP_SECONDS 0.001
#define WORKER_MAX_SLEEP_SECONDS 0.5

struct sample_ring
{
    float *samples;
    size_t capacity;
    _Atomic size_t write_index;
    _Atomic uint64_t total_written;
};

struct listener_options
{
    const char *model_path;
    const char *capture_health_path;
    char capture_health_buffer[4096];

    const char **handler_argv;
    size_t handler_argc;

    float threshold;
    unsigned int consecutive_windows;
    unsigned int inference_interval_ms;
    bool self_test;
};

struct wake_model
{
    const OrtApi *ort;
    OrtEnv *env;
    OrtSessionOptions *session_options;
    OrtSession *session;
    OrtMemoryInfo *memory_info;
    OrtAllocator *allocator;
    char *input_name;
    char *output_name;
};

struct listener_data
{
    struct pw_main_loop *loop;
    struct pw_stream *stream;

    struct sample_ring ring;
    struct wake_model model;
    struct listener_options options;

    struct spa_audio_info format;
    _Atomic unsigned int negotiated_rate;
    _Atomic unsigned int negotiated_channels;
    unsigned int resample_accumulator;

    pthread_t worker_thread;
    _Atomic bool running;

    char capture_health_path[4096];
};

static struct listener_data *g_listener;

static double monotonic_seconds(void)
{
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0)
        return 0.0;
    return (double)ts.tv_sec + ((double)ts.tv_nsec / 1e9);
}

static ssize_t write_all(int fd, const char *buffer, size_t length)
{
    size_t written = 0;

    while (written < length) {
        ssize_t result = write(fd, buffer + written, length - written);
        if (result < 0) {
            if (errno == EINTR)
                continue;
            return -1;
        }
        written += (size_t)result;
    }

    return (ssize_t)written;
}

static int build_activation_payload(char *out,
                                  size_t out_size,
                                  float probability,
                                  const char *type,
                                  double detected_at,
                                  bool include_native_flag)
{
    if (out == NULL || out_size == 0)
        return -1;

    if (!isfinite(probability) || probability < 0.0f || probability > 1.0f)
        return -1;

    if (!isfinite(detected_at))
        return -1;

    if (include_native_flag)
        return snprintf(out,
                        out_size,
                        "{\"type\":\"%s\",\"probability\":%.9f,\"detected_at\":%.9f,\"native_listener\":true}\n",
                        type,
                        (double)probability,
                        detected_at);

    return snprintf(out,
                    out_size,
                    "{\"type\":\"%s\",\"probability\":%.9f,\"detected_at\":%.9f}\n",
                    type,
                    (double)probability,
                    detected_at);
}

static int ensure_parent_dir(const char *path)
{
    char directory[4096];
    size_t len;

    if (path == NULL || path[0] == '\0')
        return -1;

    if (snprintf(directory, sizeof(directory), "%s", path) < 0)
        return -1;

    char *slash = strrchr(directory, '/');
    if (slash == NULL)
        return 0;

    *slash = '\0';
    if (directory[0] == '\0')
        return 0;

    len = strlen(directory);
    for (size_t i = 1; i < len; i++) {
        if (directory[i] != '/')
            continue;

        directory[i] = '\0';
        if (mkdir(directory, 0755) < 0 && errno != EEXIST)
            return -1;
        directory[i] = '/';
    }

    if (mkdir(directory, 0755) < 0 && errno != EEXIST)
        return -1;

    return 0;
}

static void write_capture_status(const struct listener_data *data, const char *status)
{
    char tmp_path[8192];
    int fd;
    FILE *fp;
    int rc;

    if (data == NULL || status == NULL || data->capture_health_path[0] == '\0')
        return;

    if (ensure_parent_dir(data->capture_health_path) < 0)
        return;

    rc = snprintf(tmp_path, sizeof(tmp_path), "%s.tmp.%ld", data->capture_health_path, (long)getpid());
    if (rc < 0 || (size_t)rc >= sizeof(tmp_path))
        return;

    fd = open(tmp_path, O_CREAT | O_TRUNC | O_WRONLY, 0600);
    if (fd < 0)
        return;

    fp = fdopen(fd, "w");
    if (fp == NULL) {
        close(fd);
        unlink(tmp_path);
        return;
    }

    if (fprintf(fp, "%s\n", status) < 0 || fflush(fp) != 0 || fsync(fd) != 0) {
        fclose(fp);
        unlink(tmp_path);
        return;
    }

    if (fclose(fp) != 0) {
        unlink(tmp_path);
        return;
    }

    (void)rename(tmp_path, data->capture_health_path);
}

static void clear_capture_status(const struct listener_data *data)
{
    if (data != NULL && data->capture_health_path[0] != '\0')
        unlink(data->capture_health_path);
}

static int ring_init(struct sample_ring *ring, size_t capacity)
{
    if (ring == NULL || capacity == 0)
        return -1;

    ring->samples = calloc(capacity, sizeof(float));
    if (ring->samples == NULL)
        return -1;

    ring->capacity = capacity;
    atomic_store(&ring->write_index, 0);
    atomic_store(&ring->total_written, 0);
    return 0;
}

static void ring_destroy(struct sample_ring *ring)
{
    if (ring == NULL)
        return;

    free(ring->samples);
    ring->samples = NULL;
    ring->capacity = 0;
}

static void ring_write(struct sample_ring *ring, const float *samples, size_t count)
{
    if (ring == NULL || ring->samples == NULL || samples == NULL || count == 0)
        return;

    if (ring->capacity == 0)
        return;

    size_t write_index = atomic_load(&ring->write_index);
    for (size_t i = 0; i < count; i++) {
        ring->samples[write_index] = samples[i];
        write_index = (write_index + 1U) % ring->capacity;
    }

    atomic_store(&ring->write_index, write_index);
    atomic_fetch_add(&ring->total_written, count);
}

static size_t ring_snapshot_latest(struct sample_ring *ring, float *out, size_t out_capacity)
{
    size_t available;
    size_t read_index;

    if (ring == NULL || ring->samples == NULL || out == NULL || out_capacity == 0)
        return 0;

    available = atomic_load(&ring->total_written);
    if (available < out_capacity)
        return 0;

    available = out_capacity;
    read_index = (atomic_load(&ring->write_index) + ring->capacity - available) % ring->capacity;

    for (size_t i = 0; i < available; i++) {
        out[i] = ring->samples[(read_index + i) % ring->capacity];
    }

    return available;
}

static void listener_options_destroy(struct listener_options *options)
{
    if (options == NULL)
        return;

    free((void *)options->handler_argv);
    options->handler_argv = NULL;
    options->handler_argc = 0;
    options->model_path = NULL;
    options->capture_health_path = NULL;
    options->capture_health_buffer[0] = '\0';
}

static int ort_status_check(struct wake_model *model, OrtStatus *status, const char *context)
{
    if (status == NULL)
        return 0;

    if (model != NULL && model->ort != NULL && model->ort->GetErrorMessage != NULL) {
        fprintf(stderr, "%s: %s\n", context, model->ort->GetErrorMessage(status));
        if (model->ort->ReleaseStatus != NULL)
            model->ort->ReleaseStatus(status);
    }
    return -1;
}

static int wake_model_init(struct wake_model *model, const char *model_path)
{
    const OrtApiBase *api_base = OrtGetApiBase();
    if (api_base == NULL) {
        fprintf(stderr, "missing OrtGetApiBase\n");
        return -1;
    }

    memset(model, 0, sizeof(*model));
    model->ort = api_base->GetApi(ORT_API_VERSION);
    if (model->ort == NULL) {
        fprintf(stderr, "missing OrtApi\n");
        return -1;
    }

    if (ort_status_check(model,
                        model->ort->CreateEnv(ORT_LOGGING_LEVEL_WARNING,
                                             "okay-hermes-realtime-wake-listener",
                                             &model->env),
                        "CreateEnv") < 0)
        return -1;

    if (ort_status_check(model,
                        model->ort->CreateSessionOptions(&model->session_options),
                        "CreateSessionOptions") < 0)
        return -1;

    if (ort_status_check(model,
                        model->ort->SetIntraOpNumThreads(model->session_options, 1),
                        "SetIntraOpNumThreads") < 0)
        return -1;

    if (ort_status_check(model,
                        model->ort->SetInterOpNumThreads(model->session_options, 1),
                        "SetInterOpNumThreads") < 0)
        return -1;

    if (ort_status_check(model,
                        model->ort->SetSessionExecutionMode(model->session_options, ORT_SEQUENTIAL),
                        "SetSessionExecutionMode") < 0)
        return -1;

    if (ort_status_check(model,
                        model->ort->DisableCpuMemArena(model->session_options),
                        "DisableCpuMemArena") < 0)
        return -1;

    if (ort_status_check(model,
                        model->ort->DisableMemPattern(model->session_options),
                        "DisableMemPattern") < 0)
        return -1;

    if (ort_status_check(model,
                        model->ort->CreateSession(model->env, model_path, model->session_options, &model->session),
                        "CreateSession") < 0)
        return -1;

    if (ort_status_check(model,
                        model->ort->GetAllocatorWithDefaultOptions(&model->allocator),
                        "GetAllocatorWithDefaultOptions") < 0)
        return -1;

    if (ort_status_check(model,
                        model->ort->SessionGetInputName(model->session, 0, model->allocator, &model->input_name),
                        "SessionGetInputName") < 0)
        return -1;

    if (ort_status_check(model,
                        model->ort->SessionGetOutputName(model->session, 0, model->allocator, &model->output_name),
                        "SessionGetOutputName") < 0)
        return -1;

    if (ort_status_check(model,
                        model->ort->CreateCpuMemoryInfo(OrtDeviceAllocator, OrtMemTypeDefault, &model->memory_info),
                        "CreateCpuMemoryInfo") < 0)
        return -1;

    return 0;
}

static void wake_model_destroy(struct wake_model *model)
{
    if (model == NULL || model->ort == NULL)
        return;

    if (model->session != NULL && model->ort->ReleaseSession != NULL)
        model->ort->ReleaseSession(model->session);
    if (model->memory_info != NULL && model->ort->ReleaseMemoryInfo != NULL)
        model->ort->ReleaseMemoryInfo(model->memory_info);
    if (model->session_options != NULL && model->ort->ReleaseSessionOptions != NULL)
        model->ort->ReleaseSessionOptions(model->session_options);
    if (model->output_name != NULL && model->ort->AllocatorFree != NULL) {
        OrtStatus *status = model->ort->AllocatorFree(model->allocator, model->output_name);
        if (status != NULL && model->ort->ReleaseStatus != NULL)
            model->ort->ReleaseStatus(status);
    }
    if (model->input_name != NULL && model->ort->AllocatorFree != NULL) {
        OrtStatus *status = model->ort->AllocatorFree(model->allocator, model->input_name);
        if (status != NULL && model->ort->ReleaseStatus != NULL)
            model->ort->ReleaseStatus(status);
    }
    if (model->env != NULL && model->ort->ReleaseEnv != NULL)
        model->ort->ReleaseEnv(model->env);

    memset(model, 0, sizeof(*model));
}

static int run_model(struct wake_model *model, const float *input, float *score)
{
    int64_t shape[2] = {1, MODEL_SAMPLES};
    const char *input_names[1] = {model->input_name};
    const char *output_names[1] = {model->output_name};
    OrtValue *input_tensor = NULL;
    OrtValue *output_tensor = NULL;
    const OrtValue *input_tensors_const[1];
    float *output_data = NULL;

    if (model == NULL || score == NULL)
        return -1;

    if (model->input_name == NULL || model->output_name == NULL || model->session == NULL)
        return -1;

    if (model->memory_info == NULL || input == NULL)
        return -1;

    if (model->ort == NULL)
        return -1;

    if (model->ort->CreateTensorWithDataAsOrtValue(model->memory_info,
                                                  (void *)input,
                                                  (size_t)MODEL_SAMPLES * sizeof(float),
                                                  shape,
                                                  2,
                                                  ONNX_TENSOR_ELEMENT_DATA_TYPE_FLOAT,
                                                  &input_tensor) != NULL) {
        return -1;
    }

    input_tensors_const[0] = input_tensor;

    if (ort_status_check(model,
                        model->ort->Run(model->session,
                                       NULL,
                                       input_names,
                                       input_tensors_const,
                                       1,
                                       output_names,
                                       1,
                                       &output_tensor),
                        "OrtRun") < 0) {
        model->ort->ReleaseValue(input_tensor);
        return -1;
    }

    model->ort->ReleaseValue(input_tensor);

    if (ort_status_check(model,
                        model->ort->GetTensorMutableData(output_tensor, (void **)&output_data),
                        "GetTensorMutableData") < 0) {
        model->ort->ReleaseValue(output_tensor);
        return -1;
    }

    if (output_data != NULL)
        *score = output_data[0];
    else
        *score = 0.0f;

    model->ort->ReleaseValue(output_tensor);
    return 0;
}

static int write_activation(struct listener_data *data, float probability)
{
    char payload[256];
    int len;

    (void)data;

    len = build_activation_payload(payload,
                                 sizeof(payload),
                                 probability,
                                 "activation",
                                 monotonic_seconds(),
                                 true);
    if (len < 0 || (size_t)len >= sizeof(payload))
        return -1;

    return write_all(STDOUT_FILENO, payload, (size_t)len) < 0 ? -1 : 0;
}

static int run_handler(const struct listener_data *data, float probability)
{
    int pipefd[2] = {-1, -1};
    pid_t child_pid;
    int status = 0;
    char payload[256];
    int payload_len;

    if (data == NULL || data->options.handler_argc == 0 || data->options.handler_argv == NULL)
        return -1;

    payload_len = build_activation_payload(payload,
                                         sizeof(payload),
                                         probability,
                                         "activation",
                                         monotonic_seconds(),
                                         true);
    if (payload_len < 0 || (size_t)payload_len >= sizeof(payload))
        return -1;

    if (pipe2(pipefd, O_CLOEXEC) < 0)
        return -1;

    child_pid = fork();
    if (child_pid == -1) {
        close(pipefd[0]);
        close(pipefd[1]);
        return -1;
    }

    if (child_pid == 0) {
        if (dup2(pipefd[0], STDIN_FILENO) == -1)
            _exit(127);

        close(pipefd[0]);
        close(pipefd[1]);

        execv(data->options.handler_argv[0], (char *const *)data->options.handler_argv);
        _exit(127);
    }

    close(pipefd[0]);

    if (write_all(pipefd[1], payload, (size_t)payload_len) < 0) {
        close(pipefd[1]);
        (void)waitpid(child_pid, NULL, 0);
        return -1;
    }

    close(pipefd[1]);

    if (waitpid(child_pid, &status, 0) == -1)
        return -1;
    if (!WIFEXITED(status) || WEXITSTATUS(status) != 0)
        return -1;

    return 0;
}

static void mix_and_resample_downmix(const float *samples,
                                  size_t frames,
                                  unsigned int channels,
                                  unsigned int rate,
                                  struct sample_ring *ring,
                                  unsigned int *accumulator)
{
    if (samples == NULL || ring == NULL || accumulator == NULL)
        return;

    if (channels == 0)
        channels = 1;
    if (rate == 0)
        rate = MODEL_RATE;

    for (size_t frame = 0; frame < frames; frame++) {
        float mono = 0.0f;
        for (unsigned int ch = 0; ch < channels; ch++)
            mono += samples[(frame * channels) + ch];
        mono = mono / (float)channels;

        *accumulator += MODEL_RATE;
        while (*accumulator >= rate) {
            ring_write(ring, &mono, 1);
            *accumulator -= rate;
        }
    }
}

static void sleep_worker_until_next_deadline(double now,
                                            double inference_interval,
                                            double last_capture_sample_at,
                                            bool wrote_unhealthy)
{
    double next = now + WORKER_MAX_SLEEP_SECONDS;

    double next_inference = now + inference_interval;
    if (next_inference < next)
        next = next_inference;

    if (!wrote_unhealthy) {
        double next_health = last_capture_sample_at + CAPTURE_HEALTH_TIMEOUT_SECONDS;
        if (next_health < next)
            next = next_health;
    }

    double delay = next - now;
    if (delay < WORKER_MIN_SLEEP_SECONDS)
        delay = WORKER_MIN_SLEEP_SECONDS;
    if (delay > WORKER_MAX_SLEEP_SECONDS)
        delay = WORKER_MAX_SLEEP_SECONDS;

    struct timespec sleep_time;
    sleep_time.tv_sec = (time_t)delay;
    sleep_time.tv_nsec = (long)((delay - (double)sleep_time.tv_sec) * 1e9);
    (void)nanosleep(&sleep_time, NULL);
}

static void *listener_worker(void *arg)
{
    struct listener_data *data = arg;
    float *model_window = NULL;
    uint64_t last_health_total = 0;
    unsigned int consecutive_hits = 0;
    double last_inference_at = 0.0;
    double last_capture_sample_at = 0.0;
    bool wrote_unhealthy = false;

    model_window = malloc((size_t)MODEL_SAMPLES * sizeof(float));
    if (model_window == NULL) {
        atomic_store(&data->running, false);
        if (data->loop != NULL)
            pw_main_loop_quit(data->loop);
        return NULL;
    }

    if (wake_model_init(&data->model, data->options.model_path) < 0) {
        write_capture_status(data, "handler_failed");
        free(model_window);
        atomic_store(&data->running, false);
        if (data->loop != NULL)
            pw_main_loop_quit(data->loop);
        return NULL;
    }

    write_capture_status(data, "starting");
    last_capture_sample_at = monotonic_seconds();

    while (atomic_load(&data->running)) {
        uint64_t total = atomic_load_explicit(&data->ring.total_written, memory_order_acquire);
        double now = monotonic_seconds();
        double interval = (double)data->options.inference_interval_ms / 1000.0;
        float probability = 0.0f;

        if (total != last_health_total) {
            write_capture_status(data, "healthy");
            last_health_total = total;
            wrote_unhealthy = false;
            last_capture_sample_at = now;
        } else if (!wrote_unhealthy && now - last_capture_sample_at >= CAPTURE_HEALTH_TIMEOUT_SECONDS) {
            write_capture_status(data, "unhealthy");
            wrote_unhealthy = true;
        }

        if (total >= MODEL_SAMPLES && (last_inference_at == 0.0 || now - last_inference_at >= interval)) {
            if (ring_snapshot_latest(&data->ring, model_window, MODEL_SAMPLES) == MODEL_SAMPLES) {
                if (run_model(&data->model, model_window, &probability) == 0) {
                    if (probability >= data->options.threshold)
                        consecutive_hits++;
                    else
                        consecutive_hits = 0;

                    if (consecutive_hits >= data->options.consecutive_windows) {
                        if (data->options.handler_argc == 0) {
                            if (write_activation(data, probability) != 0)
                                write_capture_status(data, "handler_failed");
                        } else if (run_handler(data, probability) != 0) {
                            write_capture_status(data, "handler_failed");
                        }
                        consecutive_hits = 0;
                    }
                }
            }
            last_inference_at = now;
        }

        sleep_worker_until_next_deadline(now, interval, last_capture_sample_at, wrote_unhealthy);
    }

    free(model_window);
    wake_model_destroy(&data->model);
    write_capture_status(data, "stopped");
    return NULL;
}

static void on_process(void *userdata)
{
    struct listener_data *data = userdata;
    struct pw_buffer *buffer = NULL;
    struct spa_buffer *spa_buffer = NULL;
    struct spa_data *spa_data = NULL;
    struct spa_chunk *chunk = NULL;
    const float *samples;
    size_t frames;
    unsigned int channels;
    unsigned int rate;

    if (data == NULL)
        return;

    buffer = pw_stream_dequeue_buffer(data->stream);
    if (buffer == NULL)
        return;

    spa_buffer = buffer->buffer;
    if (spa_buffer == NULL) {
        pw_stream_queue_buffer(data->stream, buffer);
        return;
    }

    spa_data = &spa_buffer->datas[0];
    if (spa_data == NULL || spa_data->data == NULL || spa_data->chunk == NULL) {
        pw_stream_queue_buffer(data->stream, buffer);
        return;
    }

    chunk = spa_data->chunk;
    if (chunk->size == 0) {
        pw_stream_queue_buffer(data->stream, buffer);
        return;
    }

    if (data->format.info.raw.format != SPA_AUDIO_FORMAT_F32) {
        pw_stream_queue_buffer(data->stream, buffer);
        return;
    }

    samples = (const float *)((const uint8_t *)spa_data->data + chunk->offset);
    channels = atomic_load_explicit(&data->negotiated_channels, memory_order_acquire);
    rate = atomic_load_explicit(&data->negotiated_rate, memory_order_acquire);

    frames = chunk->size / (sizeof(float) * (size_t)channels);

    if (channels == 0)
        channels = 1;
    if (rate == 0)
        rate = MODEL_RATE;

    for (size_t frame = 0; frame < frames; frame++) {
        float mono = 0.0f;

        for (unsigned int ch = 0; ch < channels; ch++)
            mono += samples[(frame * channels) + ch];

        mono /= (float)channels;

        data->resample_accumulator += MODEL_RATE;
        while (data->resample_accumulator >= rate) {
            ring_write(&data->ring, &mono, 1);
            data->resample_accumulator -= rate;
        }
    }

    pw_stream_queue_buffer(data->stream, buffer);
}

static void on_stream_param_changed(void *userdata, uint32_t id, const struct spa_pod *param)
{
    struct listener_data *data = userdata;

    if (data == NULL || param == NULL || id != SPA_PARAM_Format)
        return;

    if (spa_format_parse(param, &data->format.media_type, &data->format.media_subtype) < 0)
        return;

    if (data->format.media_type != SPA_MEDIA_TYPE_audio || data->format.media_subtype != SPA_MEDIA_SUBTYPE_raw)
        return;

    if (spa_format_audio_raw_parse(param, &data->format.info.raw) < 0)
        return;

    data->resample_accumulator = 0;
    atomic_store(&data->negotiated_rate, data->format.info.raw.rate);
    atomic_store(&data->negotiated_channels, data->format.info.raw.channels);
}

static void on_state_changed(void *userdata, enum pw_stream_state old, enum pw_stream_state state, const char *error)
{
    struct listener_data *data = userdata;

    (void)old;
    (void)error;

    if (state == PW_STREAM_STATE_ERROR || state == PW_STREAM_STATE_UNCONNECTED) {
        atomic_store(&data->running, false);
        pw_main_loop_quit(data->loop);
    }
}

static const struct pw_stream_events stream_events = {
    .version = PW_VERSION_STREAM_EVENTS,
    .param_changed = on_stream_param_changed,
    .state_changed = on_state_changed,
    .process = on_process,
};

static void signal_request_stop(int signo)
{
    (void)signo;
    if (g_listener != NULL) {
        atomic_store(&g_listener->running, false);
        if (g_listener->loop != NULL)
            pw_main_loop_quit(g_listener->loop);
    }
}

static int install_signal_handlers(void)
{
    struct sigaction action;

    memset(&action, 0, sizeof(action));
    action.sa_handler = signal_request_stop;
    if (sigaction(SIGINT, &action, NULL) < 0)
        return -1;
    if (sigaction(SIGTERM, &action, NULL) < 0)
        return -1;

    return 0;
}

static int parse_float(const char *value, float *out)
{
    char *end = NULL;
    float parsed;

    errno = 0;
    parsed = strtof(value, &end);
    if (errno != 0 || end == value || *end != '\0' || !isfinite(parsed) || parsed < 0.0f || parsed > 1.0f) {
        return -1;
    }

    *out = parsed;
    return 0;
}

static int parse_unsigned(const char *value, unsigned int *out)
{
    char *end = NULL;
    unsigned long parsed;

    errno = 0;
    parsed = strtoul(value, &end, 10);
    if (errno != 0 || end == value || *end != '\0' || parsed == 0UL)
        return -1;

    if (parsed > UINT32_MAX)
        return -1;

    *out = (unsigned int)parsed;
    return 0;
}

static int validate_existing_file(const char *path)
{
    struct stat path_info;

    if (path == NULL || path[0] == '\0')
        return -1;

    if (access(path, R_OK) != 0)
        return -1;

    if (stat(path, &path_info) != 0)
        return -1;

    if (!S_ISREG(path_info.st_mode))
        return -1;

    return 0;
}

static int parse_options(int argc, char *argv[], struct listener_options *options)
{
    memset(options, 0, sizeof(*options));
    options->threshold = DEFAULT_THRESHOLD;
    options->consecutive_windows = DEFAULT_CONSECUTIVE_WINDOWS;
    options->inference_interval_ms = DEFAULT_INFERENCE_INTERVAL_MS;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--help") == 0) {
            fprintf(stdout,
                    "Usage: okay-hermes-realtime-wake-listener [options]\n\n"
                    "  --model PATH               Path to ONNX wakeword model\n"
                    "  --handler [--] PATH [ARGS..] External activation handler invocation\n"
                    "  --capture-health PATH      Output path for capture status\n"
                    "  --threshold FLOAT          Activation threshold in [0,1] (default %.7f)\n"
                    "  --consecutive-windows N    Consecutive activations required (default %u) [or --consecutive]\n"
                    "  --inference-interval N     Inference interval in milliseconds (default %u) [or --inference-interval-ms]\n"
                    "  --self-test                Emit deterministic self-test event and exit\n"
                    "  --help                    Show this message\n",
                    DEFAULT_THRESHOLD,
                    DEFAULT_CONSECUTIVE_WINDOWS,
                    DEFAULT_INFERENCE_INTERVAL_MS);
            return 1;
        }

        if (strcmp(argv[i], "--self-test") == 0) {
            options->self_test = true;
            continue;
        }

        if (strcmp(argv[i], "--model") == 0) {
            if (i + 1 >= argc) {
                fprintf(stderr, "--model requires a value\n");
                return -1;
            }
            options->model_path = argv[++i];
            continue;
        }

        if (strcmp(argv[i], "--capture-health") == 0) {
            if (i + 1 >= argc) {
                fprintf(stderr, "--capture-health requires a value\n");
                return -1;
            }
            options->capture_health_path = argv[++i];
            continue;
        }

        if (strcmp(argv[i], "--handler") == 0) {
            if (i + 1 >= argc) {
                fprintf(stderr, "--handler requires at least a command\n");
                return -1;
            }

            if (strcmp(argv[i + 1], "--") == 0) {
                options->handler_argv = calloc((size_t)(argc - (i + 1)), sizeof(char *));
                if (options->handler_argv == NULL) {
                    fprintf(stderr, "failed to allocate handler argv\n");
                    return -1;
                }

                options->handler_argc = 0;
                for (size_t j = (size_t)(i + 2); j < (size_t)argc; j++) {
                    options->handler_argv[options->handler_argc++] = argv[j];
                }
                options->handler_argv[options->handler_argc] = NULL;
                i = argc;
                continue;
            }

            options->handler_argv = calloc(2, sizeof(char *));
            if (options->handler_argv == NULL) {
                fprintf(stderr, "failed to allocate handler argv\n");
                return -1;
            }
            options->handler_argv[0] = argv[++i];
            options->handler_argv[1] = NULL;
            options->handler_argc = 1;
            continue;
        }

        if (strcmp(argv[i], "--threshold") == 0) {
            float parsed;
            if (i + 1 >= argc || parse_float(argv[++i], &parsed) < 0) {
                fprintf(stderr, "invalid --threshold value\n");
                return -1;
            }
            options->threshold = parsed;
            continue;
        }

        if (strcmp(argv[i], "--consecutive") == 0 || strcmp(argv[i], "--consecutive-windows") == 0) {
            unsigned int parsed;
            if (i + 1 >= argc || parse_unsigned(argv[++i], &parsed) < 0) {
                fprintf(stderr, "invalid --consecutive-windows value\n");
                return -1;
            }
            options->consecutive_windows = parsed;
            continue;
        }

        if (strcmp(argv[i], "--inference-interval") == 0 ||
            strcmp(argv[i], "--inference-interval-ms") == 0) {
            unsigned int parsed;
            if (i + 1 >= argc || parse_unsigned(argv[++i], &parsed) < 0) {
                fprintf(stderr, "invalid --inference-interval value\n");
                return -1;
            }
            options->inference_interval_ms = parsed;
            continue;
        }

        fprintf(stderr, "unknown option: %s\n", argv[i]);
        return -1;
    }

    if (!options->self_test) {
        if (options->model_path == NULL || options->model_path[0] == '\0' || validate_existing_file(options->model_path) < 0) {
            fprintf(stderr, "--model is required and must be readable\n");
            return -1;
        }

        if (options->capture_health_path == NULL || options->capture_health_path[0] == '\0') {
            const char *home = getenv("HOME");
            if (home == NULL || home[0] == '\0') {
                fprintf(stderr, "HOME is required for default capture status path\n");
                return -1;
            }

            if (snprintf(options->capture_health_buffer,
                         sizeof(options->capture_health_buffer),
                         "%s/.local/state/%s/%s",
                         home,
                         CAPTURE_STATUS_DIR,
                         CAPTURE_STATUS_NAME) >= (int)sizeof(options->capture_health_buffer)) {
                fprintf(stderr, "capture status path too long\n");
                return -1;
            }
            options->capture_health_path = options->capture_health_buffer;
        }
    }

    return 0;
}

static struct pw_properties *stream_properties(void)
{
    return pw_properties_new(PW_KEY_MEDIA_TYPE,
                            "Audio",
                            PW_KEY_MEDIA_CATEGORY,
                            "Capture",
                            PW_KEY_MEDIA_ROLE,
                            "Communication",
                            PW_KEY_NODE_NAME,
                            "okay-hermes-realtime",
                            PW_KEY_NODE_DESCRIPTION,
                            "Okay Hermes Realtime wake listener",
                            NULL);
}

static void self_test_output(void)
{
    struct sample_ring ring = {0};
    const float deterministic_frames[] = {
        0.0f, 0.0f,
        0.5f, -0.5f,
        1.0f, 1.0f,
        -0.5f, 0.5f,
        0.25f, 0.75f,
        -1.0f, -1.0f,
        0.1f, 0.2f,
        -0.1f, -0.2f,
    };
    unsigned int accumulator = 0;
    float sample;
    float latest[16];
    char payload[256];
    int len;

    if (ring_init(&ring, (size_t)MODEL_RATE * MODEL_SECONDS) < 0) {
        fprintf(stderr, "self-test ring init failure\n");
        return;
    }

    mix_and_resample_downmix(deterministic_frames,
                            sizeof(deterministic_frames) / (sizeof(deterministic_frames[0]) * 2U),
                            2U,
                            MODEL_RATE / 2U,
                            &ring,
                            &accumulator);

    if (ring_snapshot_latest(&ring, latest, 8) < 8) {
        ring_destroy(&ring);
        fprintf(stderr, "self-test insufficient frames\n");
        return;
    }

    sample = (latest[0] + latest[1] + latest[2] + latest[3] + latest[4] + latest[5] + latest[6] + latest[7]) /
             8.0f;

    len = snprintf(payload,
                   sizeof(payload),
                   "{\"event\":\"self_test\",\"type\":\"self_test\",\"status\":\"ok\",\"score\":%.9f}\n",
                   (double)fabs(sample));

    ring_destroy(&ring);

    if (len < 0 || (size_t)len >= sizeof(payload))
        return;

    (void)write_all(STDOUT_FILENO, payload, (size_t)len);
}

int main(int argc, char *argv[])
{
    int status = 0;
    struct listener_data data = {0};
    struct spa_pod *params[1];
    uint8_t format_buffer[1024];
    struct pw_properties *props = NULL;
    struct spa_pod_builder builder = SPA_POD_BUILDER_INIT(format_buffer, sizeof(format_buffer));

    int parse_rc = parse_options(argc, argv, &data.options);
    if (parse_rc != 0) {
        listener_options_destroy(&data.options);
        return parse_rc > 0 ? 0 : 2;
    }

    if (data.options.self_test) {
        self_test_output();
        listener_options_destroy(&data.options);
        return 0;
    }

    if (snprintf(data.capture_health_path,
                 sizeof(data.capture_health_path),
                 "%s",
                 data.options.capture_health_path) < 0) {
        listener_options_destroy(&data.options);
        return 1;
    }

    pw_init(&argc, &argv);
    g_listener = &data;

    if (install_signal_handlers() < 0) {
        status = 1;
        goto cleanup;
    }

    atomic_store(&data.running, true);
    atomic_store(&data.negotiated_rate, MODEL_RATE);
    atomic_store(&data.negotiated_channels, 1);

    data.loop = pw_main_loop_new(NULL);
    if (data.loop == NULL) {
        status = 1;
        goto cleanup;
    }

    if (ring_init(&data.ring, (size_t)MODEL_RATE * RING_SECONDS) < 0) {
        status = 1;
        goto cleanup_loop;
    }

    props = stream_properties();
    if (props == NULL) {
        status = 1;
        goto cleanup_ring;
    }

    data.stream = pw_stream_new_simple(pw_main_loop_get_loop(data.loop),
                                      "okay-hermes-realtime",
                                      props,
                                      &stream_events,
                                      &data);
    if (data.stream == NULL) {
        status = 1;
        goto cleanup_ring;
    }

    params[0] = spa_format_audio_raw_build(&builder,
                                          SPA_PARAM_EnumFormat,
                                          &SPA_AUDIO_INFO_RAW_INIT(.format = SPA_AUDIO_FORMAT_F32));
    if (pw_stream_connect(data.stream,
                          PW_DIRECTION_INPUT,
                          PW_ID_ANY,
                          PW_STREAM_FLAG_AUTOCONNECT | PW_STREAM_FLAG_MAP_BUFFERS | PW_STREAM_FLAG_RT_PROCESS,
                          (const struct spa_pod **)params,
                          1) < 0) {
        status = 1;
        goto cleanup_stream;
    }

    if (pthread_create(&data.worker_thread, NULL, listener_worker, &data) != 0) {
        status = 1;
        goto cleanup_stream;
    }

    pw_main_loop_run(data.loop);

    atomic_store(&data.running, false);
    pthread_join(data.worker_thread, NULL);

cleanup_stream:
    if (data.stream != NULL)
        pw_stream_destroy(data.stream);

cleanup_ring:
    ring_destroy(&data.ring);

cleanup_loop:
    if (data.loop != NULL)
        pw_main_loop_destroy(data.loop);

cleanup:
    clear_capture_status(&data);
    wake_model_destroy(&data.model);
    listener_options_destroy(&data.options);
    pw_deinit();

    return status;
}
