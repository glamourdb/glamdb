#include "structured_log.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <ctime>
#include <fcntl.h>
#include <mutex>
#include <pthread.h>
#include <random>
#include <signal.h>
#include <string_view>
#include <sys/file.h>
#include <sys/stat.h>
#include <thread>
#include <unistd.h>
#include <vector>

namespace NGlamDB::NTelemetry {
namespace {

static_assert(std::atomic<std::uint64_t>::is_always_lock_free);

struct TProcessCounters
{
    std::atomic<std::uint64_t> Submitted = 0;
    std::atomic<std::uint64_t> Accepted = 0;
    std::atomic<std::uint64_t> Sampled = 0;
    std::atomic<std::uint64_t> RateLimited = 0;
    std::atomic<std::uint64_t> DroppedFull = 0;
    std::atomic<std::uint64_t> DroppedContended = 0;
    std::atomic<std::uint64_t> DroppedStopped = 0;
    std::atomic<std::uint64_t> DroppedSink = 0;
    std::atomic<std::uint64_t> Written = 0;
    std::atomic<std::uint64_t> SinkErrors = 0;
    std::atomic<std::uint64_t> Backlog = 0;
    std::atomic<std::size_t> QueueCapacity = 0;
};

TProcessCounters& GetProcessCounters() noexcept
{
    static TProcessCounters counters;
    return counters;
}

class TCounter
{
public:
    explicit TCounter(std::atomic<std::uint64_t>& aggregate)
        : Aggregate_(aggregate)
    { }

    void Add() noexcept
    {
        Value_.fetch_add(1, std::memory_order_relaxed);
        Aggregate_.fetch_add(1, std::memory_order_relaxed);
    }

    void Subtract() noexcept
    {
        Value_.fetch_sub(1, std::memory_order_relaxed);
        Aggregate_.fetch_sub(1, std::memory_order_relaxed);
    }

    std::uint64_t Get() const noexcept
    {
        return Value_.load(std::memory_order_relaxed);
    }

private:
    std::atomic<std::uint64_t> Value_ = 0;
    std::atomic<std::uint64_t>& Aggregate_;
};

constexpr std::size_t BatchSize = 64;
constexpr auto HealthInterval = std::chrono::seconds(5);

std::uint64_t NowUs() noexcept
{
    return static_cast<std::uint64_t>(
        std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::system_clock::now().time_since_epoch())
            .count());
}

std::uint64_t SteadySeconds() noexcept
{
    return static_cast<std::uint64_t>(
        std::chrono::duration_cast<std::chrono::seconds>(std::chrono::steady_clock::now().time_since_epoch()).count());
}

const char* GetEventName(ELogEvent value) noexcept
{
    switch (value) {
        case ELogEvent::RuntimeStarted:
            return "runtime_started";
        case ELogEvent::RuntimeStopped:
            return "runtime_stopped";
        case ELogEvent::RequestCompleted:
            return "request_completed";
        case ELogEvent::StorageFailed:
            return "storage_failed";
        case ELogEvent::RecoveryRequired:
            return "recovery_required";
        case ELogEvent::LoggerHealth:
            return "logger_health";
    }
    return "request_completed";
}

const char* GetOperationName(ELogOperation value) noexcept
{
    switch (value) {
        case ELogOperation::CreateStore:
            return "create_store";
        case ELogOperation::InspectStore:
            return "inspect_store";
        case ELogOperation::Health:
            return "health";
        case ELogOperation::Collections:
            return "collections";
        case ELogOperation::Snapshots:
            return "snapshots";
        case ELogOperation::Append:
            return "append";
        case ELogOperation::Scan:
            return "scan";
        case ELogOperation::Operation:
            return "operation";
        case ELogOperation::Auth:
            return "auth";
        case ELogOperation::Other:
            return "other";
    }
    return "other";
}

const char* GetOutcomeName(ELogOutcome value) noexcept
{
    switch (value) {
        case ELogOutcome::Ok:
            return "ok";
        case ELogOutcome::Error:
            return "error";
        case ELogOutcome::Unknown:
            return "unknown";
        case ELogOutcome::Denied:
            return "denied";
        case ELogOutcome::Cancelled:
            return "cancelled";
    }
    return "error";
}

const char* GetLevel(const TLogEvent& event) noexcept
{
    if (event.Event == ELogEvent::StorageFailed || event.Event == ELogEvent::RecoveryRequired ||
        event.Outcome == ELogOutcome::Error || event.Outcome == ELogOutcome::Unknown)
    {
        return "error";
    }
    return event.Outcome == ELogOutcome::Ok ? "info" : "warn";
}

struct TRecord
{
    TLogEvent Event;
    std::uint64_t TimestampUs = 0;
};

void FormatName(char* target, std::size_t size, std::size_t index) noexcept
{
    if (index == 0) {
        std::snprintf(target, size, "glamdb.log");
    } else {
        std::snprintf(target, size, "glamdb.log.%zu", index);
    }
}

class TLogSink
{
public:
    ~TLogSink()
    {
        if (FileFD_ >= 0) {
            ::close(FileFD_);
        }
        if (DirectoryFD_ >= 0) {
            ::close(DirectoryFD_);
        }
    }

    bool Open(const TLogOptions& options)
    {
        Options_ = options;
        if (options.Directory.empty()) {
            FileFD_ = ::fcntl(STDERR_FILENO, F_DUPFD_CLOEXEC, 0);
            return FileFD_ >= 0;
        }
        if (::mkdir(options.Directory.c_str(), 0700) != 0 && errno != EEXIST) {
            return false;
        }
        DirectoryFD_ = ::open(options.Directory.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
        struct stat info
        { };
        if (DirectoryFD_ < 0 || ::fstat(DirectoryFD_, &info) != 0) {
            return false;
        }
        if (info.st_uid != ::geteuid() || (info.st_mode & 0022) != 0) {
            errno = EACCES;
            return false;
        }
        return ::flock(DirectoryFD_, LOCK_EX | LOCK_NB) == 0 && CheckArchives() && OpenActive();
    }

    bool Write(std::string_view line) noexcept
    {
        if (DirectoryFD_ >= 0 && (FileFD_ < 0 || CurrentSize_ + line.size() > Options_.MaxFileBytes)) {
            if (FileFD_ < 0 ? !OpenActive() : !Rotate()) {
                return false;
            }
            if (CurrentSize_ + line.size() > Options_.MaxFileBytes && !Rotate()) {
                return false;
            }
        }
        std::size_t offset = 0;
        while (offset < line.size()) {
            const auto written = ::write(FileFD_, line.data() + offset, line.size() - offset);
            if (written < 0 && errno == EINTR) {
                continue;
            }
            if (written <= 0) {
                if (offset > 0 && DirectoryFD_ >= 0) {
                    const auto ignored = ::ftruncate(FileFD_, static_cast<off_t>(CurrentSize_));
                    (void)ignored;
                }
                return false;
            }
            offset += static_cast<std::size_t>(written);
        }
        CurrentSize_ += line.size();
        return true;
    }

private:
    bool CheckArchives() noexcept
    {
        for (std::size_t index = 1; index < 16; ++index) {
            std::array<char, 32> name{};
            FormatName(name.data(), name.size(), index);
            const auto fd = ::openat(DirectoryFD_, name.data(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
            if (fd < 0) {
                if (errno == ENOENT) {
                    continue;
                }
                return false;
            }
            const auto valid = CheckFile(fd);
            struct stat info
            { };
            const auto checked = ::fstat(fd, &info) == 0;
            ::close(fd);
            if (!valid || !checked) {
                return false;
            }
            if ((index >= Options_.FileCount || static_cast<std::size_t>(info.st_size) > Options_.MaxFileBytes) &&
                ::unlinkat(DirectoryFD_, name.data(), 0) != 0)
            {
                return false;
            }
        }
        return true;
    }

    static bool CheckFile(int fd) noexcept
    {
        struct stat info
        { };
        if (::fstat(fd, &info) != 0) {
            return false;
        }
        if (!S_ISREG(info.st_mode) || info.st_uid != ::geteuid() || info.st_nlink != 1) {
            errno = EACCES;
            return false;
        }
        return ::fchmod(fd, 0600) == 0;
    }

    bool OpenActive() noexcept
    {
        FileFD_ = ::openat(DirectoryFD_,
            "glamdb.log",
            O_WRONLY | O_APPEND | O_CREAT | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK,
            0600);
        if (FileFD_ < 0 || !CheckFile(FileFD_)) {
            return false;
        }
        struct stat info
        { };
        if (::fstat(FileFD_, &info) != 0) {
            return false;
        }
        CurrentSize_ = static_cast<std::size_t>(info.st_size);
        if (CurrentSize_ > Options_.MaxFileBytes) {
            if (::ftruncate(FileFD_, 0) != 0) {
                return false;
            }
            CurrentSize_ = 0;
        }
        return true;
    }

    bool Rotate() noexcept
    {
        ::close(FileFD_);
        FileFD_ = -1;
        if (Options_.FileCount == 1) {
            if (::unlinkat(DirectoryFD_, "glamdb.log", 0) != 0 && errno != ENOENT) {
                return false;
            }
        }
        for (auto index = Options_.FileCount - 1; index > 0; --index) {
            std::array<char, 32> source{};
            std::array<char, 32> destination{};
            FormatName(source.data(), source.size(), index - 1);
            FormatName(destination.data(), destination.size(), index);
            if (::renameat(DirectoryFD_, source.data(), DirectoryFD_, destination.data()) != 0 && errno != ENOENT) {
                return false;
            }
        }
        return OpenActive();
    }

    TLogOptions Options_;
    int DirectoryFD_ = -1;
    int FileFD_ = -1;
    std::size_t CurrentSize_ = 0;
};

std::size_t FormatRecord(std::array<char, 2048>& buffer,
    const TRecord& record,
    std::uint64_t processID,
    const TLogStats* stats) noexcept
{
    const auto seconds = static_cast<std::time_t>(record.TimestampUs / 1000000);
    std::tm utc{};
    ::gmtime_r(&seconds, &utc);
    std::array<char, 48> timestamp{};
    std::snprintf(timestamp.data(),
        timestamp.size(),
        "%04d-%02d-%02dT%02d:%02d:%02d.%06lluZ",
        utc.tm_year + 1900,
        utc.tm_mon + 1,
        utc.tm_mday,
        utc.tm_hour,
        utc.tm_min,
        utc.tm_sec,
        static_cast<unsigned long long>(record.TimestampUs % 1000000));
    const auto& event = record.Event;
    auto length = std::snprintf(buffer.data(),
        buffer.size(),
        "{\"schema\":1,\"timestamp\":\"%s\",\"service\":\"glamdb\",\"level\":\"%s\",\"event\":\"%s\","
        "\"operation\":\"%s\",\"outcome\":\"%s\",\"request_id\":\"%016llx%016llx\","
        "\"duration_us\":%llu,\"row_count\":%llu,\"status_code\":%u",
        timestamp.data(),
        GetLevel(event),
        GetEventName(event.Event),
        GetOperationName(event.Operation),
        GetOutcomeName(event.Outcome),
        static_cast<unsigned long long>(processID),
        static_cast<unsigned long long>(event.RequestID),
        static_cast<unsigned long long>(event.DurationUs),
        static_cast<unsigned long long>(event.RowCount),
        event.StatusCode);
    if (stats != nullptr && length > 0 && static_cast<std::size_t>(length) < buffer.size()) {
        length += std::snprintf(buffer.data() + length,
            buffer.size() - static_cast<std::size_t>(length),
            ",\"submitted\":%llu,\"accepted\":%llu,\"sampled\":%llu,\"rate_limited\":%llu,"
            "\"dropped_full\":%llu,\"dropped_contended\":%llu,\"dropped_stopped\":%llu,"
            "\"dropped_sink\":%llu,\"written\":%llu,\"sink_errors\":%llu,\"backlog\":%llu,\"queue_capacity\":%zu",
            static_cast<unsigned long long>(stats->Submitted),
            static_cast<unsigned long long>(stats->Accepted),
            static_cast<unsigned long long>(stats->Sampled),
            static_cast<unsigned long long>(stats->RateLimited),
            static_cast<unsigned long long>(stats->DroppedFull),
            static_cast<unsigned long long>(stats->DroppedContended),
            static_cast<unsigned long long>(stats->DroppedStopped),
            static_cast<unsigned long long>(stats->DroppedSink),
            static_cast<unsigned long long>(stats->Written),
            static_cast<unsigned long long>(stats->SinkErrors),
            static_cast<unsigned long long>(stats->Backlog),
            stats->QueueCapacity);
    }
    if (length <= 0 || static_cast<std::size_t>(length) + 2 > buffer.size()) {
        return 0;
    }
    buffer[static_cast<std::size_t>(length)] = '}';
    buffer[static_cast<std::size_t>(length) + 1] = '\n';
    return static_cast<std::size_t>(length) + 2;
}

} // namespace

TLogStats GetProcessLogStats() noexcept
{
    const auto& counters = GetProcessCounters();
    return {.Submitted = counters.Submitted.load(std::memory_order_relaxed),
        .Accepted = counters.Accepted.load(std::memory_order_relaxed),
        .Sampled = counters.Sampled.load(std::memory_order_relaxed),
        .RateLimited = counters.RateLimited.load(std::memory_order_relaxed),
        .DroppedFull = counters.DroppedFull.load(std::memory_order_relaxed),
        .DroppedContended = counters.DroppedContended.load(std::memory_order_relaxed),
        .DroppedStopped = counters.DroppedStopped.load(std::memory_order_relaxed),
        .DroppedSink = counters.DroppedSink.load(std::memory_order_relaxed),
        .Written = counters.Written.load(std::memory_order_relaxed),
        .SinkErrors = counters.SinkErrors.load(std::memory_order_relaxed),
        .Backlog = counters.Backlog.load(std::memory_order_relaxed),
        .QueueCapacity = counters.QueueCapacity.load(std::memory_order_relaxed)};
}

struct TStructuredLogger::TImpl
{
    TImpl(const TLogOptions& options, std::unique_ptr<TLogSink> sink)
        : Options_(options)
        , Sink_(std::move(sink))
        , Queue_(options.QueueCapacity)
    {
        std::random_device random;
        ProcessID_ = (static_cast<std::uint64_t>(random()) << 32U) ^ random();
        GetProcessCounters().QueueCapacity.fetch_add(Options_.QueueCapacity);
    }

    ~TImpl()
    {
        Shutdown();
        GetProcessCounters().QueueCapacity.fetch_sub(Options_.QueueCapacity);
    }

    void Shutdown() noexcept
    {
        {
            const std::lock_guard lock(Mutex_);
            Stopped_.store(true);
        }
        Ready_.notify_one();
        if (Worker_.joinable()) {
            Worker_.join();
        }
    }

    TLogStats GetStats() const noexcept
    {
        return {.Submitted = Submitted_.Get(),
            .Accepted = Accepted_.Get(),
            .Sampled = Sampled_.Get(),
            .RateLimited = RateLimited_.Get(),
            .DroppedFull = DroppedFull_.Get(),
            .DroppedContended = DroppedContended_.Get(),
            .DroppedStopped = DroppedStopped_.Get(),
            .DroppedSink = DroppedSink_.Get(),
            .Written = Written_.Get(),
            .SinkErrors = SinkErrors_.Get(),
            .Backlog = Backlog_.Get(),
            .QueueCapacity = Options_.QueueCapacity};
    }

    bool AdmitSuccess() noexcept
    {
        const auto second = SteadySeconds();
        const auto current = (second & 0xffffffffU) << 32U;
        auto previous = SuccessBucket_.load();
        const auto count = (previous & 0xffffffff00000000ULL) == current ? previous & 0xffffffffU : 0;
        return count < Options_.MaxSuccessPerSecond &&
            SuccessBucket_.compare_exchange_strong(previous, current | (count + 1));
    }

    bool TryLog(const TLogEvent& event) noexcept
    {
        Submitted_.Add();
        if (Stopped_.load()) {
            DroppedStopped_.Add();
            return false;
        }
        const auto success = event.Event == ELogEvent::RequestCompleted && event.Outcome == ELogOutcome::Ok;
        if (success && SuccessSequence_.fetch_add(1) % Options_.SuccessSampleEvery != 0) {
            Sampled_.Add();
            return false;
        }
        if (success && !AdmitSuccess()) {
            RateLimited_.Add();
            return false;
        }
        std::unique_lock lock(Mutex_, std::try_to_lock);
        if (!lock.owns_lock()) {
            DroppedContended_.Add();
            return false;
        }
        if (Stopped_.load()) {
            DroppedStopped_.Add();
            return false;
        }
        const auto limit = Options_.QueueCapacity - (success ? Options_.ImportantReserve : 0);
        if (Count_ >= limit) {
            DroppedFull_.Add();
            return false;
        }
        Queue_[(Head_ + Count_) % Queue_.size()] = {.Event = event, .TimestampUs = NowUs()};
        ++Count_;
        Accepted_.Add();
        Backlog_.Add();
        lock.unlock();
        Ready_.notify_one();
        return true;
    }

    void WriteHealth() noexcept
    {
        const auto stats = GetStats();
        const TRecord health{.Event = {.Event = ELogEvent::LoggerHealth}, .TimestampUs = NowUs()};
        std::array<char, 2048> buffer{};
        const auto size = FormatRecord(buffer, health, ProcessID_, &stats);
        if (!Sink_->Write(std::string_view(buffer.data(), size))) {
            SinkErrors_.Add();
        }
    }

    void Run() noexcept
    {
        // A closed stderr pipe must become a counted sink error, never SIGPIPE.
        sigset_t blocked;
        sigemptyset(&blocked);
        sigaddset(&blocked, SIGPIPE);
        ::pthread_sigmask(SIG_BLOCK, &blocked, nullptr);
        auto nextHealth = std::chrono::steady_clock::now() + HealthInterval;
        for (;;) {
            std::array<TRecord, BatchSize> batch{};
            std::size_t count = 0;
            {
                std::unique_lock lock(Mutex_);
                Ready_.wait_until(lock, nextHealth, [this]() {
                    return Count_ != 0 || Stopped_.load();
                });
                count = std::min(Count_, batch.size());
                for (std::size_t index = 0; index < count; ++index) {
                    batch[index] = Queue_[(Head_ + index) % Queue_.size()];
                }
                Head_ = (Head_ + count) % Queue_.size();
                Count_ -= count;
                if (count == 0 && Stopped_.load()) {
                    break;
                }
            }
            bool failed = false;
            for (std::size_t index = 0; index < count; ++index) {
                std::array<char, 2048> buffer{};
                const auto size = FormatRecord(buffer, batch[index], ProcessID_, nullptr);
                if (size > 0 && Sink_->Write(std::string_view(buffer.data(), size))) {
                    Written_.Add();
                } else {
                    SinkErrors_.Add();
                    DroppedSink_.Add();
                    failed = true;
                }
                Backlog_.Subtract();
            }
            if (std::chrono::steady_clock::now() >= nextHealth) {
                WriteHealth();
                nextHealth = std::chrono::steady_clock::now() + HealthInterval;
            }
            if (failed) {
                std::unique_lock lock(Mutex_);
                Ready_.wait_for(lock, std::chrono::milliseconds(100), [this]() {
                    return Stopped_.load();
                });
            }
        }
        WriteHealth();
    }

    TLogOptions Options_;
    std::unique_ptr<TLogSink> Sink_;
    std::vector<TRecord> Queue_;
    std::mutex Mutex_;
    std::condition_variable Ready_;
    std::size_t Head_ = 0;
    std::size_t Count_ = 0;
    std::thread Worker_;
    std::uint64_t ProcessID_ = 0;
    std::atomic<bool> Stopped_ = false;
    std::atomic<std::uint64_t> NextID_ = 1;
    std::atomic<std::uint64_t> SuccessSequence_ = 0;
    std::atomic<std::uint64_t> SuccessBucket_ = 0;
    TCounter Submitted_{GetProcessCounters().Submitted};
    TCounter Accepted_{GetProcessCounters().Accepted};
    TCounter Sampled_{GetProcessCounters().Sampled};
    TCounter RateLimited_{GetProcessCounters().RateLimited};
    TCounter DroppedFull_{GetProcessCounters().DroppedFull};
    TCounter DroppedContended_{GetProcessCounters().DroppedContended};
    TCounter DroppedStopped_{GetProcessCounters().DroppedStopped};
    TCounter DroppedSink_{GetProcessCounters().DroppedSink};
    TCounter Written_{GetProcessCounters().Written};
    TCounter SinkErrors_{GetProcessCounters().SinkErrors};
    TCounter Backlog_{GetProcessCounters().Backlog};
};

TStructuredLogger::TStructuredLogger(std::unique_ptr<TImpl> impl)
    : Impl_(std::move(impl))
{ }

TStructuredLogger::~TStructuredLogger() = default;

std::expected<std::unique_ptr<TStructuredLogger>, std::error_code> TStructuredLogger::Open(const TLogOptions& options)
try {
    if (options.QueueCapacity < 2 || options.QueueCapacity > 1048576 ||
        options.ImportantReserve >= options.QueueCapacity || options.SuccessSampleEvery == 0 ||
        options.MaxSuccessPerSecond == 0 || options.FileCount == 0 || options.FileCount > 16 ||
        options.MaxFileBytes < 4096 || options.MaxFileBytes > 1024ULL * 1024 * 1024 ||
        (!options.Directory.empty() && options.Directory.front() != '/'))
    {
        return std::unexpected(std::make_error_code(std::errc::invalid_argument));
    }
    auto sink = std::make_unique<TLogSink>();
    if (!sink->Open(options)) {
        return std::unexpected(std::error_code(errno, std::generic_category()));
    }
    auto impl = std::make_unique<TImpl>(options, std::move(sink));
    impl->Worker_ = std::thread([raw = impl.get()]() {
        raw->Run();
    });
    return std::unique_ptr<TStructuredLogger>(new TStructuredLogger(std::move(impl)));
} catch (const std::bad_alloc&) {
    return std::unexpected(std::make_error_code(std::errc::not_enough_memory));
} catch (const std::system_error& error) {
    return std::unexpected(error.code());
} catch (...) {
    return std::unexpected(std::make_error_code(std::errc::io_error));
}

bool TStructuredLogger::TryLog(const TLogEvent& event) noexcept
{
    return Impl_->TryLog(event);
}

std::uint64_t TStructuredLogger::NewRequestID() noexcept
{
    return Impl_->NextID_.fetch_add(1);
}

TLogStats TStructuredLogger::GetStats() const noexcept
{
    return Impl_->GetStats();
}

void TStructuredLogger::Shutdown() noexcept
{
    Impl_->Shutdown();
}

} // namespace NGlamDB::NTelemetry
