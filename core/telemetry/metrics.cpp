#include "core/telemetry/metrics.h"

#ifdef GLAMDB_METRICS_WITH_LOGGING
#include "core/telemetry/structured_log.h"
#endif

#include <algorithm>
#include <array>
#include <atomic>
#include <condition_variable>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <locale>
#include <mutex>
#include <sstream>
#include <stdexcept>
#include <thread>
#include <utility>

namespace NGlamDB::NTelemetry {
namespace {

static_assert(std::atomic<uint64_t>::is_always_lock_free);
static_assert(std::atomic<int64_t>::is_always_lock_free);

constexpr std::array<std::string_view, 2> OperationNames{"append", "create_collection"};
constexpr std::array<std::string_view, 4> OutcomeNames{"committed",
    "definitely_not_committed",
    "commit_unknown",
    "incomplete"};
constexpr std::array<std::string_view, 9> PhaseNames{"validation",
    "binding",
    "data",
    "metadata",
    "catalog_transaction",
    "catalog_commit",
    "recording_failure",
    "reconciling",
    "complete"};
constexpr std::array<std::string_view, 13> ErrorNames{"INVALID_ARGUMENT",
    "RESOURCE_EXHAUSTED",
    "DEADLINE_EXCEEDED",
    "CANCELLED",
    "IO_ERROR",
    "CORRUPTION",
    "RECOVERY_REQUIRED",
    "OPERATION_CONFLICT",
    "PRECONDITION_FAILED",
    "DEPENDENCY_UNAVAILABLE",
    "SCHEMA_MISMATCH",
    "INTERNAL",
    "other"};
constexpr std::array<double, 15>
    Bounds{0.0001, 0.0005, 0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 15, 60};

struct THistogram
{
    std::array<std::atomic<uint64_t>, Bounds.size() + 1> Buckets{};
    std::atomic<uint64_t> SumUs{};

    void Observe(double seconds) noexcept
    {
        const auto bucket =
            static_cast<size_t>(std::lower_bound(Bounds.begin(), Bounds.end(), seconds) - Bounds.begin());
        Buckets[bucket].fetch_add(1, std::memory_order_relaxed);
        SumUs.fetch_add(static_cast<uint64_t>(std::max(0.0, seconds) * 1'000'000), std::memory_order_relaxed);
    }
};

struct TRegistry
{
    std::array<std::array<std::atomic<uint64_t>, 4>, 2> Outcomes{};
    std::array<std::array<THistogram, 4>, 2> Durations{};
    std::array<std::array<THistogram, PhaseNames.size()>, 2> Phases{};
    std::array<std::array<std::array<std::atomic<uint64_t>, ErrorNames.size()>, PhaseNames.size()>, 2> Errors{};
    std::array<std::atomic<int64_t>, 2> InFlight{};
    std::atomic<int64_t> Pending{};
    std::atomic<int64_t> OwnedBytes{};
};

TRegistry Registry;

std::string GetLabel(std::string_view name, std::string_view value)
{
    return std::string(name) + "=\"" + std::string(value) + "\"";
}

void RenderHistogram(std::ostream& out, std::string_view name, const std::string& labels, const THistogram& histogram)
{
    uint64_t cumulative = 0;
    for (size_t i = 0; i <= Bounds.size(); ++i) {
        cumulative += histogram.Buckets[i].load(std::memory_order_relaxed);
        out << name << "_bucket{" << labels << ",le=\"";
        if (i < Bounds.size()) {
            out << Bounds[i];
        } else {
            out << "+Inf";
        }
        out << "\"} " << cumulative << '\n';
    }
    out << name << "_count{" << labels << "} " << cumulative << '\n';
    out << name << "_sum{" << labels << "} "
        << static_cast<double>(histogram.SumUs.load(std::memory_order_relaxed)) / 1'000'000.0 << '\n';
}

bool HasSamples(const THistogram& histogram)
{
    return std::ranges::any_of(histogram.Buckets, [](const auto& bucket) {
        return bucket.load(std::memory_order_relaxed) != 0;
    });
}

bool IsValidInstance(std::string_view value)
{
    return !value.empty() && value.size() <= 64 && std::ranges::all_of(value, [](char character) {
        return (character >= 'a' && character <= 'z') || (character >= 'A' && character <= 'Z') ||
            (character >= '0' && character <= '9') || character == '_' || character == '-' || character == '.';
    });
}

std::string AddInstanceLabel(const std::string& metrics, std::string_view instance)
{
    if (!IsValidInstance(instance)) {
        throw std::invalid_argument("invalid metrics service instance");
    }
    std::istringstream source(metrics);
    std::ostringstream result;
    std::string line;
    while (std::getline(source, line)) {
        if (!line.empty() && line[0] != '#') {
            const auto space = line.find(' ');
            const auto brace = line.find('{');
            if (brace < space) {
                line.insert(brace + 1, GetLabel("service_instance", instance) + ',');
            } else {
                line.insert(space, '{' + GetLabel("service_instance", instance) + '}');
            }
        }
        result << line << '\n';
    }
    return result.str();
}

#ifdef GLAMDB_METRICS_WITH_LOGGING
void RenderLogMetrics(std::ostream& out)
{
    const auto stats = GetProcessLogStats();
    const std::array<std::pair<std::string_view, uint64_t>, 7> counters{{{"submitted", stats.Submitted},
        {"accepted", stats.Accepted},
        {"sampled", stats.Sampled},
        {"rate_limited", stats.RateLimited},
        {"written", stats.Written},
        {"sink_errors", stats.SinkErrors},
        {"dropped", 0}}};
    for (const auto& [name, value] : counters) {
        out << "# HELP glamdb_log_" << name << "_total Process structured logging counter.\n"
            << "# TYPE glamdb_log_" << name << "_total counter\n";
        if (name != "dropped") {
            out << "glamdb_log_" << name << "_total " << value << '\n';
        }
    }
    for (const auto& [reason, value] :
        std::array<std::pair<std::string_view, uint64_t>, 4>{{{"queue_full", stats.DroppedFull},
            {"contended", stats.DroppedContended},
            {"stopped", stats.DroppedStopped},
            {"sink", stats.DroppedSink}}})
    {
        out << "glamdb_log_dropped_total{reason=\"" << reason << "\"} " << value << '\n';
    }
    out << "# HELP glamdb_log_backlog Accepted log records pending emission.\n"
           "# TYPE glamdb_log_backlog gauge\n"
        << "glamdb_log_backlog " << stats.Backlog << '\n'
        << "# HELP glamdb_log_queue_capacity Capacity of live process log queues.\n"
           "# TYPE glamdb_log_queue_capacity gauge\n"
        << "glamdb_log_queue_capacity " << stats.QueueCapacity << '\n';
}
#endif

} // namespace

void RecordPhase(EOperation operation, glamdb::CommitPhase phase, double seconds) noexcept
{
    const auto operationIndex = static_cast<size_t>(operation);
    const auto phaseIndex = static_cast<size_t>(phase);
    if (operationIndex < OperationNames.size() && phaseIndex < PhaseNames.size()) {
        Registry.Phases[operationIndex][phaseIndex].Observe(seconds);
    }
}

void RecordFinished(EOperation operation,
    glamdb::CommitDisposition outcome,
    glamdb::CommitPhase phase,
    std::string_view error,
    double seconds) noexcept
{
    const auto operationIndex = static_cast<size_t>(operation);
    const auto outcomeIndex = static_cast<size_t>(outcome);
    const auto phaseIndex = static_cast<size_t>(phase);
    if (operationIndex >= OperationNames.size() || outcomeIndex >= 3 || phaseIndex >= PhaseNames.size()) {
        return;
    }
    Registry.Outcomes[operationIndex][outcomeIndex].fetch_add(1, std::memory_order_relaxed);
    Registry.Durations[operationIndex][outcomeIndex].Observe(seconds);
    if (!error.empty()) {
        const auto errorIt = std::ranges::find(ErrorNames, error);
        const auto errorIndex =
            errorIt == ErrorNames.end() ? ErrorNames.size() - 1 : static_cast<size_t>(errorIt - ErrorNames.begin());
        Registry.Errors[operationIndex][phaseIndex][errorIndex].fetch_add(1, std::memory_order_relaxed);
    }
}

void RecordIncomplete(EOperation operation, double seconds) noexcept
{
    const auto operationIndex = static_cast<size_t>(operation);
    if (operationIndex < OperationNames.size()) {
        Registry.Outcomes[operationIndex][3].fetch_add(1, std::memory_order_relaxed);
        Registry.Durations[operationIndex][3].Observe(seconds);
    }
}

void AdjustInFlight(EOperation operation, int64_t delta) noexcept
{
    const auto operationIndex = static_cast<size_t>(operation);
    if (operationIndex < OperationNames.size()) {
        Registry.InFlight[operationIndex].fetch_add(delta, std::memory_order_relaxed);
    }
}

void AdjustAdmission(int64_t pendingDelta, int64_t bytesDelta) noexcept
{
    Registry.Pending.fetch_add(pendingDelta, std::memory_order_relaxed);
    Registry.OwnedBytes.fetch_add(bytesDelta, std::memory_order_relaxed);
}

std::string RenderMetrics(std::string_view serviceInstance)
{
    std::ostringstream out;
    out.imbue(std::locale::classic());
    out.precision(12);
    out << "# HELP glamdb_operations_total Completed API calls including retries; not unique durable writes.\n"
           "# TYPE glamdb_operations_total counter\n"
           "# HELP glamdb_operations_in_flight Active synchronous API calls.\n"
           "# TYPE glamdb_operations_in_flight gauge\n"
           "# HELP glamdb_operation_duration_seconds End-to-end API duration including admission and retries.\n"
           "# TYPE glamdb_operation_duration_seconds histogram\n"
           "# HELP glamdb_phase_duration_seconds Observed coordinator regions; data includes backend metadata work.\n"
           "# TYPE glamdb_phase_duration_seconds histogram\n"
           "# HELP glamdb_operation_errors_total Returned errors mapped to a fixed domain.\n"
           "# TYPE glamdb_operation_errors_total counter\n";
    for (size_t operation = 0; operation < OperationNames.size(); ++operation) {
        const auto operationLabel = GetLabel("operation", OperationNames[operation]);
        out << "glamdb_operations_in_flight{" << operationLabel << "} "
            << Registry.InFlight[operation].load(std::memory_order_relaxed) << '\n';
        for (size_t outcome = 0; outcome < OutcomeNames.size(); ++outcome) {
            const auto labels = operationLabel + ',' + GetLabel("outcome", OutcomeNames[outcome]);
            out << "glamdb_operations_total{" << labels << "} "
                << Registry.Outcomes[operation][outcome].load(std::memory_order_relaxed) << '\n';
            RenderHistogram(out, "glamdb_operation_duration_seconds", labels, Registry.Durations[operation][outcome]);
        }
        for (size_t phase = 0; phase < PhaseNames.size(); ++phase) {
            const auto labels = operationLabel + ',' + GetLabel("phase", PhaseNames[phase]);
            if (HasSamples(Registry.Phases[operation][phase])) {
                RenderHistogram(out, "glamdb_phase_duration_seconds", labels, Registry.Phases[operation][phase]);
            }
            for (size_t error = 0; error < ErrorNames.size(); ++error) {
                const auto count = Registry.Errors[operation][phase][error].load(std::memory_order_relaxed);
                if (count != 0) {
                    out << "glamdb_operation_errors_total{" << labels << ',' << GetLabel("error", ErrorNames[error])
                        << "} " << count << '\n';
                }
            }
        }
    }
    out << "# HELP glamdb_admission_pending_calls Admitted append calls retaining an admission slot.\n"
           "# TYPE glamdb_admission_pending_calls gauge\n"
        << "glamdb_admission_pending_calls " << Registry.Pending.load(std::memory_order_relaxed) << '\n'
        << "# HELP glamdb_admission_owned_input_bytes Append input bytes owned by admission scopes.\n"
           "# TYPE glamdb_admission_owned_input_bytes gauge\n"
        << "glamdb_admission_owned_input_bytes " << Registry.OwnedBytes.load(std::memory_order_relaxed) << '\n'
        << "# HELP glamdb_metrics_snapshot_timestamp_seconds Unix time when this process rendered this snapshot.\n"
           "# TYPE glamdb_metrics_snapshot_timestamp_seconds gauge\n"
        << "glamdb_metrics_snapshot_timestamp_seconds "
        << std::chrono::duration<double>(std::chrono::system_clock::now().time_since_epoch()).count() << '\n';
#ifdef GLAMDB_METRICS_WITH_LOGGING
    RenderLogMetrics(out);
#endif
    return AddInstanceLabel(out.str(), serviceInstance);
}

bool WriteMetricsTextfile(const std::filesystem::path& path, std::string_view serviceInstance) noexcept
{
    try {
        if (!path.is_absolute() || path.extension() != ".prom") {
            return false;
        }
        auto temporary = path;
        temporary += ".tmp";
        {
            std::ofstream file(temporary, std::ios::trunc | std::ios::binary);
            file << RenderMetrics(serviceInstance);
            file.close();
            if (!file) {
                return false;
            }
        }
        std::filesystem::rename(temporary, path);
        return true;
    } catch (...) {
        return false;
    }
}

struct TMetricsPublisher::TImpl
{
    std::filesystem::path Path;
    std::string ServiceInstance = "core";
    std::mutex Mutex;
    std::condition_variable Changed;
    bool Stopped = false;
    std::thread Thread;

    void Publish() const
    {
        if (!WriteMetricsTextfile(Path, ServiceInstance)) {
            std::cerr << "metrics textfile publication failed\n";
        }
    }
};

TMetricsPublisher::TMetricsPublisher()
{
    const auto* path = std::getenv("GLAMDB_METRICS_TEXTFILE");
    if (!path || *path == '\0') {
        return;
    }
    try {
        const std::filesystem::path target(path);
        if (!target.is_absolute() || target.extension() != ".prom") {
            std::cerr << "GLAMDB_METRICS_TEXTFILE requires an absolute .prom path\n";
            return;
        }
        auto impl = std::make_unique<TImpl>();
        impl->Path = target;
        const auto* instance = std::getenv("GLAMDB_METRICS_INSTANCE");
        if (instance && *instance != '\0') {
            if (!IsValidInstance(instance)) {
                std::cerr << "GLAMDB_METRICS_INSTANCE requires 1..64 letters, digits, dots, underscores or dashes\n";
                return;
            }
            impl->ServiceInstance = instance;
        }
        impl->Publish();
        impl->Thread = std::thread([state = impl.get()] {
            std::unique_lock lock(state->Mutex);
            while (!state->Changed.wait_for(lock, std::chrono::seconds(15), [&] {
                return state->Stopped;
            }))
            {
                lock.unlock();
                state->Publish();
                lock.lock();
            }
        });
        Impl_ = std::move(impl);
    } catch (...) {
        std::cerr << "metrics publisher could not start\n";
    }
}

TMetricsPublisher::~TMetricsPublisher()
{
    if (Impl_) {
        {
            std::lock_guard lock(Impl_->Mutex);
            Impl_->Stopped = true;
        }
        Impl_->Changed.notify_one();
        Impl_->Thread.join();
        Impl_->Publish();
    }
}

} // namespace NGlamDB::NTelemetry
