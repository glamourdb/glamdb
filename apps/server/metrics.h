#pragma once

#include <functional>
#include <memory>
#include <string>

namespace prometheus {
class Collectable;
class Exposer;
} // namespace prometheus

namespace glamdb::NServer {

// The application owns the optional private listener and its collectables.
class TMetrics
{
public:
    TMetrics(const std::string& address, std::function<bool()> isReady);
    ~TMetrics();

private:
    std::shared_ptr<prometheus::Collectable> Domain_;
    std::shared_ptr<prometheus::Collectable> Readiness_;
    std::unique_ptr<prometheus::Exposer> Exposer_;
};

} // namespace glamdb::NServer
