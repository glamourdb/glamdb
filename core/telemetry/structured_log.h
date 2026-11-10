#pragma once

#include <cstddef>
#include <cstdint>
#include <expected>
#include <memory>
#include <string>
#include <system_error>

namespace NGlamDB::NTelemetry {

enum class ELogEvent : std::uint8_t {
    RuntimeStarted,
    RuntimeStopped,
    RequestCompleted,
    StorageFailed,
    RecoveryRequired,
    LoggerHealth
};
enum class ELogOperation : std::uint8_t {
    Other,
    CreateStore,
    InspectStore,
    Health,
    Collections,
    Snapshots,
    Append,
    Scan,
    Operation,
    Auth
};
enum class ELogOutcome : std::uint8_t { Ok, Error, Unknown, Denied, Cancelled };

// The event deliberately has no free text, URL, path, identity or payload fields.
struct TLogEvent
{
    ELogEvent Event = ELogEvent::RequestCompleted;
    ELogOperation Operation = ELogOperation::Other;
    ELogOutcome Outcome = ELogOutcome::Ok;
    std::uint64_t RequestID = 0;
    std::uint64_t DurationUs = 0;
    std::uint64_t RowCount = 0;
    std::uint32_t StatusCode = 0;
};

struct TLogOptions
{
    // Empty selects a duplicate of stderr. Files must be outside the warehouse.
    std::string Directory;
    std::size_t QueueCapacity = 8192;
    std::size_t ImportantReserve = 64;
    std::uint64_t SuccessSampleEvery = 100;
    std::uint32_t MaxSuccessPerSecond = 20;
    std::size_t MaxFileBytes = 16ULL * 1024 * 1024;
    std::size_t FileCount = 4;
};

struct TLogStats
{
    std::uint64_t Submitted = 0;
    std::uint64_t Accepted = 0;
    std::uint64_t Sampled = 0;
    std::uint64_t RateLimited = 0;
    std::uint64_t DroppedFull = 0;
    std::uint64_t DroppedContended = 0;
    std::uint64_t DroppedStopped = 0;
    std::uint64_t DroppedSink = 0;
    std::uint64_t Written = 0;
    std::uint64_t SinkErrors = 0;
    std::uint64_t Backlog = 0;
    std::size_t QueueCapacity = 0;
};

// Cumulative process counters survive logger destruction; gauges describe live loggers.
TLogStats GetProcessLogStats() noexcept;

class TStructuredLogger
{
public:
    static std::expected<std::unique_ptr<TStructuredLogger>, std::error_code> Open(const TLogOptions& options = {});
    ~TStructuredLogger();
    TStructuredLogger(const TStructuredLogger&) = delete;
    TStructuredLogger& operator=(const TStructuredLogger&) = delete;

    // No allocations, waiting for a mutex, file IO or network IO on this path.
    bool TryLog(const TLogEvent& event) noexcept;
    std::uint64_t NewRequestID() noexcept;
    TLogStats GetStats() const noexcept;
    // Stop producers before destruction. Drains the queue; OS sink IO can delay shutdown.
    void Shutdown() noexcept;

private:
    struct TImpl;
    explicit TStructuredLogger(std::unique_ptr<TImpl> impl);
    std::unique_ptr<TImpl> Impl_;
};

} // namespace NGlamDB::NTelemetry
