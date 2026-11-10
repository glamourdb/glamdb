#include "core/telemetry/metrics.h"

#ifdef GLAMDB_METRICS_WITH_LOGGING
#include "core/telemetry/structured_log.h"
#endif

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <thread>
#include <vector>

namespace {
void Check(bool condition, const char* message)
{
    if (!condition) {
        throw std::runtime_error(message);
    }
}
} // namespace

int main(int argc, char** argv)
{
    using namespace NGlamDB::NTelemetry;
    using namespace glamdb;
    const auto before = RenderMetrics();
    Check(before.find("glamdb_phase_duration_seconds_count{") == std::string::npos, "unobserved phase was invented");
#ifdef GLAMDB_METRICS_WITH_LOGGING
    {
        const auto prior = GetProcessLogStats();
        auto logger = TStructuredLogger::Open();
        Check(logger.has_value(), "logger did not open");
        TLogEvent event;
        event.Event = ELogEvent::RuntimeStarted;
        Check((*logger)->TryLog(event), "important log record was not accepted");
        (*logger)->Shutdown();
        logger->reset();
        const auto stats = GetProcessLogStats();
        Check(stats.Submitted > prior.Submitted && stats.Written > prior.Written,
            "actual log counters did not advance");
        const auto snapshot = RenderMetrics();
        Check(snapshot.find("glamdb_log_written_total{service_instance=\"core\"} " + std::to_string(stats.Written)) !=
                std::string::npos,
            "actual logger counters were not exported");
    }
#endif
    {
        TOperationScope<AppendOutcome> scope(EOperation::Append);
        scope.EnterPhase(CommitPhase::Data);
        const auto outcome = scope.Finish({"sensitive-operation-id",
            CommitDisposition::CommitUnknown,
            CommitPhase::Data,
            std::nullopt,
            ErrorDetail{"unbounded-secret-error", "private-message"},
            std::nullopt});
        Check(outcome.disposition == CommitDisposition::CommitUnknown, "outcome changed");
    }
    {
        TOperationScope<CreateOutcome> scope(EOperation::CreateCollection);
    }
    std::vector<std::thread> writers;
    for (int thread = 0; thread < 8; ++thread) {
        writers.emplace_back([] {
            for (int call = 0; call < 10000; ++call) {
                RecordFinished(EOperation::Append, CommitDisposition::Committed, CommitPhase::Complete, {}, 0.01);
            }
        });
    }
    for (auto& writer : writers) {
        writer.join();
    }
    AdjustAdmission(1, 8192);
    Check(RenderMetrics().find("glamdb_admission_owned_input_bytes{service_instance=\"core\"} 8192\n") !=
            std::string::npos,
        "owned bytes missing");
    AdjustAdmission(-1, -8192);
    const auto snapshot = RenderMetrics();
    Check(
        snapshot.find(
            "glamdb_operations_total{service_instance=\"core\",operation=\"append\",outcome=\"committed\"} 80000\n") !=
            std::string::npos,
        "concurrent increments were lost");
    Check(snapshot.find("glamdb_operation_duration_seconds_bucket{service_instance=\"core\",operation=\"append\","
                        "outcome=\"committed\",le=\"0."
                        "01\"} 80000\n") != std::string::npos,
        "histogram boundary was incorrect");
    Check(snapshot.find("outcome=\"incomplete\"} 1\n") != std::string::npos, "incomplete scope was hidden");
    Check(snapshot.find("phase=\"data\",error=\"other\"} 1\n") != std::string::npos, "error label was not bounded");
    Check(snapshot.find("sensitive") == std::string::npos && snapshot.find("secret") == std::string::npos &&
            snapshot.find("private") == std::string::npos,
        "identifier or diagnostic leaked");
    Check(!WriteMetricsTextfile("relative.prom"), "relative textfile accepted");
    if (argc == 2) {
        const std::filesystem::path target = argv[1];
        Check(WriteMetricsTextfile(target), "textfile publication failed");
        Check(!std::filesystem::exists(target.string() + ".tmp"), "temporary snapshot not renamed");
        std::ifstream file(target);
        const std::string contents((std::istreambuf_iterator<char>(file)), std::istreambuf_iterator<char>());
        Check(contents.find("glamdb_metrics_snapshot_timestamp_seconds{") != std::string::npos, "snapshot age missing");
    }
    std::cout << snapshot;
}
