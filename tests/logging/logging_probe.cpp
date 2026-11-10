#include "core/telemetry/structured_log.h"

#include <chrono>
#include <cstdlib>
#include <iostream>
#include <string>
#include <sys/stat.h>
#include <thread>
#include <unistd.h>
#include <vector>

using namespace NGlamDB::NTelemetry;

int main(int argc, char** argv)
try {
    if (argc != 4) {
        return 2;
    }
    const std::string mode = argv[1];
    TLogOptions options;
    options.Directory = mode == "blocked" ? "" : argv[2];
    options.QueueCapacity = (mode == "stress" || mode == "blocked") ? 32 : 8192;
    options.ImportantReserve = 8;
    options.SuccessSampleEvery = mode == "sample" ? 10 : mode == "default" ? 100 : 1;
    options.MaxSuccessPerSecond = mode == "rate" ? 2 : mode == "default" ? 20 : 1000000;
    options.MaxFileBytes = (mode == "rotate" || mode == "sink-error") ? 4096 : 16ULL * 1024 * 1024;
    options.FileCount = 3;
    auto opened = TStructuredLogger::Open(options);
    if (!opened) {
        std::cerr << "logger_open_failed\n";
        return 3;
    }
    auto& logger = **opened;
    if (mode == "hold") {
        std::cout << "ready\n" << std::flush;
        std::this_thread::sleep_for(std::chrono::seconds(10));
        return 0;
    }
    if (mode == "sink-error") {
        ::chmod(options.Directory.c_str(), 0500);
    }
    const auto before = std::chrono::steady_clock::now();
    const auto logLifecycle = [&logger, &mode](ELogEvent event) {
        if (mode == "stress" || mode == "blocked") {
            logger.TryLog({.Event = event});
            return;
        }
        while (!logger.TryLog({.Event = event})) {
            std::this_thread::yield();
        }
    };
    logLifecycle(ELogEvent::RuntimeStarted);
    const auto count = std::stoull(argv[3]);
    const auto produce = [&logger, count, &mode]() {
        for (std::uint64_t index = 0; index < count; ++index) {
            const auto error = mode == "errors" || mode == "stress" || mode == "blocked" || mode == "sink-error";
            const TLogEvent event{.Event = ELogEvent::RequestCompleted,
                .Operation = ELogOperation::Append,
                .Outcome = error ? ELogOutcome::Error : ELogOutcome::Ok,
                .RequestID = logger.NewRequestID(),
                .DurationUs = 123,
                .RowCount = 7,
                .StatusCode = error ? 503U : 200U};
            if (mode == "errors") {
                while (!logger.TryLog(event)) {
                    std::this_thread::yield();
                }
            } else {
                logger.TryLog(event);
            }
        }
    };
    if (mode == "stress") {
        std::vector<std::thread> threads;
        for (unsigned index = 0; index < 8; ++index) {
            threads.emplace_back(produce);
        }
        for (auto& thread : threads) {
            thread.join();
        }
    } else {
        produce();
    }
    logLifecycle(ELogEvent::RuntimeStopped);
    if (mode != "blocked") {
        logger.Shutdown();
    }
    if (mode == "sink-error") {
        ::chmod(options.Directory.c_str(), 0700);
    }
    const auto stats = logger.GetStats();
    if (mode != "blocked") {
        opened->reset();
    }
    const auto processStats = GetProcessLogStats();
    const auto elapsed =
        std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::steady_clock::now() - before).count();
    std::cout << "{\"submitted\":" << stats.Submitted << ",\"accepted\":" << stats.Accepted
              << ",\"sampled\":" << stats.Sampled << ",\"rate_limited\":" << stats.RateLimited
              << ",\"dropped_full\":" << stats.DroppedFull << ",\"dropped_contended\":" << stats.DroppedContended
              << ",\"dropped_sink\":" << stats.DroppedSink << ",\"written\":" << stats.Written
              << ",\"sink_errors\":" << stats.SinkErrors << ",\"backlog\":" << stats.Backlog
              << ",\"process_submitted\":" << processStats.Submitted << ",\"process_written\":" << processStats.Written
              << ",\"process_capacity\":" << processStats.QueueCapacity << ",\"elapsed_us\":" << elapsed << "}\n"
              << std::flush;
    if (mode == "blocked") {
        ::_exit(0);
    }
} catch (...) {
    return 100;
}
