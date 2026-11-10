#pragma once

#include "storage/types.h"

#include <chrono>
#include <cstdint>
#include <filesystem>
#include <memory>
#include <string>
#include <string_view>

namespace NGlamDB::NTelemetry {

enum class EOperation : std::uint8_t { Append, CreateCollection };

// Internal process-wide accounting. All label domains and storage are fixed.
void RecordPhase(EOperation operation, glamdb::CommitPhase phase, double seconds) noexcept;
void RecordFinished(EOperation operation,
    glamdb::CommitDisposition outcome,
    glamdb::CommitPhase phase,
    std::string_view error,
    double seconds) noexcept;
void RecordIncomplete(EOperation operation, double seconds) noexcept;
void AdjustInFlight(EOperation operation, int64_t delta) noexcept;
void AdjustAdmission(int64_t pendingDelta, int64_t bytesDelta) noexcept;
std::string RenderMetrics(std::string_view serviceInstance = "core");
bool WriteMetricsTextfile(const std::filesystem::path& path, std::string_view serviceInstance = "core") noexcept;

template <class TOutcome>
class TOperationScope
{
public:
    explicit TOperationScope(EOperation operation) noexcept
        : Operation_(operation)
    {
        AdjustInFlight(Operation_, 1);
    }

    ~TOperationScope()
    {
        if (!Finished_) {
            OnPhaseEnd();
            RecordIncomplete(Operation_, GetSeconds(Start_));
        }
        AdjustInFlight(Operation_, -1);
    }

    TOperationScope(const TOperationScope&) = delete;
    TOperationScope& operator=(const TOperationScope&) = delete;

    void EnterPhase(glamdb::CommitPhase phase) noexcept
    {
        OnPhaseEnd();
        Phase_ = phase;
        PhaseStart_ = TClock::now();
    }

    TOutcome Finish(TOutcome outcome) noexcept
    {
        OnPhaseEnd();
        RecordFinished(Operation_,
            outcome.disposition,
            outcome.phase,
            outcome.error ? outcome.error->code : std::string_view{},
            GetSeconds(Start_));
        Finished_ = true;
        return outcome;
    }

private:
    using TClock = std::chrono::steady_clock;

    static double GetSeconds(TClock::time_point start) noexcept
    {
        return std::chrono::duration<double>(TClock::now() - start).count();
    }

    void OnPhaseEnd() noexcept
    {
        RecordPhase(Operation_, Phase_, GetSeconds(PhaseStart_));
    }

    EOperation Operation_;
    TClock::time_point Start_ = TClock::now();
    TClock::time_point PhaseStart_ = Start_;
    glamdb::CommitPhase Phase_ = glamdb::CommitPhase::Validation;
    bool Finished_ = false;
};

// Opt-in lifecycle owner; no formatting, filesystem access or threads in Record*.
// GLAMDB_METRICS_TEXTFILE must name an absolute .prom file. Failure is diagnostic
// only and never changes a database operation's result.
class TMetricsPublisher
{
public:
    TMetricsPublisher();
    ~TMetricsPublisher();
    TMetricsPublisher(const TMetricsPublisher&) = delete;
    TMetricsPublisher& operator=(const TMetricsPublisher&) = delete;

private:
    struct TImpl;
    std::unique_ptr<TImpl> Impl_;
};

} // namespace NGlamDB::NTelemetry
