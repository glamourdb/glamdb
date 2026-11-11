#include "apps/server/metrics.h"

#include "core/telemetry/metrics.h"

#include <prometheus/collectable.h>
#include <prometheus/exposer.h>
#include <prometheus/gauge.h>
#include <prometheus/registry.h>

#include <utility>

namespace glamdb::NServer {
namespace {

class TReadiness final : public prometheus::Collectable
{
public:
    explicit TReadiness(std::function<bool()> isReady)
        : IsReady_(std::move(isReady))
        , Ready_(prometheus::BuildGauge()
                     .Name("glamdb_server_ready")
                     .Help("One when at least one database is open and all managed open engines accept writes.")
                     .Labels({{"service_instance", "core"}})
                     .Register(Registry_)
                     .Add({}))
    { }

    std::vector<prometheus::MetricFamily> Collect() const override
    {
        bool ready = false;
        try {
            ready = IsReady_();
        } catch (...) {
            // A health observation failure must close readiness.
            ready = false;
        }
        Ready_.Set(ready ? 1 : 0);
        return Registry_.Collect();
    }

private:
    const std::function<bool()> IsReady_;
    prometheus::Registry Registry_;
    prometheus::Gauge& Ready_;
};

} // namespace

TMetrics::TMetrics(const std::string& address, std::function<bool()> isReady)
{
    if (!address.empty()) {
        Domain_ = NGlamDB::NTelemetry::GetMetricsCollectable();
        Readiness_ = std::make_shared<TReadiness>(std::move(isReady));
        Exposer_ = std::make_unique<prometheus::Exposer>(address);
        Exposer_->RegisterCollectable(Domain_);
        Exposer_->RegisterCollectable(Readiness_);
    }
}

TMetrics::~TMetrics() = default;

} // namespace glamdb::NServer
